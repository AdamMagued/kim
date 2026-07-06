#!/usr/bin/env python3
"""Standalone `codex app-server` protocol probe (parity proposal Part 0).

Speaks newline-delimited JSON-RPC 2.0 to a real `codex app-server` child and
dumps every line in both directions, prefixed `>>` (sent) / `<<` (received).
Server-initiated requests (approvals) are ALWAYS auto-declined — the probe
never approves anything, so it is safe to run in any directory.

Modes:
  probe_appserver.py                      # initialize + thread/start, then exit
  probe_appserver.py --turn "make x.txt"  # + one turn/start against the model
  probe_appserver.py --turn "…" --canned  # turn against a local canned proxy
                                          # (offline: no real model needed)
  probe_appserver.py --out transcript.jsonl

`--canned` starts a loopback HTTP server that speaks just enough of the
OpenAI Responses API to drive one deterministic turn: relay 1 returns a
`shell` tool call (`touch kim_probe.txt`), relay 2 a final text answer.
With `approvalPolicy: on-request` the tool call produces a real
`item/commandExecution/requestApproval` server request — which this probe
declines — making the recorded transcript a faithful sample of the approval
round-trip without executing anything.

Re-run after every codex upgrade and diff the transcript + regenerate the
schema snapshot (`codex app-server generate-json-schema --out
codex_engine/appserver_schema`) to spot protocol drift.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from typing import Any, Optional

CLIENT_INFO = {"name": "kim-probe", "title": "Kim app-server probe", "version": "0.1.0"}


def _log(prefix: str, obj: dict, out_file) -> None:
    line = json.dumps(obj, ensure_ascii=False)
    print(f"{prefix} {line}", flush=True)
    if out_file is not None:
        record = {"dir": "send" if prefix == ">>" else "recv", "msg": obj}
        out_file.write(json.dumps(record, ensure_ascii=False) + "\n")
        out_file.flush()


# ── Canned Responses-API model server (offline turn) ─────────────────────────


class CannedModel:
    """Loopback HTTP server speaking one deterministic Responses-API turn."""

    def __init__(self, out_file=None, escalate: bool = False) -> None:
        self._relay = 0
        self._runner = None
        self.port = 0
        self._out_file = out_file
        self._escalate = escalate

    async def start(self) -> int:
        from aiohttp import web

        app = web.Application()
        app.router.add_post("/v1/responses", self._responses)
        app.router.add_get("/v1/models", self._models)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        return self.port

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _models(self, request):
        from aiohttp import web

        return web.json_response({"object": "list", "data": [
            {"id": "kim-canned", "object": "model", "created": 0, "owned_by": "kim"},
        ]})

    async def _responses(self, request):
        from aiohttp import web

        self._relay += 1
        body = await request.json()
        if self._out_file is not None:
            record = {"dir": "model-request", "relay": self._relay, "body": body}
            self._out_file.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._out_file.flush()
        if self._relay == 1:
            # codex-cli 0.134 exposes the PTY exec tool as `exec_command`
            # (verified from the recorded relay-1 tool list; "shell" is
            # rejected with "unsupported call: shell").
            args: dict = {"cmd": "touch kim_probe.txt"}
            if self._escalate:
                # Force a real approval request: ask for escalated permissions.
                args = {
                    "cmd": "touch ~/kim_probe_escalated.txt",
                    "sandbox_permissions": "require_escalated",
                    "justification": "probe: record the approval request shape",
                }
            output = [{
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps(args),
                "call_id": "call_probe_1",
            }]
        else:
            output = [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text",
                             "text": "Probe turn complete (the command was declined)."}],
            }]
        reply = {
            "id": f"resp_probe_{self._relay}",
            "object": "response",
            "status": "completed",
            "output": output,
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        }
        if bool(body.get("stream")):
            return self._sse(reply)
        return web.json_response(reply)

    def _sse(self, reply: dict):
        from aiohttp import web

        events: list[dict] = [{"type": "response.created",
                               "response": {**reply, "status": "in_progress"}}]
        for idx, item in enumerate(reply["output"]):
            item_id = f"item_{self._relay}_{idx}"
            item = {**item, "id": item_id}
            events.append({"type": "response.output_item.added", "output_index": idx,
                           "item": {**item, "status": "in_progress"}})
            if item["type"] == "function_call":
                events.append({"type": "response.function_call_arguments.delta",
                               "item_id": item_id, "output_index": idx,
                               "delta": item["arguments"]})
                events.append({"type": "response.function_call_arguments.done",
                               "item_id": item_id, "output_index": idx,
                               "arguments": item["arguments"]})
            elif item["type"] == "message":
                text = "".join(b.get("text", "") for b in item.get("content", []))
                events.append({"type": "response.output_text.delta", "item_id": item_id,
                               "output_index": idx, "content_index": 0, "delta": text})
                events.append({"type": "response.output_text.done", "item_id": item_id,
                               "output_index": idx, "content_index": 0, "text": text})
            events.append({"type": "response.output_item.done", "output_index": idx,
                           "item": {**item, "status": "completed"}})
        events.append({"type": "response.completed", "response": reply})
        body = "".join(f"data: {json.dumps(ev)}\n\n" for ev in events) + "data: [DONE]\n\n"
        return web.Response(body=body.encode(), content_type="text/event-stream")


# ── Probe driver ─────────────────────────────────────────────────────────────


async def run_probe(args: argparse.Namespace) -> int:
    binary = args.codex_bin or os.environ.get("CODEX_BIN", "").strip() or "codex"
    resolved = shutil.which(binary) if not os.path.isabs(binary) else binary
    if not resolved:
        print(f"error: codex binary not found: {binary}", file=sys.stderr)
        return 2

    out_file = open(args.out, "w", encoding="utf-8") if args.out else None
    canned: Optional[CannedModel] = None
    cwd = args.cwd or tempfile.mkdtemp(prefix="kim-appserver-probe-")

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "USER": os.environ.get("USER", ""),
        "TMPDIR": os.environ.get("TMPDIR", ""),
        "LANG": os.environ.get("LANG", ""),
        "CODEX_API_KEY": "kim-probe-key",
    }

    proc = await asyncio.create_subprocess_exec(
        resolved, "app-server",
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert proc.stdin is not None and proc.stdout is not None

    next_id = 0
    pending: dict[int, asyncio.Future] = {}

    def send(obj: dict) -> None:
        _log(">>", obj, out_file)
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(obj) + "\n").encode())

    async def request(method: str, params: Optional[dict] = None, timeout: float = 60.0) -> Any:
        nonlocal next_id
        next_id += 1
        rid = next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        pending[rid] = fut
        send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        return await asyncio.wait_for(fut, timeout=timeout)

    turn_completed = asyncio.Event()

    async def reader() -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                print(f"?? {text}", flush=True)
                continue
            _log("<<", msg, out_file)
            if "id" in msg and "method" in msg:
                # Server request — ALWAYS auto-decline.
                method = str(msg.get("method", ""))
                if "fileChange" in method or "applyPatch" in method.lower():
                    result: dict = {"decision": "decline"}
                elif method in ("execCommandApproval", "applyPatchApproval"):
                    result = {"decision": "denied"}
                else:
                    result = {"decision": "decline"}
                send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
            elif "id" in msg:
                fut = pending.pop(int(msg["id"]), None)
                if fut is not None and not fut.done():
                    if "error" in msg:
                        fut.set_exception(RuntimeError(json.dumps(msg["error"])))
                    else:
                        fut.set_result(msg.get("result"))
            elif msg.get("method") == "turn/completed":
                turn_completed.set()

    reader_task = asyncio.create_task(reader())

    try:
        await request("initialize", {
            "clientInfo": CLIENT_INFO,
            "capabilities": {"experimentalApi": True},
        })
        send({"jsonrpc": "2.0", "method": "initialized"})

        thread_params: dict = {
            "cwd": cwd,
            "approvalPolicy": "on-request",
            "sandbox": "workspace-write",
            "ephemeral": bool(args.ephemeral),
        }
        if args.canned:
            canned = CannedModel(out_file=out_file, escalate=bool(args.escalate))
            port = await canned.start()
            thread_params.update({
                "model": "kim-canned",
                "modelProvider": "kim-proxy",
                "config": {
                    "model_providers.kim-proxy.name": "Kim Probe Proxy",
                    "model_providers.kim-proxy.base_url": f"http://127.0.0.1:{port}/v1",
                    "model_providers.kim-proxy.wire_api": "responses",
                    "model_providers.kim-proxy.env_key": "CODEX_API_KEY",
                },
            })
        started = await request("thread/start", thread_params)
        thread = (started or {}).get("thread") or {}
        thread_id = thread.get("id", "")
        print(f"-- thread started: {thread_id}", flush=True)

        if args.turn:
            await request("turn/start", {
                "threadId": thread_id,
                "input": [{"type": "text", "text": args.turn}],
            })
            await asyncio.wait_for(turn_completed.wait(), timeout=args.turn_timeout)
            print("-- turn completed", flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        reader_task.cancel()
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        if canned is not None:
            await canned.stop()
        if out_file is not None:
            out_file.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--codex-bin", default=None, help="codex binary (default: $CODEX_BIN or PATH)")
    parser.add_argument("--cwd", default=None, help="thread cwd (default: fresh temp dir)")
    parser.add_argument("--turn", default=None, help="run one turn/start with this prompt")
    parser.add_argument("--canned", action="store_true",
                        help="route the model at a local canned Responses server (offline)")
    parser.add_argument("--escalate", action="store_true",
                        help="canned turn asks for escalated permissions (forces an approval request)")
    parser.add_argument("--ephemeral", action="store_true", help="do not persist the thread")
    parser.add_argument("--turn-timeout", type=float, default=120.0)
    parser.add_argument("--out", default=None, help="also write a JSONL transcript here")
    args = parser.parse_args()
    sys.exit(asyncio.run(run_probe(args)))


if __name__ == "__main__":
    main()

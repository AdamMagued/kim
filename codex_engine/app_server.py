"""JSON-RPC 2.0 client for ``codex app-server`` (parity proposal Part 1).

Speaks newline-delimited JSON-RPC over the child's stdio. Self-contained: no
Kim imports, no globals — unit-testable against any subprocess that speaks
the protocol (see ``tests/test_app_server_client.py``'s fake server).

Probe P2 results (recorded live against codex-cli 0.134.0 on 2026-07-06;
transcript: ``codex_engine/appserver_schema/SAMPLE_TURN.jsonl``):

- With ``capabilities.experimentalApi: true`` in ``initialize``, the **v2**
  approval methods arrive as server→client *requests*:
  ``item/commandExecution/requestApproval`` / ``item/fileChange/requestApproval``
  with params ``threadId, turnId, itemId, command, cwd, reason,
  commandActions, proposedExecpolicyAmendment, availableDecisions`` and a
  ``{"decision": accept|acceptForSession|decline|cancel}`` response.
- The v1 shapes (``execCommandApproval`` / ``applyPatchApproval``, decision
  vocab ``approved|approved_for_session|denied|abort``) are still in the
  protocol schema; the dispatcher tolerates both (see ``decline_result_for``).
- A simple turn's notification sequence: ``thread/started`` → ``turn/started``
  → ``item/started``(userMessage) → ``item/completed`` →
  ``item/started``(commandExecution) → [approval request] →
  ``item/completed`` → ``thread/tokenUsage/updated`` →
  ``item/started``(agentMessage) → ``item/agentMessage/delta`` →
  ``item/completed`` → ``turn/completed``.
- The exec tool codex exposes to the model is ``exec_command`` (PTY runner);
  ``shell`` is rejected with "unsupported call: shell".

Incoming-message taxonomy (the part that matters):

- ``id`` + ``method`` → **server request** (approval etc.). MUST be answered
  or codex hangs the turn: yielded to the consumer, tracked in
  ``_outstanding``, and auto-declined on ``stop()`` if still unanswered.
- ``method`` only → notification. Yielded.
- ``id`` only → response to one of our requests. Resolves the pending future.
- Unknown/malformed lines never crash the reader — they are logged and
  skipped (tolerant parsing; the protocol is flagged experimental).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Optional, Union

logger = logging.getLogger("kim.app_server")

_SCHEMA_DIR = Path(__file__).resolve().parent / "appserver_schema"

CLIENT_INFO = {"name": "kim", "title": "Kim code mode", "version": "1.0.0"}

# asyncio's default StreamReader limit is 64 KiB per line; a single JSON-RPC
# line (e.g. turn/diff/updated with a large unified diff, or item/completed
# for a big command output) easily exceeds it and would raise ValueError out
# of the reader, killing the whole turn (C1). 16 MiB is generous headroom.
STREAM_LIMIT = 16 * 1024 * 1024

# Decision vocabularies (see module docstring). ``decline_result_for`` picks
# the right one per request method.
V2_DECLINE = {"decision": "decline"}
V1_DECLINE = {"decision": "denied"}

_V1_APPROVAL_METHODS = {"execCommandApproval", "applyPatchApproval"}


@dataclass
class ServerRequest:
    """Server→client request. Must be answered via ``AppServerClient.respond``."""

    id: Union[int, str]
    method: str
    params: dict = field(default_factory=dict)


@dataclass
class Notification:
    """Server→client notification (no reply expected)."""

    method: str
    params: dict = field(default_factory=dict)


Incoming = Union[ServerRequest, Notification]


class AppServerError(RuntimeError):
    """A JSON-RPC error response, or a dead/failed app-server process."""


def decline_result_for(method: str) -> dict:
    """The safest decline payload for an unanswered server request."""
    if method in _V1_APPROVAL_METHODS:
        return dict(V1_DECLINE)
    return dict(V2_DECLINE)


def parse_codex_version(version_output: str) -> Optional[tuple[int, int, int]]:
    """``codex --version`` output (``codex-cli 0.134.0``) → (0, 134, 0)."""
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version_output or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def pinned_schema_version() -> Optional[tuple[int, int, int]]:
    """The codex version the in-repo schema snapshot was generated from."""
    try:
        return parse_codex_version((_SCHEMA_DIR / "VERSION").read_text(encoding="utf-8"))
    except OSError:
        return None


def check_schema_drift(binary_version: str) -> tuple[bool, Optional[str]]:
    """Version gate (Part 0): (ok_to_run, warning_or_error_message).

    Refuses only on MAJOR drift from the pinned snapshot version; warns on
    minor drift; silent on patch drift or when either version is unparseable
    (an unparseable version is not evidence of incompatibility).
    """
    current = parse_codex_version(binary_version)
    pinned = pinned_schema_version()
    if current is None or pinned is None:
        return True, None
    if current[0] != pinned[0]:
        return False, (
            f"codex {'.'.join(map(str, current))} has a different MAJOR version than the "
            f"protocol snapshot Kim was built against ({'.'.join(map(str, pinned))}). "
            "Refusing the app-server transport; set codex_bridge.transport: exec or "
            "update Kim's schema snapshot (scripts/probe_appserver.py)."
        )
    if current[1] != pinned[1]:
        return True, (
            f"codex {'.'.join(map(str, current))} differs from Kim's pinned app-server "
            f"protocol snapshot ({'.'.join(map(str, pinned))}) — the protocol is "
            "experimental and may have drifted. If code mode misbehaves, regenerate "
            "the snapshot (see codex_engine/appserver_schema/)."
        )
    return True, None


class AppServerClient:
    """Async newline-delimited JSON-RPC 2.0 client for ``codex app-server``.

    Usage::

        client = AppServerClient(["codex", "app-server"], env=env)
        await client.start()
        await client.initialize()
        started = await client.request("thread/start", {...})
        async for msg in client.events():
            ...  # Notification / ServerRequest (answer via client.respond)
        await client.stop()
    """

    def __init__(
        self,
        argv: list[str],
        env: Optional[dict[str, str]] = None,
        cwd: Optional[str] = None,
        default_timeout: float = 60.0,
    ) -> None:
        self._argv = list(argv)
        self._env = dict(env) if env is not None else None
        self._cwd = cwd
        self._default_timeout = default_timeout
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._outstanding: dict[Union[int, str], str] = {}  # server request id → method
        self._queue: asyncio.Queue[Optional[Incoming]] = asyncio.Queue()
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._stderr_tail: deque[str] = deque(maxlen=60)
        self._closed = False

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Spawn the app-server child and start the reader pumps."""
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv,
            env=self._env,
            cwd=self._cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def initialize(self, client_info: Optional[dict] = None) -> dict:
        """``initialize`` handshake + ``initialized`` notification.

        Requests the experimental API so the v2 thread/turn/item methods and
        approval requests are available (verified live, see module docstring).
        """
        result = await self.request(
            "initialize",
            {
                "clientInfo": dict(client_info or CLIENT_INFO),
                "capabilities": {"experimentalApi": True},
            },
        )
        self.notify("initialized")
        return result

    async def stop(self) -> None:
        """Graceful shutdown: auto-decline outstanding approvals, terminate,
        SIGKILL fallback. Idempotent."""
        if self._closed:
            return
        self._closed = True
        # Any server request we never answered would hang codex's turn — and a
        # hung child ignores SIGTERM grace. Decline them all first.
        for req_id, method in list(self._outstanding.items()):
            with contextlib.suppress(Exception):
                await self.respond(req_id, decline_result_for(method))
        proc = self._proc
        if proc is not None and proc.returncode is None:
            # Flush + EOF stdin first: the child gets to read any buffered
            # decline lines and exit on its own before we escalate to signals.
            if proc.stdin is not None:
                with contextlib.suppress(Exception):
                    await proc.stdin.drain()
                with contextlib.suppress(Exception):
                    proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._fail_pending(AppServerError("app-server client stopped"))
        # Wake any events() consumer.
        self._queue.put_nowait(None)

    def stderr_tail(self) -> str:
        """Recent stderr output, for surfacing on abnormal exit."""
        return "\n".join(self._stderr_tail)

    @property
    def returncode(self) -> Optional[int]:
        return self._proc.returncode if self._proc is not None else None

    # ── Outgoing ─────────────────────────────────────────────────────────────

    async def request(
        self, method: str, params: Optional[dict] = None, timeout: Optional[float] = None
    ) -> dict:
        """Send an id-correlated request; await (and return) its result."""
        self._next_id += 1
        req_id = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            self._write({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
            result = await asyncio.wait_for(fut, timeout=timeout or self._default_timeout)
        except asyncio.TimeoutError:
            raise AppServerError(f"app-server request timed out: {method}") from None
        finally:
            self._pending.pop(req_id, None)
        return result if isinstance(result, dict) else {}

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

    async def respond(self, request_id: Union[int, str], result: dict) -> None:
        """Answer a server-initiated request (approval decision etc.)."""
        self._outstanding.pop(request_id, None)
        self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    # ── Incoming ─────────────────────────────────────────────────────────────

    async def events(self) -> AsyncIterator[Incoming]:
        """Yield notifications and server requests until the child exits."""
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    # ── Internals ────────────────────────────────────────────────────────────

    def _write(self, payload: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise AppServerError("app-server process is not running")
        try:
            proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
            raise AppServerError(f"app-server stdin write failed: {exc}") from exc

    def _fail_pending(self, error: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(error)
        self._pending.clear()

    async def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                try:
                    raw = await proc.stdout.readline()
                except ValueError:
                    # One line exceeded STREAM_LIMIT. The reader keeps the
                    # buffered data, so subsequent readline() calls drain the
                    # oversized line in fragments — each fails JSON parsing and
                    # is skipped. Degrade gracefully instead of killing the
                    # turn (C1).
                    logger.warning(
                        "app-server emitted a line longer than %d bytes; skipping it",
                        STREAM_LIMIT,
                    )
                    continue
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("app-server non-JSON line ignored: %.200s", line)
                    continue
                if not isinstance(msg, dict):
                    continue
                self._dispatch(msg)
        finally:
            # Child exited (or reader cancelled): fail in-flight requests and
            # end the events() stream.
            if not self._closed:
                tail = self.stderr_tail()
                detail = f" stderr tail:\n{tail}" if tail else ""
                self._fail_pending(
                    AppServerError(f"codex app-server exited unexpectedly.{detail}")
                )
                self._queue.put_nowait(None)

    def _dispatch(self, msg: dict) -> None:
        has_id = "id" in msg
        method = msg.get("method")
        if has_id and isinstance(method, str):
            req = ServerRequest(id=msg["id"], method=method, params=_params_of(msg))
            self._outstanding[req.id] = method
            self._queue.put_nowait(req)
        elif isinstance(method, str):
            self._queue.put_nowait(Notification(method=method, params=_params_of(msg)))
        elif has_id:
            raw_id = msg["id"]
            try:
                key = int(raw_id)
            except (TypeError, ValueError):
                logger.debug("app-server response with non-numeric id ignored: %r", raw_id)
                return
            fut = self._pending.pop(key, None)
            if fut is None or fut.done():
                return
            if "error" in msg:
                err = msg.get("error")
                text = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                fut.set_exception(AppServerError(text))
            else:
                fut.set_result(msg.get("result"))
        else:
            logger.debug("app-server message with neither id nor method ignored")

    async def _read_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        while True:
            try:
                raw = await proc.stderr.readline()
            except ValueError:
                logger.warning("app-server stderr line exceeded stream limit; skipping")
                continue
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                self._stderr_tail.append(line)
                logger.debug("app-server stderr: %s", line)


def _params_of(msg: dict) -> dict:
    params = msg.get("params")
    return params if isinstance(params, dict) else {}

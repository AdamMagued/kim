"""App-server transport for the codex bridge service (parity proposal Part 2).

The second engine path behind ``codex_bridge:  transport: app-server``:
instead of spawning ``codex exec --json`` per message, spawn
``codex app-server`` (JSON-RPC over stdio, ``codex_engine/app_server.py``),
resume the session's persisted codex thread (sidecar ``codex_thread_id``),
run exactly ONE ``turn/start``, translate protocol notifications into Kim's
typed events, bridge native approval requests over the existing stdin
decision channel, and exit. The spawn-per-message process model, the
``_CodexProxy`` (model transport to the browser LLM), the sidecar location,
and the ``TASK_COMPLETE:`` final-line contract are all unchanged.

What this buys over exec (why Phase 3 exists):

- **Native per-command approvals** — codex asks *before* running an escalated
  command (``item/commandExecution/requestApproval``); Kim renders it and the
  user's accept / acceptForSession / decline goes straight back to codex.
  ``KIM_CODEX_BYPASS_SANDBOX`` is retired on this path (S5): full-auto maps to
  ``approvalPolicy: never`` **inside** the workspace-write sandbox, never to
  ``--dangerously-bypass-approvals-and-sandbox``.
- **True session resume** — ``thread.id`` is persisted in the thread-state
  sidecar and ``thread/resume`` restores codex's own transcript, so codex
  remembers its earlier tool outputs across messages.
- **Live UX** — plan checklist, streaming command output, unified diff and
  token usage all arrive as protocol notifications and are re-emitted as the
  Part-3 typed Kim events.
- The browser LLM is **model-only**: tool execution happens natively inside
  codex's sandbox, not by Kim parsing prose.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import queue
import re
import signal
import sys
import threading
from typing import Any, Awaitable, Callable, Optional

from codex_engine.app_server import (
    AppServerClient,
    AppServerError,
    Notification,
    ServerRequest,
    check_schema_drift,
    decline_result_for,
)
from orchestrator.events_gen import (
    LOG_TAG_FAILED,
    LOG_TAG_TASK_COMPLETE,
    emit_answer,
    emit_assistant_delta,
    emit_command_approval_request,
    emit_command_output,
    emit_diff_update,
    emit_file_change_approval_request,
    emit_item_lifecycle,
    emit_plan_update,
    emit_reasoning_delta,
    emit_status,
    emit_token_usage,
    emit_turn_lifecycle,
)

logger = logging.getLogger("kim.codex_appserver")

# Provider names must never leak into user-visible reasoning (same rule as
# engine._surface_relay_reasoning).
_PROVIDER_NAME_RE = re.compile(r"\b(Gemini|Claude|ChatGPT|Grok|DeepSeek)\b", re.IGNORECASE)

_V1_APPROVAL_METHODS = {"execCommandApproval", "applyPatchApproval"}
_APPROVAL_METHODS = _V1_APPROVAL_METHODS | {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
}

# v1 ReviewDecision vocabulary (see appserver_schema/ExecCommandApprovalResponse).
_V1_DECISION = {"accept": "approved", "acceptForSession": "approved_for_session", "decline": "denied"}


def transport_name(config: dict) -> str:
    """Normalized ``codex_bridge.transport`` value (default ``exec``)."""
    raw = str(((config.get("codex_bridge") or {}).get("transport")) or "exec")
    normalized = raw.strip().lower().replace("_", "-").replace("appserver", "app-server")
    return normalized if normalized in ("exec", "app-server") else "exec"


def _bridge_cfg(config: dict) -> dict:
    cfg = config.get("codex_bridge")
    return cfg if isinstance(cfg, dict) else {}


def _cfg_int(cfg: dict, key: str, default: int) -> int:
    try:
        return int(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def build_appserver_env(bearer_token: str) -> dict[str, str]:
    """Minimal env for the ``codex app-server`` child.

    Mirrors the exec path's allowlist (#1) with two deltas:
    - ``CODEX_API_KEY`` carries the proxy bearer (thread/start config sets
      ``env_key = "CODEX_API_KEY"``); no OPENAI_* needed — the base_url is an
      inline config override, not env.
    - ``KIM_APPROVAL_SOCK`` / ``KIM_APPROVAL_TCP`` are forwarded so any Kim MCP
      tools codex spawns can still reach the per-session approval broker
      (without them, codex-side MCP approvals default-deny).
    - NO ``CODEX_HOME`` override: the user's real ``~/.codex`` (MCP servers,
      skills) applies on this path.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "USER": os.environ.get("USER", ""),
        "TMPDIR": os.environ.get("TMPDIR", ""),
        "LANG": os.environ.get("LANG", ""),
        "CODEX_API_KEY": bearer_token,
    }
    for var in ("KIM_APPROVAL_SOCK", "KIM_APPROVAL_TCP"):
        if os.environ.get(var):
            env[var] = os.environ[var]
    if sys.platform == "win32":
        for var in ("SystemRoot", "ComSpec", "USERPROFILE", "TEMP", "TMP", "Path"):
            if var in os.environ:
                env[var] = os.environ[var]
    return env


def resolve_policies(config: dict) -> tuple[str, str]:
    """(approvalPolicy, sandbox) for this run — the S5 mapping.

    ``KIM_HITL_RISK_THRESHOLD`` set (permission mode "always ask"/"ask risky")
    → the configured approval policy (default ``on-request``). Unset (full
    auto) → ``never``, still INSIDE the sandbox. ``KIM_CODEX_BYPASS_SANDBOX``
    is deliberately ignored here: the app-server path never bypasses the
    sandbox — escalation is what native approvals are for.
    """
    cfg = _bridge_cfg(config)
    approval = str(cfg.get("approval_policy") or "on-request").strip() or "on-request"
    sandbox = str(cfg.get("sandbox_mode") or "workspace-write").strip() or "workspace-write"
    if not os.environ.get("KIM_HITL_RISK_THRESHOLD", "").strip():
        approval = "never"
    if os.environ.get("KIM_CODEX_BYPASS_SANDBOX", "").strip() == "1":
        emit_status(
            "Note: KIM_CODEX_BYPASS_SANDBOX is ignored on the app-server transport — "
            "commands run in the workspace-write sandbox and escalations ask for approval."
        )
    return approval, sandbox


def _stdin_is_interactive() -> bool:
    """True when someone is on the other end of our stdin to answer approvals."""
    if os.environ.get("KIM_TAURI_MODE") == "1":
        return True
    if os.environ.get("KIM_STDIN_APPROVALS") == "1":  # set by the kim CLI (Part 4)
        return True
    try:
        return sys.stdin.isatty()
    except Exception:  # noqa: BLE001
        return False


def parse_decision_line(line: str) -> Optional[tuple[str, Optional[str]]]:
    """Parse one stdin decision line → (decision, request_id) or None.

    Accepts the Part-3 vocabulary
    ``{"type":"approval_decision","id":…,"decision":"accept"|"acceptForSession"|"decline"}``
    and stays backward-compatible with the legacy shapes
    ``{"type":"hitl_approve","approved":bool[,"decision":…]}`` and bare
    ``{"approved": bool}``. Unrelated lines (steering etc.) return None.
    """
    try:
        data = json.loads(line.strip())
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    kind = data.get("type")
    if kind not in (None, "approval_decision", "hitl_approve"):
        return None
    raw_id = data.get("id")
    req_id = str(raw_id) if raw_id is not None else None
    decision = data.get("decision")
    if isinstance(decision, str) and decision in ("accept", "acceptForSession", "decline"):
        return decision, req_id
    if "approved" in data:
        return ("accept" if bool(data.get("approved")) else "decline"), req_id
    return None


class _StdinDecisionPump:
    """T1/H4: the single owned stdin reader for this process.

    The old per-request ``run_in_executor(sys.stdin.readline)`` leaked a
    blocked reader thread whenever ``wait_for`` timed out — cancellation does
    not stop a blocking ``readline``, so the abandoned thread eventually
    consumed the NEXT approval's decision line. This pump owns stdin with one
    daemon thread for the life of the process and hands parsed decision
    tuples to whoever is currently waiting; non-decision lines are ignored.

    Also intended for reuse by codex_bridge_service's own gate reads so the
    process keeps exactly one stdin reader.
    """

    _EOF = ("__eof__", None)

    def __init__(self) -> None:
        self._q: "queue.Queue[tuple[str, Optional[str]]]" = queue.Queue()
        self._started = False
        self._start_lock = threading.Lock()
        self._eof = False

    def start(self) -> None:
        with self._start_lock:
            if self._started:
                return
            self._started = True
            threading.Thread(
                target=self._read_loop, daemon=True, name="kim-stdin-decisions"
            ).start()

    def _read_loop(self) -> None:
        try:
            for line in sys.stdin:
                parsed = parse_decision_line(line)
                if parsed is not None:
                    self._q.put(parsed)
                # Not a decision (e.g. a steer line) — ignore, keep reading.
        except Exception as exc:  # noqa: BLE001
            logger.warning("approval stdin read failed (%s) — declining", exc)
        self._eof = True
        self._q.put(self._EOF)  # wake any waiter: the supervisor went away

    def drain(self) -> None:
        """Drop queued (stale) decisions — none can belong to a request that
        has not started waiting yet."""
        while True:
            try:
                stale = self._q.get_nowait()
            except queue.Empty:
                return
            if stale == self._EOF:
                self._q.put(self._EOF)
                return
            logger.warning("dropping stale approval decision line: id=%r", stale[1])

    def _get_blocking(self, timeout: float) -> Any:
        """One bounded queue read → decision tuple | "empty" | "eof"."""
        try:
            item = self._q.get(timeout=timeout)
        except queue.Empty:
            return "empty"
        if item == self._EOF:
            self._q.put(self._EOF)  # keep EOF visible to later waiters
            return "eof"
        return item

    async def read_decision(self, timeout: float) -> Optional[tuple[str, Optional[str]]]:
        """Await the next decision (or None on timeout/EOF).

        Polls the queue in short blocking slices so that if this coroutine is
        cancelled/abandoned, the worker thread expires within ~0.25 s instead
        of squatting on the queue where it could swallow a later decision.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            result = await loop.run_in_executor(
                None, self._get_blocking, min(0.25, remaining)
            )
            if result == "eof":
                return None
            if result != "empty":
                return result


_DECISION_PUMP: Optional[_StdinDecisionPump] = None


def get_decision_pump() -> _StdinDecisionPump:
    global _DECISION_PUMP
    if _DECISION_PUMP is None:
        _DECISION_PUMP = _StdinDecisionPump()
    return _DECISION_PUMP


async def _read_stdin_decision(timeout: float) -> Optional[tuple[str, Optional[str]]]:
    """Wait for the next decision line (or timeout/EOF → None).

    Routed through the process-wide stdin pump so a timed-out wait never
    leaves an orphaned reader thread blocked on stdin (T1/H4).
    """
    pump = get_decision_pump()
    pump.start()
    return await pump.read_decision(timeout)


def _scrub(text: str) -> str:
    return _PROVIDER_NAME_RE.sub("Kim", text)


def _summarize_command_actions(params: dict) -> str:
    command = params.get("command")
    if isinstance(command, str) and command.strip():
        return command
    if isinstance(command, list):  # v1 argv shape
        return " ".join(str(part) for part in command)
    return ""


class AppServerTurnRunner:
    """One message = one run: resume-or-start the thread, run one turn."""

    def __init__(
        self,
        *,
        task: str,
        cwd: str,
        model: Optional[str],
        config: dict,
        proxy_port: int,
        bearer_token: str,
        thread_state: dict,
        binary_path: str,
        client: Optional[Any] = None,
        decision_reader: Optional[Callable[[float], Awaitable[Optional[tuple[str, Optional[str]]]]]] = None,
        register_process: Optional[Callable[[Any], None]] = None,
        install_signal_handler: bool = True,
    ) -> None:
        self._task = task
        self._cwd = cwd
        self._model = model
        self._config = config
        self._cfg = _bridge_cfg(config)
        self._proxy_port = proxy_port
        self._bearer = bearer_token
        self._state = thread_state
        self._binary = binary_path
        self._client = client  # injected fake in tests; real one built in run()
        self._decision_reader = decision_reader or _read_stdin_decision
        self._register_process = register_process
        self._install_signal_handler = install_signal_handler
        self._interactive = _stdin_is_interactive()
        self._approval_timeout = float(_cfg_int(self._cfg, "approval_timeout_s", 120))
        self._compact_budget = _cfg_int(self._cfg, "codex_compact_tokens", 300_000)
        self._compact_fired = False
        self._thread_id = ""
        self._turn_id = ""
        self._turn_status = ""
        self._turn_error = ""
        self._answer_parts: list[str] = []
        self._interrupted = False
        self._item_titles: dict[str, str] = {}

    # ── Public entry ─────────────────────────────────────────────────────────

    async def run(self) -> int:
        client = self._client
        if client is None:
            client = AppServerClient(
                [self._binary, "app-server"],
                env=build_appserver_env(self._bearer),
            )
        self._client = client
        previous_handler: Any = None
        # start() is inside the try so a failure after the child spawns
        # (initialize error, register hook raising) still reaches the
        # finally's client.stop() and never leaks the codex process (L7).
        try:
            await client.start()
            if self._register_process is not None:
                proc = getattr(client, "_proc", None)
                if proc is not None:
                    self._register_process(proc)
            await client.initialize()
            await self._resume_or_start()
            if self._install_signal_handler:
                previous_handler = self._install_sigterm()
            return await self._run_turn()
        except AppServerError as exc:
            print(f"{LOG_TAG_FAILED} Codex app-server error: {exc}", flush=True)
            return 1
        finally:
            if previous_handler is not None:
                with contextlib.suppress(Exception):
                    signal.signal(signal.SIGTERM, previous_handler)
            await client.stop()

    # ── Thread lifecycle ─────────────────────────────────────────────────────

    def _thread_config_overrides(self) -> dict:
        """Inline dotted config: route the kim-proxy provider at our proxy.

        Mirrors ``_write_codex_config`` (engine.py) — same provider id, wire
        API and env key — but with no temp CODEX_HOME: the user's real
        ``~/.codex`` config applies.
        """
        return {
            "model_providers.kim-proxy.name": "Kim Proxy",
            "model_providers.kim-proxy.base_url": f"http://127.0.0.1:{self._proxy_port}/v1",
            "model_providers.kim-proxy.wire_api": "responses",
            "model_providers.kim-proxy.env_key": "CODEX_API_KEY",
        }

    def _safe_model(self) -> str:
        model = self._model
        if model:
            cleaned = re.sub(r"[^A-Za-z0-9_\-.:/ ]", "", model)[:128]
            if cleaned:
                return cleaned
        return "kim-proxy-model"

    async def _resume_or_start(self) -> None:
        assert self._client is not None
        approval, sandbox = resolve_policies(self._config)
        stored_id = str(self._state.get("codex_thread_id") or "")
        stored_cwd = str(self._state.get("codex_thread_cwd") or "")
        common = {
            "cwd": self._cwd,
            "approvalPolicy": approval,
            "sandbox": sandbox,
            "model": self._safe_model(),
            "modelProvider": "kim-proxy",
            "config": self._thread_config_overrides(),
        }
        if stored_id and stored_cwd == self._cwd:
            try:
                result = await self._client.request(
                    "thread/resume", {"threadId": stored_id, **common}
                )
                self._remember_thread(result, resumed=True)
                return
            except AppServerError as exc:
                # Deleted/foreign thread — degrade to a fresh one.
                logger.warning("thread/resume %s failed (%s) — starting fresh", stored_id, exc)
                emit_status("Could not resume the previous codex session — starting a fresh one…")
        result = await self._client.request("thread/start", common)
        self._remember_thread(result, resumed=False)

    def _remember_thread(self, result: dict, *, resumed: bool) -> None:
        thread = result.get("thread") if isinstance(result, dict) else None
        thread_id = str((thread or {}).get("id") or "")
        if not thread_id:
            raise AppServerError("thread/start returned no thread id")
        self._thread_id = thread_id
        # Persist immediately-adjacent state; the service saves the sidecar in
        # its finally (crash-safe enough: a lost id only costs continuity).
        self._state["codex_thread_id"] = thread_id
        self._state["codex_thread_cwd"] = self._cwd
        if resumed:
            emit_status("Resumed the previous codex session for this project.")

    # ── The turn ─────────────────────────────────────────────────────────────

    async def _run_turn(self) -> int:
        assert self._client is not None
        task_timeout = _cfg_int(self._cfg, "task_timeout_s", 1800)
        await self._client.request(
            "turn/start",
            {"threadId": self._thread_id, "input": [{"type": "text", "text": self._task}]},
        )
        try:
            await asyncio.wait_for(self._pump(), timeout=task_timeout)
        except asyncio.TimeoutError:
            print(
                f"{LOG_TAG_FAILED} Codex task timed out after {task_timeout // 60} minutes.",
                flush=True,
            )
            return 1
        return self._finish()

    def _finish(self) -> int:
        if self._turn_status == "completed":
            answer = "\n\n".join(part for part in self._answer_parts if part.strip()).strip()
            answer = answer or "Task completed."
            emit_answer(answer)
            print(f"{LOG_TAG_TASK_COMPLETE} {answer}", flush=True)
            return 0
        if self._turn_status == "interrupted" or self._interrupted:
            emit_status("Task interrupted.")
            return 130
        detail = self._turn_error or f"turn ended with status {self._turn_status or 'unknown'}"
        print(f"{LOG_TAG_FAILED} Codex turn failed: {detail}", flush=True)
        return 1

    async def _pump(self) -> None:
        assert self._client is not None
        async for msg in self._client.events():
            if isinstance(msg, ServerRequest):
                await self._handle_server_request(msg)
            elif isinstance(msg, Notification):
                done = await self._handle_notification(msg)
                if done:
                    return
        # events() ended without turn/completed → the child died.
        if not self._turn_status:
            tail = ""
            with contextlib.suppress(Exception):
                tail = self._client.stderr_tail()
            self._turn_error = "codex app-server exited before the turn completed" + (
                f"\n{tail}" if tail else ""
            )
            self._turn_status = "failed"

    # ── Server requests (approvals) ──────────────────────────────────────────

    async def _handle_server_request(self, req: ServerRequest) -> None:
        assert self._client is not None
        if req.method not in _APPROVAL_METHODS:
            # Unknown server request: answering wrongly is worse than the
            # safest structured decline (codex tolerates unknown-field results
            # poorly, but an unanswered request hangs the turn forever).
            logger.warning("Unknown app-server request %s — declining", req.method)
            await self._client.respond(req.id, decline_result_for(req.method))
            return
        decision = await self._collect_decision(req)
        await self._client.respond(req.id, self._decision_result(req.method, decision))

    def _decision_result(self, method: str, decision: str) -> dict:
        if method in _V1_APPROVAL_METHODS:
            return {"decision": _V1_DECISION.get(decision, "denied")}
        return {"decision": decision}

    async def _collect_decision(self, req: ServerRequest) -> str:
        params = req.params
        request_id = str(req.id)
        is_file_change = "fileChange" in req.method or "applyPatch" in req.method
        if is_file_change:
            item_id = str(params.get("itemId") or params.get("callId") or "")
            emit_file_change_approval_request(
                id=request_id,
                files=self._file_change_files(params, item_id),
                reason=str(params.get("reason") or ""),
            )
        else:
            network_ctx = params.get("networkApprovalContext")
            emit_command_approval_request(
                id=request_id,
                command=_summarize_command_actions(params),
                cwd=str(params.get("cwd") or ""),
                reason=str(params.get("reason") or params.get("justification") or ""),
                risk="network" if network_ctx else "",
                network=bool(network_ctx),
                amendment=list(params.get("proposedExecpolicyAmendment") or []),
            )
        if not self._interactive:
            emit_status(
                "No interactive approval channel (not running under the Kim app or CLI) — "
                "declining the escalated action. Re-run from Kim to approve it."
            )
            return "decline"
        # T1: decisions queued before this request started waiting are stale
        # (e.g. a late Approve for an earlier, timed-out prompt) — drop them
        # instead of letting them authorize THIS request. Only the default
        # stdin reader has a queue to drain; injected test readers do not.
        if self._decision_reader is _read_stdin_decision:
            get_decision_pump().drain()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._approval_timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                emit_status(
                    f"No approval decision within {int(self._approval_timeout)}s — declining."
                )
                return "decline"
            parsed = await self._decision_reader(remaining)
            if parsed is None:
                emit_status(
                    f"No approval decision within {int(self._approval_timeout)}s — declining."
                )
                return "decline"
            decision, echo_id = parsed
            if echo_id is None or echo_id == request_id:
                # Id-less decisions are accepted for backward compatibility
                # with pre-T1 writers (legacy {"approved": bool} lines).
                return decision
            logger.warning(
                "discarding approval decision for id %r — it does not match the "
                "pending request %r", echo_id, request_id,
            )

    def _file_change_files(self, params: dict, item_id: str) -> list:
        """Best-effort [{path, kind}] from the tracked fileChange item."""
        raw = self._item_titles.get(f"changes:{item_id}")
        if raw:
            with contextlib.suppress(Exception):
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return parsed
        return []

    # ── Notifications ────────────────────────────────────────────────────────

    async def _handle_notification(self, note: Notification) -> bool:
        """Translate one protocol notification. Returns True when the turn is over."""
        method = note.method
        params = note.params
        if method == "turn/started":
            turn = params.get("turn") or {}
            self._turn_id = str(turn.get("id") or "")
            emit_turn_lifecycle(phase="started", turn_id=self._turn_id)
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            self._turn_status = str(turn.get("status") or "completed")
            error = turn.get("error")
            if isinstance(error, dict):
                self._turn_error = str(error.get("message") or json.dumps(error))
            elif error:
                self._turn_error = str(error)
            phase = "interrupted" if self._turn_status == "interrupted" else (
                "completed" if self._turn_status == "completed" else "failed"
            )
            emit_turn_lifecycle(phase=phase, turn_id=str(turn.get("id") or self._turn_id))
            return True
        elif method == "item/started" or method == "item/completed":
            self._on_item(method.split("/", 1)[1], params)
        elif method == "item/agentMessage/delta":
            emit_assistant_delta(chunk=_scrub(str(params.get("delta") or "")))
        elif method in ("item/reasoning/textDelta", "item/reasoning/summaryTextDelta"):
            emit_reasoning_delta(chunk=_scrub(str(params.get("delta") or "")))
        elif method == "item/commandExecution/outputDelta":
            emit_command_output(
                item_id=str(params.get("itemId") or ""),
                chunk=str(params.get("delta") or ""),
            )
        elif method == "turn/plan/updated":
            emit_plan_update(steps=list(params.get("plan") or []))
        elif method == "turn/diff/updated":
            emit_diff_update(unified_diff=str(params.get("diff") or ""))
        elif method == "thread/tokenUsage/updated":
            await self._on_token_usage(params)
        elif method == "thread/compacted":
            emit_status("Codex compacted its own transcript.")
        elif method == "error":
            message = str(params.get("message") or params.get("error") or params)
            emit_status(f"codex error: {_scrub(message)}")
        elif method == "warning":
            message = str(params.get("message") or "")
            # The kim-proxy model has no upstream metadata — that warning is
            # expected noise on every run; anything else is worth surfacing.
            if message and "Model metadata" not in message:
                emit_status(f"codex warning: {_scrub(message)}")
        # Everything else (account/*, mcpServer/*, fuzzyFileSearch/*, …) is
        # deliberately ignored — tolerant by default.
        return False

    def _on_item(self, phase: str, params: dict) -> None:
        item = params.get("item")
        if not isinstance(item, dict):
            return
        kind = str(item.get("type") or "")
        item_id = str(item.get("id") or "")
        title = ""
        if kind == "commandExecution":
            title = str(item.get("command") or "")
        elif kind == "fileChange":
            changes = item.get("changes")
            if isinstance(changes, list):
                paths = [str(c.get("path") or "") for c in changes if isinstance(c, dict)]
                title = ", ".join(p for p in paths if p)
                # Stash the raw change list for a fileChange approval request.
                with contextlib.suppress(Exception):
                    self._item_titles[f"changes:{item_id}"] = json.dumps([
                        {"path": c.get("path"), "kind": c.get("kind")}
                        for c in changes if isinstance(c, dict)
                    ])
        elif kind == "agentMessage":
            if phase == "completed":
                text = str(item.get("text") or "")
                if text.strip():
                    self._answer_parts.append(_scrub(text))
        elif kind == "userMessage":
            return  # echo of our own input — not activity
        emit_item_lifecycle(item_id=item_id, kind=kind, phase=phase, title=_scrub(title))

    async def _on_token_usage(self, params: dict) -> None:
        usage = params.get("tokenUsage")
        total = usage.get("total") if isinstance(usage, dict) else None
        if not isinstance(total, dict):
            return
        input_tokens = int(total.get("inputTokens") or 0)
        output_tokens = int(total.get("outputTokens") or 0)
        total_tokens = int(total.get("totalTokens") or input_tokens + output_tokens)
        emit_token_usage(input=input_tokens, output=output_tokens, total=total_tokens)
        if (
            self._compact_budget > 0
            and total_tokens >= self._compact_budget
            and not self._compact_fired
            and self._thread_id
        ):
            self._compact_fired = True
            emit_status("Codex transcript near its budget — compacting it natively…")
            assert self._client is not None
            with contextlib.suppress(AppServerError, asyncio.TimeoutError):
                await self._client.request(
                    "thread/compact/start", {"threadId": self._thread_id}, timeout=30.0
                )

    # ── Cancellation ─────────────────────────────────────────────────────────

    def _install_sigterm(self) -> Any:
        """SIGTERM → graceful ``turn/interrupt``, hard client stop after 3s."""
        loop = asyncio.get_running_loop()

        def _handler(_sig: int, _frame: object) -> None:
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._interrupt()))

        try:
            return signal.signal(signal.SIGTERM, _handler)
        except (OSError, ValueError):
            return None

    async def _interrupt(self) -> None:
        if self._interrupted:
            return
        self._interrupted = True
        emit_status("Stopping — asking codex to interrupt the turn…")
        client = self._client
        if client is not None and self._thread_id and self._turn_id:
            with contextlib.suppress(Exception):
                await client.request(
                    "turn/interrupt",
                    {"threadId": self._thread_id, "turnId": self._turn_id},
                    timeout=3.0,
                )
        await asyncio.sleep(3.0)
        # If the turn still hasn't completed, force the pump to end.
        if not self._turn_status and client is not None:
            with contextlib.suppress(Exception):
                await client.stop()


# ── Service-facing entry points ───────────────────────────────────────────────


async def check_binary_version(binary_path: str) -> tuple[bool, Optional[str]]:
    """Part-0 version gate: run ``codex --version``; refuse only on major drift."""
    proc: Optional[asyncio.subprocess.Process] = None
    try:
        proc = await asyncio.create_subprocess_exec(
            binary_path, "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        version_output = raw.decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        # A hung `codex --version` must not be leaked on timeout (L8/C3):
        # wait_for cancels communicate() but leaves the child running — reap it
        # so a hung version check doesn't orphan a codex process.
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
        logger.warning("codex --version failed (%s) — skipping the version gate", exc)
        return True, None
    return check_schema_drift(version_output)


async def run_app_server_task(
    *,
    task: str,
    cwd: str,
    model: Optional[str],
    config: dict,
    proxy: Any,
    thread_state: dict,
    binary_path: str,
    client: Optional[Any] = None,
    decision_reader: Optional[Callable[[float], Awaitable[Optional[tuple[str, Optional[str]]]]]] = None,
    register_process: Optional[Callable[[Any], None]] = None,
    install_signal_handler: bool = True,
) -> int:
    """Run one code-mode message over the app-server transport.

    The caller (codex_bridge_service) has already: gated HITL/git, started the
    ``_CodexProxy`` and loaded the thread-state sidecar (which it saves again
    in its ``finally``).
    """
    ok, message = await check_binary_version(binary_path)
    if message:
        emit_status(message)
    if not ok:
        print(f"{LOG_TAG_FAILED} {message}", flush=True)
        return 1
    # Rb6: MAX_RELAYS is per-turn on this path.
    if hasattr(proxy, "begin_turn"):
        proxy.begin_turn()
    runner = AppServerTurnRunner(
        task=task,
        cwd=cwd,
        model=model,
        config=config,
        proxy_port=int(getattr(proxy, "_port", 0) or 0),
        bearer_token=str(getattr(proxy, "_bearer_token", "")),
        thread_state=thread_state,
        binary_path=binary_path,
        client=client,
        decision_reader=decision_reader,
        register_process=register_process,
        install_signal_handler=install_signal_handler,
    )
    return await runner.run()


async def compact_codex_thread(
    *,
    cwd: str,
    config: dict,
    thread_state: dict,
    binary_path: str,
    client: Optional[Any] = None,
) -> bool:
    """Fire a native ``thread/compact/start`` for the sidecar's codex thread.

    Used by the ``/compact`` control task so BOTH context budgets shrink: the
    browser thread (existing ``_compact_browser_thread``) and codex's own
    transcript. Best-effort: any failure returns False and costs nothing.
    """
    thread_id = str(thread_state.get("codex_thread_id") or "")
    stored_cwd = str(thread_state.get("codex_thread_cwd") or "")
    if not thread_id or stored_cwd != cwd:
        return False
    own_client = client is None
    if client is None:
        client = AppServerClient([binary_path, "app-server"], env=build_appserver_env("kim-compact"))
    try:
        if own_client:
            await client.start()
            await client.initialize()
        await client.request("thread/resume", {"threadId": thread_id, "cwd": cwd})
        await client.request("thread/compact/start", {"threadId": thread_id}, timeout=120.0)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("codex-side compact failed: %s", exc)
        return False
    finally:
        if own_client:
            with contextlib.suppress(Exception):
                await client.stop()

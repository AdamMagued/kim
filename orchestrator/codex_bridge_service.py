"""
Codex bridge service — lifecycle-managed launcher for browser-backed Codex runs.

Merges ``orchestrator/run_codex_bridge.py`` and the subprocess-launch logic from
``codex_engine/engine.py`` into a single module with:

- ``atexit`` handler: kills the Codex process on any exit path
- ``signal.SIGTERM`` handler: same cleanup (Tauri sends SIGTERM on cancel)
- Codex stderr forwarded to stdout as ``[STATUS] codex error: {line}``
- The user's real CODEX_HOME (``~/.codex``) is honored on both transports;
  Kim only layers its proxy-provider routing on top (``-c`` overrides on the
  exec path, inline thread config on the app-server path).

Usage (invoked by Tauri subprocess.rs, not by humans):
    python -m orchestrator.codex_bridge_service \\
        --task "write fibonacci.py and test it" \\
        --cwd  /path/to/project \\
        --provider browser:gemini
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import concurrent.futures
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import AsyncIterator, Optional

from orchestrator.events_gen import (
    LOG_TAG_ERROR,
    LOG_TAG_FAILED,
    LOG_TAG_TASK_COMPLETE,
    emit_hitl_approval_request,
    emit_run_done,
    emit_status,
    emit_agent_done,
    emit_agent_cancelled,
    emit_agent_error,
)

from codex_engine.engine import (
    CODEX_BINARY,
    _CodexProxy,
    _codex_browser_system_prompt,
    _get_compact_threshold,
    _is_benign_codex_stderr,
)
from codex_engine.thread_state import (
    load_thread_state,
    reset_thread_state,
    save_thread_state,
)
from mcp_server.policy_platform import _with_windows_env_aliases
from orchestrator.process_kill import _kill_process_tree
from orchestrator.codex_appserver_transport import (
    compact_codex_thread,
    parse_decision_line,
    run_app_server_task,
    transport_name,
)
from orchestrator.compact_prompt import (
    _parse_compact_json,
    build_in_thread_compact_prompt,
    render_handoff_text,
)
from orchestrator.providers.base import create_provider
from orchestrator.agent_config import load_config as _shared_load_config

# Control tasks that compact the code-mode browser thread instead of running
# Codex. Mirrors _COMPACT_CONTROL_TASKS in orchestrator/agent.py so /compact
# behaves the same in the Code tab and CLI code mode as in normal chat.
_COMPACT_CONTROL_TASKS = {"/compact", "compact", "__kim_compact_context__"}

# Default config.yaml resolution (when --config is omitted) now lives in the
# shared orchestrator.agent_config.load_config helper (see _load_config
# below). Import resolution for codex_engine.*, orchestrator.* and
# mcp_server.* comes from PYTHONPATH=kim_root set by the Tauri launcher
# (subprocess.rs), so no sys.path manipulation is needed here.

logger = logging.getLogger("kim.codex_bridge_service")

# asyncio's default StreamReader limit is 64 KiB per line. `codex exec --json`
# emits whole tool results (e.g. a large file read) as ONE JSON line, which
# blows past that and raises ValueError out of the stream iterator, killing
# the task with "Codex bridge error: Separator is found…" (C1).
_STREAM_LIMIT = 16 * 1024 * 1024

# How long to wait for the codex exec child to exit after both of its pipes
# hit EOF, before killing it (external finding 3.5: an unbounded wait() here
# could hang the whole bridge process).
_EXEC_WAIT_TIMEOUT_S = 30.0


async def _iter_stream_lines(stream: asyncio.StreamReader) -> AsyncIterator[str]:
    """Yield decoded lines from a subprocess stream, tolerating over-limit lines.

    A line longer than the StreamReader limit makes ``readline()`` raise
    ``ValueError``; the buffered data survives, so continuing drains the
    oversized line in fragments instead of crashing the whole run (C1).
    """
    while True:
        try:
            raw = await stream.readline()
        except ValueError:
            logger.warning(
                "codex subprocess emitted a line longer than %d bytes; skipping it",
                _STREAM_LIMIT,
            )
            continue
        if not raw:
            return
        yield raw.decode("utf-8", errors="replace").rstrip()


# ── Module-level cleanup state ────────────────────────────────────────────────

_active_process: Optional[asyncio.subprocess.Process] = None
_active_proxy: Optional[_CodexProxy] = None


def _cleanup_sync() -> None:
    """Kill the active Codex process (and its process group). Called from
    atexit and SIGTERM handler.

    A bare proc.kill() only signals the codex-exec PID, orphaning any
    shell/tool subprocess codex itself spawned (finding 3) — the same class
    of bug the timeout handling below fixes. The process is spawned with
    start_new_session=True (POSIX) / CREATE_NEW_PROCESS_GROUP (Windows), so
    _kill_process_tree can reap the whole group here too.
    """
    proc = _active_process
    if proc is not None:
        try:
            _kill_process_tree(proc.pid)
        except Exception:
            pass
    # The aiohttp proxy lives in the same process — it shuts down with us.


atexit.register(_cleanup_sync)


def _install_sigterm_handler() -> None:
    def _handler(sig: int, _frame: object) -> None:
        _cleanup_sync()
        sys.exit(128 + sig)

    try:
        signal.signal(signal.SIGTERM, _handler)
    except (OSError, ValueError):
        pass  # SIGTERM not available on all platforms (e.g. Windows, nested loops)


_install_sigterm_handler()


# ── Config loading ─────────────────────────────────────────────────────────────


def _load_config(path: Optional[str]) -> dict:
    """Load config.yaml the same way the rest of the orchestrator does.

    Thin wrapper kept for this module's existing internal call site — the
    actual loading (default-path resolution, malformed/non-mapping
    degrade-to-{}) lives in the shared ``orchestrator.agent_config.load_config``
    helper, which ``codex_engine/standalone_proxy.py`` also calls (via a
    runtime import, to keep codex_engine free of a module-level orchestrator
    dependency) so both Codex bridge entry points read config identically.
    """
    return _shared_load_config(path)


# ── CLI ────────────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run a Codex task via Kim's browser provider.")
    p.add_argument("--task", required=True, help="Coding task for Codex.")
    p.add_argument("--cwd", required=True, help="Working directory for Codex.")
    p.add_argument(
        "--provider",
        default=os.environ.get("KIM_PROVIDER", "browser"),
        help="Browser provider, e.g. 'browser:gemini'.",
    )
    p.add_argument("--model", default=None, help="Model name to pass to Codex.")
    p.add_argument("--config", default=None, help="Path to config.yaml.")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


# ── Main async logic ──────────────────────────────────────────────────────────


def _status(message: str) -> None:
    """Print a typed status line to stdout for the Rust IPC parser."""
    emit_status(message)


def _sandbox_fingerprint() -> str:
    """Identify the permission level baked into this run's codex instructions.

    A stored browser thread was taught its abilities by the instructions sent
    when it started; if the user changes the sandbox (e.g. sets
    KIM_CODEX_BYPASS_SANDBOX=1), reusing that thread leaves the model believing
    its OLD permission level. Compared against thread_state["sandbox"] to force
    a fresh chat on change.
    """
    bypass = os.environ.get("KIM_CODEX_BYPASS_SANDBOX", "").strip() == "1"
    return "bypass" if bypass else "default"


def _cli_session_changed(thread_state: dict, current_session: str) -> bool:
    """True when a stored thread is NOT owned by the current CLI session.

    Each `kim` launch gets a fresh session id, and a new session is a new chat:
    it must start with ZERO prior context. So any stored thread whose
    ``cli_session`` differs from the current id — including a sidecar with no
    ``cli_session`` at all (written by the desktop app or an older Kim) —
    counts as changed and is reset without compaction or handoff. Resuming an
    unowned thread is exactly the bug where a "fresh" chat resurrected an old
    conversation's context.

    The desktop app sets no ``KIM_CLI_SESSION_ID``; with no current session id
    this always returns False, so desktop Code-tab continuity is unaffected.
    """
    if not current_session:
        return False
    return (thread_state.get("cli_session") or "") != current_session


def _thread_sandbox_changed(thread_state: dict, current: str) -> bool:
    """True when a stored thread's instructions describe a different permission
    level than *current* (sidecars from before this field existed count as
    "default" — they were all created under the default read-only sandbox)."""
    if not thread_state.get("sent_instructions"):
        return False
    return (thread_state.get("sandbox") or "default") != current


def _is_git_repo(cwd: str) -> bool:
    """True if ``cwd`` (or any ancestor) is inside a git working tree.

    Mirrors the check Codex itself performs before ``codex exec``: it walks up
    looking for a ``.git`` marker (a directory in a normal clone, or a file in a
    git worktree/submodule).  Used to decide whether Codex needs the explicit
    ``--skip-git-repo-check`` opt-in.
    """
    try:
        start = Path(cwd).resolve()
    except Exception:  # noqa: BLE001
        return False
    for cur in (start, *start.parents):
        if (cur / ".git").exists():
            return True
    return False


# One owned stdin read at a time (C2). A timed-out approval used to orphan a
# run_in_executor(readline) thread that later STOLE the decision line meant
# for the next approval (silently cascading declines) and blocked interpreter
# shutdown. Instead: a single pending read is parked here on timeout and
# reused by the next prompt; a decision that arrives AFTER its prompt already
# timed out is discarded, never applied to a later approval.
_pending_stdin_read: Optional["concurrent.futures.Future[str]"] = None
# Guards the check-then-set on _pending_stdin_read (finding 7): without it,
# two concurrent callers could both observe "no pending read" and each start
# its own daemon reader thread on the same stdin, breaking the "one owned
# stdin read at a time" invariant the comment above already claims. Not
# currently reachable (single-pending-approval call graph) but this makes
# the invariant enforced in code rather than merely assumed.
_pending_stdin_read_lock = asyncio.Lock()


async def _begin_stdin_read() -> "concurrent.futures.Future[str]":
    """Return the future for the single in-flight stdin readline.

    Reuses a still-pending read left behind by a timed-out prompt (so the
    reader never doubles up on stdin); discards an already-completed one (a
    late decision belonging to the previous, timed-out approval).
    """
    global _pending_stdin_read  # noqa: PLW0603
    async with _pending_stdin_read_lock:
        fut = _pending_stdin_read
        if fut is not None and fut.done():
            logger.warning("Discarding stale HITL decision from a timed-out prompt")
            _pending_stdin_read = None
            fut = None
        if fut is None:
            fut = concurrent.futures.Future()

            def _read(target: "concurrent.futures.Future[str]" = fut) -> None:
                try:
                    line = sys.stdin.readline()
                except Exception as exc:  # noqa: BLE001
                    if not target.cancelled():
                        target.set_exception(exc)
                    return
                if not target.cancelled():
                    target.set_result(line)

            # Daemon thread: a read that never completes must not block exit.
            threading.Thread(target=_read, name="kim-hitl-stdin", daemon=True).start()
            _pending_stdin_read = fut
        return fut


async def _await_hitl_decision(timeout: float = 120.0, *, request_id: Optional[str] = None) -> bool:
    """Block on stdin for an approval decision line from the Rust supervisor.

    The supervisor reads our stdout, shows a confirmation dialog, then writes
    an approval decision to our stdin — the legacy ``{"approved": bool}`` /
    ``{"type": "hitl_approve", "approved": bool}`` shapes or the newer
    ``{"type": "approval_decision", "decision": …}`` shape. Returns True only
    on an explicit approval; any error/timeout/EOF denies.

    Finding 2: the same stdin also carries mid-run *steer* notes (and other
    non-decision lines). Those must NOT be read as an approval — the old code
    did ``bool(data.get("approved", False))``, so a steer line (no ``approved``
    key) parsed as ``False`` and silently DENIED the pending approval. We now
    route each line through ``parse_decision_line``: a real accept/decline is
    honored, while a steer / user-note / unparseable line is skipped and we
    keep waiting for the actual decision (within the remaining budget).

    Finding 5: when *request_id* is given, a decision line whose echoed id
    doesn't match is discarded (kept waiting) instead of being applied — the
    same T1 contract codex_appserver_transport.py's ``_collect_decision``
    uses, so a late decision for an earlier, already-timed-out prompt can
    never authorize a different pending request. An id-less decision line
    (``echo_id is None``, e.g. a legacy writer) is still accepted, matching
    the transport's own backward-compatibility rule. When *request_id* is
    None (caller opted out), matching is skipped entirely — today's only
    callers always pass one, so this only matters for future callers.
    """
    global _pending_stdin_read  # noqa: PLW0603
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            logger.warning("HITL stdin read timed out — denying")
            return False
        fut = await _begin_stdin_read()
        try:
            # shield: a timeout must not cancel the underlying read — the parked
            # future is what lets the NEXT prompt pick the stream back up (C2).
            line: str = await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(fut)), timeout=remaining
            )
        except asyncio.TimeoutError:
            logger.warning("HITL stdin read timed out — denying")
            return False
        except Exception as exc:  # noqa: BLE001
            _pending_stdin_read = None
            logger.warning("HITL stdin read failed (%s) — denying", exc)
            return False
        _pending_stdin_read = None
        if not line:
            # EOF — the supervisor closed the pipe/went away; deny.
            logger.warning("HITL stdin closed before a decision — denying")
            return False
        decision = parse_decision_line(line)
        if decision is None:
            # A steer note / user message / unparseable line — not an approval
            # decision. Do not misread it as a denial (Finding 2); keep waiting.
            logger.debug(
                "Ignoring non-decision stdin line during approval wait: %r",
                line.strip()[:120],
            )
            continue
        decision_value, echo_id = decision
        if request_id is not None and echo_id is not None and echo_id != request_id:
            logger.warning(
                "discarding HITL decision for id %r — it does not match the "
                "pending request %r", echo_id, request_id,
            )
            continue
        return decision_value in ("accept", "acceptForSession")


async def _request_hitl_approval(task: str) -> bool:
    """Emit a HITL approval request event and block on stdin for the decision (#2).

    Returns True if approved, False if denied or timed out.
    """
    request_id = secrets.token_hex(8)
    emit_hitl_approval_request(
        "codex_bridge",
        "high",
        "Codex can execute arbitrary shell commands in your project directory.",
        task[:200],
        id=request_id,
    )
    return await _await_hitl_decision(request_id=request_id)


async def _request_git_check_approval(cwd: str) -> bool:
    """Ask the user to confirm running Codex in a non-git directory.

    Codex normally refuses to run outside a git repository so its edits stay
    trackable/undoable.  When the user opts in we pass ``--skip-git-repo-check``;
    this gate makes that opt-in explicit rather than silent.
    """
    request_id = secrets.token_hex(8)
    emit_hitl_approval_request(
        "codex_bridge_git_check",
        "high",
        "This folder is not a git repository, so Codex cannot track or undo its "
        "edits here. Run Codex in this directory anyway?",
        cwd[:200],
        id=request_id,
    )
    return await _await_hitl_decision(request_id=request_id)


async def _compact_browser_thread(provider, cwd: str, provider_name: str) -> tuple[bool, str]:
    """Ask the live code-mode browser thread to compact itself into a handoff.

    The thread already holds the conversation, so the compact request carries
    no transcript. The handoff (or nothing, on failure) is written to the
    thread-state sidecar; either way the next code task starts a fresh chat.
    Returns (summarized_ok, handoff_text).
    """
    handoff = ""
    try:
        # Delta-only send: don't re-inject the codex system prompt into the
        # thread we are about to retire.
        if hasattr(provider, "mark_thread_continuation"):
            provider.mark_thread_continuation()
        response = await provider.complete(
            messages=[{"role": "user", "content": build_in_thread_compact_prompt()}],
            tools=[],
            system=_codex_browser_system_prompt(),
        )
        raw = str(response.get("content", "")).strip() if isinstance(response, dict) else ""
        if raw and not raw.upper().startswith("NEED_HELP"):
            artifact = _parse_compact_json(raw)
            handoff = render_handoff_text(artifact).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("In-thread compact failed: %s", exc)

    state = reset_thread_state(cwd, provider_name, handoff=handoff or None)
    _stamp_thread_owner(cwd, provider_name, state)
    # Rollover now, while handling /compact. Previously the old chat remained
    # visibly active and the fresh chat was deferred until the next user task.
    # Keep the handoff pending; the next real task seeds it into this blank chat.
    if hasattr(provider, "start_fresh_chat"):
        opened = await provider.start_fresh_chat()
        if not opened:
            logger.warning("Compaction handoff saved, but fresh browser chat could not be opened")
    return bool(handoff), handoff


def _stamp_thread_owner(cwd: str, provider_name: str, state: dict) -> dict:
    """Record the owning CLI session + sandbox fingerprint on a sidecar.

    A reset sidecar with NO owner would be treated as a foreign thread by
    ``_cli_session_changed`` on the very next task, silently dropping a
    handoff the user just armed with /compact in this same session. Stamping
    ownership keeps same-session continuity while a NEW session (different or
    missing id) still starts with zero context.
    """
    state["sandbox"] = _sandbox_fingerprint()
    session = os.environ.get("KIM_CLI_SESSION_ID", "").strip()
    if session:
        state["cli_session"] = session
    save_thread_state(cwd, provider_name, state)
    return state


async def _run_compact_task(args: argparse.Namespace, config: dict) -> int:
    """Handle a /compact control task: compact the thread, arm a fresh chat, exit."""
    _status("Compacting the code-mode browser thread…")
    provider = create_provider(args.provider, config)
    ok, handoff = await _compact_browser_thread(provider, args.cwd, args.provider)
    # App-server transport: ALSO compact codex's own transcript (two
    # independent context budgets — parity Part 2.4). Best-effort.
    if transport_name(config) == "app-server":
        binary = os.environ.get("CODEX_BIN", "").strip() or CODEX_BINARY
        binary_path = shutil.which(binary) if not os.path.isabs(binary) else binary
        if binary_path and os.path.exists(binary_path):
            state_after = load_thread_state(args.cwd, args.provider)
            if await compact_codex_thread(
                cwd=args.cwd,
                config=config,
                thread_state=state_after,
                binary_path=str(binary_path),
            ):
                _status("Codex transcript compacted natively as well.")
    if ok:
        _status("Context compacted — the next code task starts a fresh chat seeded with the handoff.")
        # Temporary verification aid: with KIM_DEBUG_COMPACT=1 set, print the
        # model-written handoff so you can confirm it actually summarized the
        # conversation (not just reported success).
        if os.environ.get("KIM_DEBUG_COMPACT") == "1" and handoff:
            print("───── COMPACTED HANDOFF (KIM_DEBUG_COMPACT) ─────", flush=True)
            print(handoff, flush=True)
            print(f"───── END HANDOFF ({len(handoff)} chars) ─────", flush=True)
        print(
            f"{LOG_TAG_TASK_COMPLETE} Context compacted. The next code task will continue "
            "from the handoff in a fresh browser chat.",
            flush=True,
        )
    else:
        _status("Could not summarize the current thread — the next code task starts fresh.")
        print(
            f"{LOG_TAG_TASK_COMPLETE} Thread reset. No handoff could be generated, so the "
            "next code task starts a fresh browser chat without prior context.",
            flush=True,
        )
    return 0


async def _run_async(args: argparse.Namespace) -> int:
    global _active_process, _active_proxy  # noqa: PLW0603

    if not args.provider.lower().startswith("browser"):
        print(
            f"{LOG_TAG_ERROR} codex_bridge_service requires a browser provider; got {args.provider!r}.",
            file=sys.stderr,
        )
        return 2

    config = _load_config(args.config)
    config["provider"] = args.provider
    os.environ["PROJECT_ROOT"] = args.cwd

    # WARNING: KIM_CODEX_BYPASS_SANDBOX=1 enables full RCE with NO sandbox and NO approvals.
    # This must only be run in containerized or disposable environments.
    bypass = os.environ.get("KIM_CODEX_BYPASS_SANDBOX", "").strip() == "1"
    if bypass:
        print(
            "WARNING: KIM_CODEX_BYPASS_SANDBOX=1 is active. This runs codex-exec with "
            "full RCE capabilities and disables all approvals. Ensure this is running "
            "in a containerized/disposable workspace.",
            file=sys.stderr,
            flush=True,
        )
        # Refuse to combine bypass with browser-provider tasks from scraped web content
        is_web_derived = any(
            k in args.task.lower()
            for k in ("http", "www", "url", "scrape", "fetch", "web", "download", "scraped")
        )
        if is_web_derived:
            print(
                f"{LOG_TAG_ERROR} Refusing to run in bypass sandbox mode: task string contains "
                "web keywords or URLs which pose prompt-injection RCE risks.",
                file=sys.stderr,
                flush=True,
            )
            return 1

    # /compact control task: compact the browser thread instead of running
    # Codex. No HITL gate — nothing executes in the user's project.
    if args.task.strip().lower() in _COMPACT_CONTROL_TASKS:
        return await _run_compact_task(args, config)

    # Gate: when running under Tauri, require explicit HITL approval before
    # spawning Codex.  Codex can run arbitrary shell commands, so this is a
    # HIGH-risk operation that must never silently auto-execute (#2).
    if os.environ.get("KIM_TAURI_MODE") == "1":
        approved = await _request_hitl_approval(args.task)
        if not approved:
            _status("Codex launch denied by user. No code was executed.")
            print(f"{LOG_TAG_FAILED} Codex launch denied by user.", flush=True)
            return 1

    # Gate: Codex refuses to run outside a git repo (so its edits stay
    # trackable) unless --skip-git-repo-check is passed.  Rather than fail hard,
    # let the user opt in explicitly.  The CLI does its own terminal y/N prompt
    # and signals approval via KIM_CODEX_SKIP_GIT_CHECK=1; the desktop uses the
    # stdin HITL dialog.  Either way the opt-in is never silent.
    skip_git_check = False
    if not _is_git_repo(args.cwd):
        if os.environ.get("KIM_CODEX_SKIP_GIT_CHECK", "").strip() == "1":
            skip_git_check = True
        elif os.environ.get("KIM_TAURI_MODE") == "1":
            if await _request_git_check_approval(args.cwd):
                skip_git_check = True
            else:
                _status("Codex launch declined — this folder is not a git repository.")
                print(
                    f"{LOG_TAG_FAILED} Not a git repository and running Codex here was declined.",
                    flush=True,
                )
                return 1
        else:
            # No interactive channel to confirm on — refuse rather than bypass
            # Codex's safety gate silently.
            _status("Not a git repository — Codex needs a git repo or explicit confirmation.")
            print(
                f"{LOG_TAG_FAILED} Not a git repository. Run Kim from inside a git repo, "
                "`git init` here, or confirm when prompted to run anyway.",
                flush=True,
            )
            return 1

    _status(f"✓ Using Codex via browser bridge ({args.provider})")

    provider = create_provider(args.provider, config)
    # Reset per-session state so the new task gets a fresh system prompt injection.
    if hasattr(provider, "_sent_system_prompt"):
        provider._sent_system_prompt = False  # type: ignore[attr-defined]

    # Resolve the Codex binary (Tauri may have set CODEX_BIN).
    binary = os.environ.get("CODEX_BIN", "").strip() or CODEX_BINARY
    binary_path = shutil.which(binary) if not os.path.isabs(binary) else binary
    if not binary_path or not os.path.exists(binary_path):
        print(f"{LOG_TAG_FAILED} Codex binary not found: {binary}.", flush=True)
        return 1

    _status(f"codex binary: {binary_path}")

    # ── Cross-task browser-thread state (stateful mode + handoff seeding) ──
    # The per-run auto-compaction inside the proxy only sees the CURRENT run's
    # items, so growth of a reused thread across tasks is checked here, before
    # the task starts.
    bp_cfg = config.get("browser_provider") or {}
    stateful = bool(bp_cfg.get("stateful_threads", False))
    thread_state = load_thread_state(args.cwd, args.provider)
    current_sandbox = _sandbox_fingerprint()
    current_session = os.environ.get("KIM_CLI_SESSION_ID", "").strip()
    if stateful and thread_state.get("sent_instructions"):
        if _cli_session_changed(thread_state, current_session):
            # A different CLI session owns this on-disk thread — the user quit
            # and reopened `kim` (a "new chat"). Resuming the old thread sends
            # only the delta with no fresh system prompt, so the model drifts
            # back to prose. Start a fresh browser chat for the new session.
            _status("New Kim session — starting a fresh browser chat…")
            # A new CLI launch is a genuinely new conversation on BOTH sides
            # of the bridge. Keeping codex_thread_id here resumed Codex's old
            # transcript, which could immediately compact and re-seed the new
            # browser chat with the conversation the user just left.
            thread_state = reset_thread_state(args.cwd, args.provider, preserve_codex_thread=False)
        elif thread_state.get("burned"):
            # The stored thread ignored the tool protocol even after a format
            # nudge — its context argues against compliance, so resuming it
            # only compounds the refusals. Drop it (no compact: a summary
            # written by a refusing thread carries the refusal with it).
            _status("Previous browser thread ignored the tool protocol — starting fresh…")
            thread_state = reset_thread_state(args.cwd, args.provider)
        elif _thread_sandbox_changed(thread_state, current_sandbox):
            # The stored browser thread was taught the OLD permission level
            # (e.g. read-only) and will keep refusing writes even after the
            # user grants full access — start a fresh chat so the new codex
            # instructions actually reach the model.
            _status("Sandbox permissions changed — starting a fresh browser chat…")
            await _compact_browser_thread(provider, args.cwd, args.provider)
            thread_state = load_thread_state(args.cwd, args.provider)
        else:
            threshold = _get_compact_threshold(args.provider)
            compact_at = float(bp_cfg.get("compact_at_ratio", 0.80))
            max_turns = int(bp_cfg.get("max_thread_turns", 40))
            est_tokens = int(thread_state.get("est_tokens") or 0)
            turns = int(thread_state.get("turns") or 0)
            if est_tokens >= int(threshold * compact_at) or turns >= max_turns:
                _status("Browser thread near its limit — compacting into a fresh chat before this task…")
                await _compact_browser_thread(provider, args.cwd, args.provider)
                thread_state = load_thread_state(args.cwd, args.provider)
    # Record the permission level + owning CLI session this thread's
    # instructions describe, so the next run can detect a change (persisted by
    # save_thread_state below).
    thread_state["sandbox"] = current_sandbox
    if current_session:
        thread_state["cli_session"] = current_session

    proxy = _CodexProxy(
        provider,
        provider_name=args.provider,
        thread_state=thread_state,
        stateful=stateful,
    )
    _active_proxy = proxy
    proxy_port = await proxy.start()

    logger.info("Proxy started on port %d", proxy_port)

    try:
        # ── App-server transport (parity Part 2 — the default) ─────────────
        # JSON-RPC codex with native per-command approvals, workspace-write
        # sandbox, and true session resume via the sidecar codex_thread_id.
        # `codex_bridge: transport: exec` selects the legacy per-message
        # `codex exec --json` spawn instead; a version-gate refusal (major
        # protocol drift) also degrades to exec for this run.
        if transport_name(config) == "app-server":
            def _register(proc: object) -> None:
                global _active_process  # noqa: PLW0603
                _active_process = proc  # type: ignore[assignment]

            rc = await run_app_server_task(
                task=args.task,
                cwd=args.cwd,
                model=args.model,
                config=config,
                proxy=proxy,
                thread_state=thread_state,
                binary_path=str(binary_path),
                register_process=_register,
                version_gate_fallback=True,
            )
            _active_process = None
            if rc is not None:
                return rc
            _status(
                "This codex binary does not match Kim's app-server protocol "
                "snapshot — using the legacy exec transport for this run."
            )
        return await _run_exec_task(
            args, config, str(binary_path), skip_git_check, proxy, proxy_port
        )
    finally:
        _active_process = None
        await proxy.stop()
        _active_proxy = None
        # Persist whatever the run left behind (turns, token estimate,
        # sent_instructions, remaining handoff) for the next code task.
        save_thread_state(args.cwd, args.provider, thread_state)


def _exec_config_overrides(proxy_port: int, model: Optional[str]) -> list[str]:
    """``-c`` CLI overrides routing codex at Kim's local proxy (C3).

    The exec path used to point CODEX_HOME at a throwaway temp dir holding a
    generated config.toml, which silently dropped the user's real
    ``~/.codex`` config (MCP servers, skills, custom instructions) and
    destroyed rollout files. Instead we now leave CODEX_HOME alone — the
    user's real codex home applies — and layer only the kim-proxy provider
    routing on top via CLI config overrides (same keys as
    ``_write_codex_config`` / the app-server transport's inline config).
    """
    safe_model = None
    if model:
        import re as _re
        safe_model = _re.sub(r"[^A-Za-z0-9_\-.:/ ]", "", model)[:128] or None
    pairs = [
        ("model_provider", "kim-proxy"),
        ("model", safe_model or "kim-proxy-model"),
        ("model_providers.kim-proxy.name", "Kim Proxy"),
        ("model_providers.kim-proxy.base_url", f"http://127.0.0.1:{proxy_port}/v1"),
        ("model_providers.kim-proxy.wire_api", "responses"),
        ("model_providers.kim-proxy.env_key", "CODEX_API_KEY"),
    ]
    argv: list[str] = []
    for key, value in pairs:
        argv += ["-c", f'{key}="{value}"']  # quoted → parsed as a TOML string
    return argv


async def _run_exec_task(
    args: argparse.Namespace,
    config: dict,
    binary_path: str,
    skip_git_check: bool,
    proxy: _CodexProxy,
    proxy_port: int,
) -> int:
    """Legacy exec transport: one ``codex exec --json`` spawn for this message.

    Kept as the explicit/config fallback behind the app-server default; the
    caller owns proxy shutdown and thread-state persistence.
    """
    global _active_process  # noqa: PLW0603

    # Minimal env — do not inherit full os.environ (parent secrets) (#1).
    # NO CODEX_HOME override (C3): the user's real codex home (~/.codex or an
    # explicitly exported CODEX_HOME) applies, so their config/MCP/skills are
    # honored and rollout files persist.
    path_value = os.environ.get("PATH") or os.environ.get("Path", "")
    home_value = os.environ.get("HOME") or os.environ.get("USERPROFILE", "")
    user_value = os.environ.get("USER") or os.environ.get("USERNAME", "")
    tmp_value = (
        os.environ.get("TMPDIR")
        or os.environ.get("TEMP")
        or os.environ.get("TMP", "")
    )
    env = {
        "PATH": path_value,
        "HOME": home_value,
        "USER": user_value,
        "TMPDIR": tmp_value,
        "LANG": os.environ.get("LANG", ""),
        # Per-run bearer token generated by the proxy (#47).
        "CODEX_API_KEY": proxy._bearer_token,
        "OPENAI_API_KEY": proxy._bearer_token,
        "OPENAI_BASE_URL": f"http://127.0.0.1:{proxy_port}/v1",
    }
    if os.environ.get("CODEX_HOME"):
        env["CODEX_HOME"] = os.environ["CODEX_HOME"]
    if "KIM_RUN_ID" in os.environ:
        env["KIM_RUN_ID"] = os.environ["KIM_RUN_ID"]
    if "KIM_SESSION_ID" in os.environ:
        env["KIM_SESSION_ID"] = os.environ["KIM_SESSION_ID"]
    # Windows normalizes environment keys to uppercase. Collect the required
    # runtime variables case-insensitively and use canonical child-side names.
    env = _with_windows_env_aliases(
        env, os.environ, include_runtime=sys.platform == "win32"
    )
    # --dangerously-bypass-approvals-and-sandbox requires explicit opt-in (#1).
    bypass_flag = os.environ.get("KIM_CODEX_BYPASS_SANDBOX", "").strip()
    cmd = [
        binary_path,
        "exec",
        "--json",
        *_exec_config_overrides(proxy_port, args.model),
    ]
    if bypass_flag == "1":
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    if skip_git_check:
        cmd.append("--skip-git-repo-check")
    cmd += ["-C", args.cwd, args.task]

    # Spawn as a session/process-group leader so a timeout or cancellation can
    # kill codex AND every shell/tool subprocess it spawned, not just the
    # single codex-exec PID (finding 3). Mirrors scheduled_runner.py's own
    # spawn — POSIX gets start_new_session (pgid == pid, killed via
    # os.killpg in _kill_process_tree); Windows gets a new process group
    # (killed via `taskkill /T`).
    _group_kwargs: dict = {}
    if sys.platform == "win32":
        _group_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        _group_kwargs["start_new_session"] = True

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LIMIT,
            **_group_kwargs,
        )
        _active_process = process

        async def _stream_stdout() -> None:
            assert process.stdout
            async for line in _iter_stream_lines(process.stdout):
                if line:
                    print(line, flush=True)

        async def _drain_stderr() -> None:
            assert process.stderr
            async for line in _iter_stream_lines(process.stderr):
                if line:
                    logger.debug("codex stderr: %s", line)
                    # Surface codex subprocess errors to the Kim activity
                    # feed — but not benign chatter like the "Reading
                    # additional input from stdin..." notice codex prints
                    # because our stdin is /dev/null.
                    if not _is_benign_codex_stderr(line):
                        _status(f"codex error: {line}")

        # Whole-run budget. Browser relays are slow (a single one may
        # legitimately take minutes of typing + generation), and one
        # Codex task spans many relays — a budget at or below the
        # per-relay browser waits (600s each in site_configs) kills
        # healthy long tasks. Default 1800s; config codex_bridge.task_timeout_s.
        try:
            task_timeout = int(
                (config.get("codex_bridge") or {}).get("task_timeout_s", 1800)
            )
        except (TypeError, ValueError):
            task_timeout = 1800
        try:
            await asyncio.wait_for(
                asyncio.gather(_stream_stdout(), _drain_stderr()),
                timeout=task_timeout,
            )
        except asyncio.TimeoutError:
            logger.error("Codex subprocess timed out after %ds", task_timeout)
            try:
                # Kill the whole process group, not just codex-exec itself —
                # a bare process.kill() orphaned any shell/tool subprocess
                # codex had spawned (finding 3).
                _kill_process_tree(process.pid)
                await asyncio.wait_for(process.wait(), timeout=5)
            except Exception:
                pass
            print(
                f"{LOG_TAG_FAILED} Codex task timed out after {task_timeout // 60} minutes.",
                flush=True,
            )
            return 1

        # Both pipes hit EOF, so the child is exiting — but wait() itself can
        # still hang (e.g. a stuck reaper); bound it instead of trusting it
        # (external finding 3.5).
        try:
            exit_code = await asyncio.wait_for(process.wait(), timeout=_EXEC_WAIT_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.error("Codex subprocess did not exit after its pipes closed — killing it")
            try:
                # Same process-group kill as above (finding 3).
                _kill_process_tree(process.pid)
                exit_code = await asyncio.wait_for(process.wait(), timeout=5)
            except Exception:
                print(
                    f"{LOG_TAG_FAILED} Codex did not exit after finishing its output.",
                    flush=True,
                )
                return 1
        _active_process = None

        if exit_code != 0:
            print(f"{LOG_TAG_FAILED} Codex exited with code {exit_code}.", flush=True)

        return exit_code

    except Exception as e:
        logger.exception("Codex bridge service crashed")
        print(f"{LOG_TAG_FAILED} Codex bridge error: {e}", flush=True)
        return 1
    finally:
        _active_process = None


# ── Entry point ────────────────────────────────────────────────────────────────


def _emit_terminal_for_rc(rc: int) -> None:
    """Emit a typed run-lifecycle terminal event for a codex-bridge exit code.

    F-H-2 / F-H-1: the Code-tab (codex browser-bridge) path historically
    signalled termination ONLY via legacy magic-string lines (``TASK_COMPLETE:``
    / ``[FAILED]``), so — unlike a chat run, which emits typed ``kim:run-done``
    from ``cli.py`` — the frontend had no typed, run-attributable signal to
    clear ``isRunning`` on. That divergence is the root cause of the stuck
    spinner (F-F-5) and the event-bleed on a session switch (F-F-2). We now
    emit ``kim:run-done`` on EVERY codex-bridge exit path (mapped from the
    return code), mirroring the chat contract. The event self-stamps the
    run-identity envelope from ``KIM_RUN_ID`` / ``KIM_SESSION_ID`` when the
    spawner exports them (F-H-8 — handoff to D' for ``codex_browser_spec``).
    The legacy lines are kept for back-compat (codex CLI text protocol).
    """
    if rc == 0:
        termination, success = "task_complete", True
    elif rc == 130:
        termination, success = "cancelled", False
    else:
        termination, success = "failed", False
    try:
        emit_run_done(termination, success)
        if success:
            emit_agent_done(True)
        elif termination == "cancelled":
            emit_agent_cancelled(False)
        else:
            emit_agent_error(f"Codex bridge exited with code {rc}")
            emit_agent_done(False)
    except Exception:  # noqa: BLE001 — a terminal event must never mask the real rc
        pass


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        rc = asyncio.run(_run_async(args))
    except KeyboardInterrupt:
        rc = 130
    # F-H-2: typed terminal lifecycle event on the SAME channel as chat runs.
    _emit_terminal_for_rc(rc)
    sys.exit(rc)


if __name__ == "__main__":
    main()

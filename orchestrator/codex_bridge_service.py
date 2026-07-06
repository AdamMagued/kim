"""
Codex bridge service — lifecycle-managed launcher for browser-backed Codex runs.

Merges ``orchestrator/run_codex_bridge.py`` and the subprocess-launch logic from
``codex_engine/engine.py`` into a single module with:

- ``atexit`` handler: kills the Codex process on any exit path
- ``signal.SIGTERM`` handler: same cleanup (Tauri sends SIGTERM on cancel)
- ``tempfile.TemporaryDirectory`` context manager: automatic config cleanup
- Codex stderr forwarded to stdout as ``[STATUS] codex error: {line}``

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
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path
from typing import Optional

from orchestrator.events_gen import (
    LOG_TAG_ERROR,
    LOG_TAG_FAILED,
    LOG_TAG_TASK_COMPLETE,
    emit_hitl_approval_request,
    emit_status,
)

from codex_engine.engine import (
    CODEX_BINARY,
    _CodexProxy,
    _codex_browser_system_prompt,
    _get_compact_threshold,
    _write_codex_config,
)
from codex_engine.thread_state import (
    load_thread_state,
    reset_thread_state,
    save_thread_state,
)
from orchestrator.codex_appserver_transport import (
    compact_codex_thread,
    run_app_server_task,
    transport_name,
)
from orchestrator.compact_prompt import (
    _parse_compact_json,
    build_in_thread_compact_prompt,
    render_handoff_text,
)
from orchestrator.providers.base import create_provider

# Control tasks that compact the code-mode browser thread instead of running
# Codex. Mirrors _COMPACT_CONTROL_TASKS in orchestrator/agent.py so /compact
# behaves the same in the Code tab and CLI code mode as in normal chat.
_COMPACT_CONTROL_TASKS = {"/compact", "compact", "__kim_compact_context__"}

# Repo root — used to locate the default ``config.yaml`` when ``--config`` is
# omitted (see ``_load_config`` below). Import resolution for ``codex_engine.*``,
# ``orchestrator.*`` and ``mcp_server.*`` comes from ``PYTHONPATH=kim_root`` set by
# the Tauri launcher (``subprocess.rs``), so no ``sys.path`` manipulation is needed.
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent

# Named constant for the placeholder API key used in local proxy auth (#53).
# The proxy validates a per-run cryptographically random bearer token; this
# value is only a human-readable label, not a real secret.
_LOCAL_PROXY_KEY = "kim-proxy-key"

logger = logging.getLogger("kim.codex_bridge_service")

# ── Module-level cleanup state ────────────────────────────────────────────────

_active_process: Optional[asyncio.subprocess.Process] = None
_active_proxy: Optional[_CodexProxy] = None


def _cleanup_sync() -> None:
    """Kill the active Codex process. Called from atexit and SIGTERM handler."""
    proc = _active_process
    if proc is not None:
        try:
            proc.kill()
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
    cfg_path = Path(path) if path else _REPO / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        return {}
    try:
        loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        return loaded if isinstance(loaded, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


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
    """True when a stored thread belongs to a DIFFERENT CLI session than the
    current one (user quit and reopened kim). No current session id, no stored
    session id, or a matching id all mean "not a new session" — so legacy
    sidecars and the desktop app (which sets no id) never falsely reset."""
    if not current_session:
        return False
    stored = thread_state.get("cli_session")
    return bool(stored) and stored != current_session


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


async def _await_hitl_decision(timeout: float = 120.0) -> bool:
    """Block on stdin for a {"approved": bool} line from the Rust supervisor.

    The supervisor reads our stdout, shows a confirmation dialog, then writes
    {"type": "hitl_approve", "approved": bool} to our stdin.  Returns True only
    on an explicit approval; any error/timeout denies.
    """
    import asyncio

    loop = asyncio.get_running_loop()
    try:
        line: str = await asyncio.wait_for(
            loop.run_in_executor(None, sys.stdin.readline),
            timeout=timeout,
        )
        data = json.loads(line.strip())
        return bool(data.get("approved", False))
    except Exception as exc:  # noqa: BLE001
        logger.warning("HITL stdin read failed (%s) — denying", exc)
        return False


async def _request_hitl_approval(task: str) -> bool:
    """Emit a HITL approval request event and block on stdin for the decision (#2).

    Returns True if approved, False if denied or timed out.
    """
    emit_hitl_approval_request(
        "codex_bridge",
        "high",
        "Codex can execute arbitrary shell commands in your project directory.",
        task[:200],
    )
    return await _await_hitl_decision()


async def _request_git_check_approval(cwd: str) -> bool:
    """Ask the user to confirm running Codex in a non-git directory.

    Codex normally refuses to run outside a git repository so its edits stay
    trackable/undoable.  When the user opts in we pass ``--skip-git-repo-check``;
    this gate makes that opt-in explicit rather than silent.
    """
    emit_hitl_approval_request(
        "codex_bridge_git_check",
        "high",
        "This folder is not a git repository, so Codex cannot track or undo its "
        "edits here. Run Codex in this directory anyway?",
        cwd[:200],
    )
    return await _await_hitl_decision()


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

    reset_thread_state(cwd, provider_name, handoff=handoff or None)
    return bool(handoff), handoff


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
            thread_state = reset_thread_state(args.cwd, args.provider)
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

    # ── App-server transport (parity Part 2) ──────────────────────────────
    # Behind `codex_bridge: transport: app-server`: JSON-RPC codex with native
    # per-command approvals, workspace-write sandbox, and true session resume
    # via the sidecar codex_thread_id. The exec path below stays the default.
    if transport_name(config) == "app-server":
        def _register(proc: object) -> None:
            global _active_process  # noqa: PLW0603
            _active_process = proc  # type: ignore[assignment]

        try:
            return await run_app_server_task(
                task=args.task,
                cwd=args.cwd,
                model=args.model,
                config=config,
                proxy=proxy,
                thread_state=thread_state,
                binary_path=str(binary_path),
                register_process=_register,
            )
        finally:
            _active_process = None
            await proxy.stop()
            _active_proxy = None
            save_thread_state(args.cwd, args.provider, thread_state)

    try:
        with tempfile.TemporaryDirectory(prefix="kim-codex-config-") as config_dir:
            config_file = Path(config_dir) / "config.toml"
            _write_codex_config(config_file, proxy_port, args.model)

            # Minimal env — do not inherit full os.environ (parent secrets) (#1).
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", ""),
                "USER": os.environ.get("USER", ""),
                "TMPDIR": os.environ.get("TMPDIR", ""),
                "LANG": os.environ.get("LANG", ""),
                "CODEX_HOME": config_dir,
                # Per-run bearer token generated by the proxy (#47).
                "CODEX_API_KEY": proxy._bearer_token,
                "OPENAI_API_KEY": proxy._bearer_token,
                "OPENAI_BASE_URL": f"http://127.0.0.1:{proxy_port}/v1",
            }
            # On Windows the POSIX vars above are absent; forward the essentials
            # so the codex child process can locate system tools and temp storage.
            if sys.platform == "win32":
                _WIN_PASSTHROUGH = (
                    "SystemRoot",
                    "ComSpec",
                    "USERPROFILE",
                    "TEMP",
                    "TMP",
                    # Windows uses "Path" (mixed-case) in addition to "PATH".
                    "Path",
                )
                for _var in _WIN_PASSTHROUGH:
                    if _var in os.environ:
                        env[_var] = os.environ[_var]
            # --dangerously-bypass-approvals-and-sandbox requires explicit opt-in (#1).
            bypass_flag = os.environ.get("KIM_CODEX_BYPASS_SANDBOX", "").strip()
            cmd = [
                str(binary_path),
                "exec",
                "--json",
            ]
            if bypass_flag == "1":
                cmd.append("--dangerously-bypass-approvals-and-sandbox")
            if skip_git_check:
                cmd.append("--skip-git-repo-check")
            cmd += ["-C", args.cwd, args.task]

            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _active_process = process

                async def _stream_stdout() -> None:
                    assert process.stdout
                    async for raw in process.stdout:
                        line = raw.decode("utf-8", errors="replace").rstrip()
                        if line:
                            print(line, flush=True)

                async def _drain_stderr() -> None:
                    assert process.stderr
                    async for raw in process.stderr:
                        line = raw.decode("utf-8", errors="replace").rstrip()
                        if line:
                            logger.debug("codex stderr: %s", line)
                            # Surface codex subprocess errors to the Kim activity feed.
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
                        process.kill()
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except Exception:
                        pass
                    print(
                        f"{LOG_TAG_FAILED} Codex task timed out after {task_timeout // 60} minutes.",
                        flush=True,
                    )
                    return 1

                exit_code = await process.wait()
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

    finally:
        await proxy.stop()
        _active_proxy = None
        # Persist whatever the run left behind (turns, token estimate,
        # sent_instructions, remaining handoff) for the next code task.
        save_thread_state(args.cwd, args.provider, thread_state)


# ── Entry point ────────────────────────────────────────────────────────────────


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
    sys.exit(rc)


if __name__ == "__main__":
    main()

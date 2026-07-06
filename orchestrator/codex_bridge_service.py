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

from orchestrator.events_gen import emit_hitl_approval_request, emit_status

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


async def _request_hitl_approval(task: str) -> bool:
    """Emit a HITL approval request event and block on stdin for the user's decision (#2).

    The Rust supervisor reads our stdout, sees the event, shows a confirmation
    dialog in the UI, then writes {"type": "hitl_approve", "approved": bool} to
    our stdin.  We block here (with a 120 s timeout) until that decision arrives.

    Returns True if approved, False if denied or timed out.
    """
    import asyncio

    emit_hitl_approval_request(
        "codex_bridge",
        "high",
        "Codex can execute arbitrary shell commands in your project directory.",
        task[:200],
    )

    loop = asyncio.get_running_loop()
    try:
        line: str = await asyncio.wait_for(
            loop.run_in_executor(None, sys.stdin.readline),
            timeout=120.0,
        )
        data = json.loads(line.strip())
        return bool(data.get("approved", False))
    except Exception as exc:  # noqa: BLE001
        logger.warning("HITL stdin read failed (%s) — denying Codex launch", exc)
        return False


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
    ok, _handoff = await _compact_browser_thread(provider, args.cwd, args.provider)
    if ok:
        _status("Context compacted — the next code task starts a fresh chat seeded with the handoff.")
        print(
            "TASK_COMPLETE: Context compacted. The next code task will continue "
            "from the handoff in a fresh browser chat.",
            flush=True,
        )
    else:
        _status("Could not summarize the current thread — the next code task starts fresh.")
        print(
            "TASK_COMPLETE: Thread reset. No handoff could be generated, so the "
            "next code task starts a fresh browser chat without prior context.",
            flush=True,
        )
    return 0


async def _run_async(args: argparse.Namespace) -> int:
    global _active_process, _active_proxy  # noqa: PLW0603

    if not args.provider.lower().startswith("browser"):
        print(
            f"[ERROR] codex_bridge_service requires a browser provider; got {args.provider!r}.",
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
            print("[FAILED] Codex launch denied by user.", flush=True)
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
        print(f"[FAILED] Codex binary not found: {binary}.", flush=True)
        return 1

    _status(f"codex binary: {binary_path}")

    # ── Cross-task browser-thread state (stateful mode + handoff seeding) ──
    # The per-run auto-compaction inside the proxy only sees the CURRENT run's
    # items, so growth of a reused thread across tasks is checked here, before
    # the task starts.
    bp_cfg = config.get("browser_provider") or {}
    stateful = bool(bp_cfg.get("stateful_threads", False))
    thread_state = load_thread_state(args.cwd, args.provider)
    if stateful and thread_state.get("sent_instructions"):
        threshold = _get_compact_threshold(args.provider)
        compact_at = float(bp_cfg.get("compact_at_ratio", 0.80))
        max_turns = int(bp_cfg.get("max_thread_turns", 40))
        est_tokens = int(thread_state.get("est_tokens") or 0)
        turns = int(thread_state.get("turns") or 0)
        if est_tokens >= int(threshold * compact_at) or turns >= max_turns:
            _status("Browser thread near its limit — compacting into a fresh chat before this task…")
            await _compact_browser_thread(provider, args.cwd, args.provider)
            thread_state = load_thread_state(args.cwd, args.provider)

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

                try:
                    await asyncio.wait_for(
                        asyncio.gather(_stream_stdout(), _drain_stderr()),
                        timeout=600,
                    )
                except asyncio.TimeoutError:
                    logger.error("Codex subprocess timed out after 600s")
                    try:
                        process.kill()
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except Exception:
                        pass
                    print("[FAILED] Codex task timed out after 10 minutes.", flush=True)
                    return 1

                exit_code = await process.wait()
                _active_process = None

                if exit_code != 0:
                    print(f"[FAILED] Codex exited with code {exit_code}.", flush=True)

                return exit_code

            except Exception as e:
                logger.exception("Codex bridge service crashed")
                print(f"[FAILED] Codex bridge error: {e}", flush=True)
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

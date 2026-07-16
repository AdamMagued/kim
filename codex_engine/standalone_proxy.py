"""Standalone Codex bridge proxy — the Python side of kimcli.

Spawned directly by the Rust launcher (NOT by orchestrator/codex_bridge_service.py,
which is the desktop-app/exec-transport entry point): the launcher starts this
as a plain subprocess, reads ONE JSON handshake line from its stdout, then
spawns the kimcli binary pointed at ``http://127.0.0.1:<port>/v1`` with
``OPENAI_API_KEY=<token>``.

Usage:
    python -m codex_engine.standalone_proxy --provider claude
        [--config path/to/config.yaml]
        [--mode browser-contract|chat-passthrough]   (default: chat-passthrough)
        [--max-relays N]
        [--parent-pid PID]
        [--preflight]

Stdout contract (IMPORTANT — nothing else may ever reach stdout; it is the
one-shot handshake channel the Rust launcher parses):
    success: exactly one line  {"event": "ready", "port": <int>, "token": "<bearer>"}
    failure: exactly one line  {"event": "fatal", "message": "<...>"}, exit(1)
Everything else (logging, --preflight status) goes to stderr, mirroring
mcp_server/server.py's "stdout is reserved for protocol messages" convention.

Mode note: only ``chat-passthrough`` (the default) actually keeps this
contract. ``_CodexProxy``'s browser-contract code path (narration, the
compaction/salvage status lines) calls the module-level bare
``print(..., flush=True)`` in codex_engine/engine.py, which was written for
the desktop app's exec transport, where stdout IS the continuously-parsed IPC
channel. chat-passthrough mode never reaches that code (compaction is
skipped and /v1/responses 400s outright — see engine.py's module docstring
"Modes"), so it is the only mode safe to run under this stdout contract.
``--mode browser-contract`` is accepted for testing/parity but a real kimcli
launch should never select it.

Shutdown: SIGTERM/SIGINT triggers a clean aiohttp teardown and exit(0). When
--parent-pid is given, a background watchdog polls every 5s (override via
KIM_STANDALONE_PROXY_WATCHDOG_INTERVAL_S, tests only) and shuts down if that
process is gone, so a killed/crashed Rust launcher never leaves an orphaned
proxy running.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from typing import Optional

logger = logging.getLogger("kim.standalone_codex_proxy")

_WATCHDOG_INTERVAL_S = float(os.environ.get("KIM_STANDALONE_PROXY_WATCHDOG_INTERVAL_S", "5.0"))


def _configure_stderr_logging() -> None:
    """All logging goes to stderr — stdout is the one-shot handshake channel."""
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Standalone Codex bridge proxy for kimcli.")
    p.add_argument("--provider", required=True, help="Provider name, e.g. 'claude', 'browser:gemini', 'fake'.")
    p.add_argument("--config", default=None, help="Path to config.yaml (defaults to the repo's config.yaml).")
    p.add_argument(
        "--mode", default="chat-passthrough",
        choices=["browser-contract", "chat-passthrough"],
        help="See module docstring — only chat-passthrough keeps the stdout handshake contract.",
    )
    p.add_argument("--max-relays", type=int, default=None)
    p.add_argument("--parent-pid", type=int, default=None, help="Watchdog: exit if this pid disappears.")
    p.add_argument(
        "--preflight", action="store_true",
        help="Best-effort provider warm-up status printed to stderr, then continue serving.",
    )
    return p.parse_args(argv)


def _pid_alive(pid: int) -> bool:
    """Best-effort cross-platform liveness check for the watchdog.

    POSIX: os.kill(pid, 0) — no signal sent, just existence/permission check.
    Windows: os.kill(pid, 0) is not supported by CPython for arbitrary pids
    (raises OSError unconditionally), and psutil is NOT a project dependency
    (see requirements.txt) — so this uses a small ctypes OpenProcess check.
    If ctypes itself is unavailable, degrade gracefully: assume alive rather
    than risk spuriously killing a live proxy from a broken liveness check.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
            )
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
            return True
        except Exception:
            logger.warning("Parent-pid watchdog unavailable on this Windows host; disabling it.")
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    except OSError:
        return False
    return True


async def _watchdog(parent_pid: int, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        if not _pid_alive(parent_pid):
            logger.warning("Parent pid %d is gone — shutting down.", parent_pid)
            stop_event.set()
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=_WATCHDOG_INTERVAL_S)


def _run_preflight(provider: object) -> None:
    """Best-effort provider warm-up status, printed to STDERR only — never blocks serving.

    BrowserProvider has no dedicated public async warm-up hook today (its
    CDP/browser connect happens inline inside complete()); actually driving a
    full browser launch as a side effect of a CLI flag is heavier and
    riskier than a preflight check should be, so this is informational only.
    """
    name = type(provider).__name__
    if "Browser" in name:
        print(
            f"[preflight] {name}: browser provider — CDP/browser connect happens "
            "on the first real request (no dedicated warm-up hook today).",
            file=sys.stderr, flush=True,
        )
    else:
        print(f"[preflight] {name}: ready.", file=sys.stderr, flush=True)


def _print_ready(port: int, token: str) -> None:
    print(json.dumps({"event": "ready", "port": port, "token": token}), flush=True)


def _print_fatal(message: str) -> None:
    print(json.dumps({"event": "fatal", "message": message}), flush=True)


async def _async_main(args: argparse.Namespace) -> int:
    # Runtime imports: codex_engine avoids an orchestrator dependency at
    # module scope (see codex_engine/engine.py's module docstring); this
    # function is the one legitimate place in codex_engine that needs the
    # orchestrator provider factory + the shared config loader.
    from orchestrator.agent_config import load_config
    from orchestrator.providers.base import create_provider
    from codex_engine.engine import _CodexProxy

    config = load_config(args.config)

    try:
        provider = create_provider(args.provider, config)
    except Exception as e:
        _print_fatal(f"Could not create provider {args.provider!r}: {e}")
        return 1

    if args.preflight:
        _run_preflight(provider)

    proxy = _CodexProxy(
        provider,
        provider_name=args.provider,
        thread_state={},
        stateful=False,
        mode=args.mode,
        max_relays=args.max_relays,
    )

    try:
        port = await proxy.start()
    except Exception as e:
        _print_fatal(f"Could not start proxy server: {e}")
        return 1

    _print_ready(port, proxy._bearer_token)  # noqa: SLF001 — same-package access

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            # add_signal_handler is POSIX-only in asyncio. Windows falls back
            # to signal.signal, which covers Ctrl+C (SIGINT); Windows has no
            # real SIGTERM delivery, so a Windows parent must terminate this
            # process another way (TerminateProcess) — documented limitation.
            with contextlib.suppress(OSError, ValueError):
                signal.signal(sig, lambda *_a: _request_stop())

    watchdog_task = (
        asyncio.create_task(_watchdog(args.parent_pid, stop_event))
        if args.parent_pid else None
    )

    await stop_event.wait()

    if watchdog_task is not None:
        watchdog_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog_task

    await proxy.stop()
    return 0


def main(argv: Optional[list] = None) -> None:
    _configure_stderr_logging()
    args = _parse_args(argv)
    try:
        rc = asyncio.run(_async_main(args))
    except Exception as e:
        # Last-resort guard: an unhandled exception must never skip the
        # handshake contract entirely — emit a fatal line so the Rust
        # launcher's blocking readline() doesn't hang forever.
        _print_fatal(str(e))
        rc = 1
    sys.exit(rc)


if __name__ == "__main__":
    main()

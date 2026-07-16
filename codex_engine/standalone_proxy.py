"""Standalone Codex bridge proxy — the Python side of kimcli.

Spawned directly by the Rust launcher (NOT by orchestrator/codex_bridge_service.py,
which is the desktop-app/exec-transport entry point): the launcher starts this
as a plain subprocess, reads ONE JSON handshake line from its stdout, then
spawns the kimcli binary pointed at ``http://127.0.0.1:<port>/v1`` with
``OPENAI_API_KEY=<token>``.

Usage:
    python -m codex_engine.standalone_proxy --provider claude
        [--config path/to/config.yaml]
        [--mode auto|browser-contract|responses-passthrough|chat-passthrough]
            (default: auto)
        [--max-relays N]
        [--parent-pid PID]
        [--preflight]

Stdout contract (IMPORTANT — nothing else may ever reach stdout; it is the
one-shot handshake channel the Rust launcher parses):
    success: exactly one line  {"event": "ready", "port": <int>, "token": "<bearer>"}
    failure: exactly one line  {"event": "fatal", "message": "<...>"}, exit(1)
Immediately after the ready line is flushed, stdout's underlying OS file
descriptor is dup2'd onto stderr's (see _async_main) — every later bare
``print()`` anywhere in this process (engine.py's browser-contract
narration/compaction/salvage lines, this module's own --preflight status,
third-party libraries) is transparently redirected to stderr for the rest of
the process's life, so it can never corrupt the one-shot handshake channel
or deadlock a launcher that stops draining stdout after its first read. Only
the fatal path (which always runs before that redirect) writes to the real
stdout.

Mode ("auto", the default): ``browser:*`` (and bare ``"browser"``) providers
resolve to "browser-contract" — the primary kimcli path for those
providers, since BrowserProvider.complete() speaks the /v1/responses JSON
contract rather than native tool-calling. Every other provider (claude,
gemini — both auth modes, deepseek, ollama-behind-proxy) resolves to
"responses-passthrough": codex 0.144.3 removed the chat-completions wire
API entirely (``WireApi`` in codex-rs/model-provider-info/src/lib.rs has
only the ``Responses`` variant), so codex will never call
/v1/chat/completions — API providers must be served natively on
/v1/responses via codex_engine/responses_passthrough.py, which keeps item
structure (no prompt flattening) and calls complete() with the native
BaseProvider signature (no clear_chat/handoff kwargs). "chat-passthrough"
(codex_engine/chat_passthrough.py's OpenAI /v1/chat/completions
translation) is never selected by auto-resolution — codex itself has no use
for it — but stays available as an explicit override for OpenAI-compatible
clients other than codex that do speak wire_api="chat". All three explicit
mode values remain available as overrides for testing/parity.

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
        "--mode", default="auto",
        choices=["auto", "browser-contract", "responses-passthrough", "chat-passthrough"],
        help="See module docstring — 'auto' resolves browser:* providers to "
             "browser-contract and everything else to responses-passthrough.",
    )
    p.add_argument("--max-relays", type=int, default=None)
    p.add_argument("--parent-pid", type=int, default=None, help="Watchdog: exit if this pid disappears.")
    p.add_argument(
        "--preflight", action="store_true",
        help="Best-effort provider warm-up status printed to stderr, then continue serving.",
    )
    return p.parse_args(argv)


def _resolve_auto_mode(provider_name: str) -> str:
    """"auto" mode resolution (module docstring "Mode").

    browser:* providers (and bare "browser") are the primary kimcli path for
    those providers — BrowserProvider.complete() speaks the /v1/responses
    JSON contract, not native BaseProvider tool-calling — so they resolve to
    "browser-contract". Every other provider (claude, gemini, deepseek,
    ollama-behind-proxy, ...) resolves to "responses-passthrough": codex
    0.144.3 removed the chat-completions wire API entirely, so those
    providers are served natively on /v1/responses instead.
    """
    name = provider_name.strip().lower()
    return "browser-contract" if name == "browser" or name.startswith("browser:") else "responses-passthrough"


def _pid_alive(pid: int) -> bool:
    """Best-effort cross-platform liveness check for the watchdog.

    POSIX: os.kill(pid, 0) — no signal sent, just existence/permission check.
    Windows: os.kill(pid, 0) is not supported by CPython for arbitrary pids
    (raises OSError unconditionally), and psutil is NOT a project dependency
    (see requirements.txt) — so this uses ctypes OpenProcess +
    GetExitCodeProcess. OpenProcess succeeding is NOT enough on its own: a
    Windows process object stays queryable by pid as long as ANY handle to
    it remains open anywhere — including the launcher's own
    ``subprocess.Popen`` handle to a parent it already terminated and
    waited on — so a bare "did OpenProcess succeed" check read an already-
    dead parent as alive forever and the watchdog never fired (verified:
    this is what made tests/test_standalone_proxy.py's watchdog test hang
    on Windows CI). Comparing the queried exit code against STILL_ACTIVE
    (259) reports the true run state regardless of who else still holds a
    handle to the process object.
    If ctypes itself is unavailable, degrade gracefully: assume alive rather
    than risk spuriously killing a live proxy from a broken liveness check.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
            )
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                queried = ctypes.windll.kernel32.GetExitCodeProcess(  # type: ignore[attr-defined]
                    handle, ctypes.byref(exit_code),
                )
                if not queried:
                    return False  # couldn't read the exit code — don't orphan the proxy on a guess
                return exit_code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
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

    mode = _resolve_auto_mode(args.provider) if args.mode == "auto" else args.mode

    proxy = _CodexProxy(
        provider,
        provider_name=args.provider,
        thread_state={},
        stateful=False,
        mode=mode,
        max_relays=args.max_relays,
    )

    try:
        port = await proxy.start()
    except Exception as e:
        _print_fatal(f"Could not start proxy server: {e}")
        return 1

    _print_ready(port, proxy._bearer_token)  # noqa: SLF001 — same-package access

    # Contract (module docstring): stdout carries exactly the ONE handshake
    # line just printed above, then becomes an alias of stderr for the rest
    # of this process's life. dup2 repoints the underlying OS file descriptor
    # (fd 1), so every later write through it — engine.py's browser-contract
    # narration/compaction/salvage bare print()s, --preflight status, any
    # third-party library that writes to stdout — is redirected transparently.
    # No rebinding of the `sys.stdout` Python object is needed: it still
    # wraps fd 1, which now points at the same file as stderr.
    sys.stdout.flush()
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

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

"""
Standalone bridge relay — runs a local HTTP proxy and routes Codex's LLM
requests through Kim's BrowserProvider.

Unlike `run_codex_bridge.py`, this script does NOT spawn the `codex` binary.
The caller is expected to launch `codex` separately with configuration
pointing to Kim's local proxy.

The shell wrapper at `scripts/codex-via-browser` is the intended caller.
That wrapper backgrounds this relay (with stdio piped to a log file so it
does not interfere with Codex's TUI) and then execs `codex` in the foreground,
giving the user the full interactive REPL while every LLM call is silently
relayed through their logged-in browser session.

The relay runs until it receives SIGINT/SIGTERM.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from mcp_server.tools.codex_bridge import _CodexProxy  # noqa: E402
from orchestrator.providers.base import create_provider  # noqa: E402

logger = logging.getLogger("kim.run_codex_relay")


def _load_config(path: str | None) -> dict:
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


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run a local HTTP proxy that relays Codex LLM calls through Kim's browser provider.",
    )
    p.add_argument(
        "--port",
        type=int,
        default=0,
        help="Port for the proxy server (0 = auto-assign).",
    )
    p.add_argument(
        "--provider",
        default=os.environ.get("KIM_PROVIDER", "browser:gemini"),
        help="Browser provider identifier, e.g. 'browser:gemini' or 'browser:claude'.",
    )
    p.add_argument(
        "--config",
        default=None,
        help="Optional path to config.yaml.",
    )
    p.add_argument(
        "--ready-file",
        default=None,
        help="Optional path; relay creates this file once the proxy is ready.",
    )
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


async def _run_relay(args: argparse.Namespace) -> int:
    if not args.provider.lower().startswith("browser"):
        print(
            f"[ERROR] run_codex_relay requires a browser provider; got {args.provider!r}.",
            file=sys.stderr,
        )
        return 2

    config = _load_config(args.config)
    config["provider"] = args.provider

    logger.info(f"Provider: {args.provider}")
    provider = create_provider(args.provider, config)

    # Start the proxy
    proxy = _CodexProxy(provider)
    port = await proxy.start()
    logger.info(f"Codex proxy listening on http://127.0.0.1:{port}")

    # Signal the wrapper that we are ready
    if args.ready_file:
        try:
            Path(args.ready_file).write_text(f"ready:{port}", encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not write ready file {args.ready_file}: {e}")

    # Print port so the wrapper script can pick it up
    print(f"PROXY_PORT={port}", flush=True)

    stop_event = asyncio.Event()

    def _request_stop(*_: object) -> None:
        logger.info("Stop signal received — shutting down proxy")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            pass

    try:
        await stop_event.wait()
    finally:
        await proxy.stop()

    logger.info("Relay exited cleanly")
    return 0


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        rc = asyncio.run(_run_relay(args))
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()

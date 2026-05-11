"""
Standalone bridge relay — watches a bridge directory and routes Claw's LLM
requests through Kim's BrowserProvider.

Unlike `run_claw_bridge.py`, this script does NOT spawn the `claw` binary.
The caller is expected to launch `claw` separately with these env vars:

    CLAW_FILE_BRIDGE=1
    CLAW_BRIDGE_DIR=<same path passed via --bridge-dir>

The shell wrapper at `scripts/claw-via-browser` is the intended caller.
That wrapper backgrounds this relay (with stdio piped to a log file so it
does not interfere with claw's TUI) and then execs `claw` in the foreground,
giving the user the full interactive REPL while every LLM call is silently
relayed through their logged-in browser session.

The relay loops forever until it receives SIGINT/SIGTERM. Each `bridge_request.json`
the binary writes is consumed and answered with `bridge_response.json`. The
existing one-shot helpers in `mcp_server.tools.claw_bridge` are reused for
the per-request work — only the loop control changes.
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

from mcp_server.tools.claw_bridge import (  # noqa: E402
    POLL_INTERVAL,
    _relay_one_request,
    _restore_browser_state,
    _save_browser_state,
)
from orchestrator.providers.base import BaseProvider, create_provider  # noqa: E402

logger = logging.getLogger("kim.run_claw_relay")


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


def _setup_signals(stop_event: asyncio.Event) -> None:
    def _request_stop(*_: object) -> None:
        logger.info("Stop signal received — exiting relay loop")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            # Windows: signal.add_signal_handler is unsupported; fall back to
            # the default behaviour (KeyboardInterrupt for SIGINT).
            pass


async def _relay_loop(
    provider: BaseProvider,
    bridge_dir: Path,
    request_file: Path,
    stop_event: asyncio.Event,
) -> int:
    relay_count = 0
    while not stop_event.is_set():
        if request_file.exists():
            relay_count += 1
            logger.info(f"[relay #{relay_count}] Picked up bridge_request.json")
            try:
                await _relay_one_request(provider, relay_count, bridge_dir)
            except Exception as e:  # noqa: BLE001
                logger.exception(f"[relay #{relay_count}] Failed: {e}")
        else:
            # Sleep briefly, but break out fast on stop.
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass
    return relay_count


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Watch a Claw bridge directory and relay LLM calls through Kim's browser provider.",
    )
    p.add_argument(
        "--bridge-dir",
        required=True,
        help="The directory claw was launched with (CLAW_BRIDGE_DIR).",
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
        help="Optional path; relay creates this file once the browser provider is connected.",
    )
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


async def _run_relay(args: argparse.Namespace) -> int:
    if not args.provider.lower().startswith("browser"):
        print(
            f"[ERROR] run_claw_relay requires a browser provider; got {args.provider!r}.",
            file=sys.stderr,
        )
        return 2

    bridge_dir = Path(args.bridge_dir).resolve()
    bridge_dir.mkdir(parents=True, exist_ok=True)
    request_file = bridge_dir / "bridge_request.json"

    config = _load_config(args.config)
    config["provider"] = args.provider

    logger.info(f"Bridge dir: {bridge_dir}")
    logger.info(f"Provider:   {args.provider}")
    provider = create_provider(args.provider, config)

    # Save browser state so we can restore it after the user exits claw.
    saved_url = await _save_browser_state(provider)

    # Signal the wrapper that we are ready to relay so it can launch claw
    # without racing the very first request.
    if args.ready_file:
        try:
            Path(args.ready_file).write_text("ready", encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not write ready file {args.ready_file}: {e}")

    stop_event = asyncio.Event()
    _setup_signals(stop_event)

    relay_count = 0
    try:
        relay_count = await _relay_loop(provider, bridge_dir, request_file, stop_event)
    finally:
        await _restore_browser_state(provider, saved_url)

    logger.info(f"Relay exited cleanly after {relay_count} request(s)")
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

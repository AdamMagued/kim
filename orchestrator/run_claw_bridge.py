"""
Spawn `claw` and relay its LLM calls through Kim's BrowserProvider so users
can run Code-tab tasks without an Anthropic API key.

This wraps the file-bridge protocol Claw already supports (`CLAW_FILE_BRIDGE=1`).
Claw writes each LLM request to <CLAW_BRIDGE_DIR>/bridge_request.json; we read
it, hand it to BrowserProvider.complete(), and write bridge_response.json back.
The relay loop lives in `mcp_server/tools/claw_bridge.run_claw_subtask`.

Usage (invoked by the Tauri shell, not by humans):

    python -m orchestrator.run_claw_bridge \
        --task "write fibonacci.py and test it" \
        --cwd  /path/to/project \
        --provider browser:gemini

The script emits human-readable progress lines on stdout that ChatView's
`parseLogLine` already understands (`[STATUS] …`, `[SUCCESS] …`, etc.).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Make `import orchestrator.…` and `import mcp_server.…` resolve when we are
# launched via `python -m orchestrator.run_claw_bridge` from the kim-pro root.
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from mcp_server.tools.claw_bridge import run_claw_subtask  # noqa: E402
from orchestrator.providers.base import create_provider  # noqa: E402


def _load_config(path: str | None) -> dict:
    """Lightweight config loader — avoids pulling agent.py's heavy imports.

    Reads `<repo>/config.yaml` (or the explicit path) when PyYAML is
    available, otherwise returns {}. BrowserProvider only needs a handful of
    keys (`browser_provider.*`, `project_root`, `voice`, …) and falls back
    to defaults for anything missing.
    """
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


logger = logging.getLogger("kim.run_claw_bridge")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run a Claw task via Kim's browser provider.")
    p.add_argument("--task", required=True, help="The coding task to hand to Claw.")
    p.add_argument("--cwd", required=True, help="Working directory Claw should run in (the user's project).")
    p.add_argument(
        "--provider",
        default=os.environ.get("KIM_PROVIDER", "browser"),
        help="Browser provider identifier, e.g. 'browser:gemini' or 'browser:claude'.",
    )
    p.add_argument("--config", default=None, help="Optional path to config.yaml.")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


async def _main_async(args: argparse.Namespace) -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not args.provider.lower().startswith("browser"):
        # Defensive: this script only makes sense for browser-backed runs.
        # If the user picked an API provider, the Tauri side should call Claw
        # directly with the API key, not through us.
        print(
            f"[ERROR] run_claw_bridge requires a browser provider; got {args.provider!r}.",
            file=sys.stderr,
        )
        return 2

    config = _load_config(args.config)
    config["provider"] = args.provider

    # Make sure BrowserProvider's project_root resolution stays anchored to
    # the user's *project* (not the Kim repo) so any logging that references
    # cwd is accurate. PROJECT_ROOT for the bridge is intentionally the user
    # project — the relay never executes tools itself, Claw does.
    os.environ["PROJECT_ROOT"] = args.cwd

    print("[STATUS] Kim is working on your task…", flush=True)

    provider = create_provider(args.provider, config)

    # Honor CLAW_BIN so the Tauri side can hand us the exact binary path it
    # already resolved via find_claw_binary, instead of relay_subtask repeating
    # the search dance with a different default. Empty string falls through
    # to run_claw_subtask's own default.
    claw_bin_env = os.environ.get("CLAW_BIN", "").strip() or None

    try:
        result = await run_claw_subtask(
            task=args.task,
            browser_provider=provider,  # type: ignore[arg-type]
            cwd=args.cwd,
            claw_binary=claw_bin_env,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Claw bridge crashed")
        print(f"[FAILED] Claw bridge error: {e}", flush=True)
        return 1

    msg = result.get("message", "")
    if result.get("success"):
        # Log the technical detail to stderr; surface a clean message for the UI.
        logger.info(msg)
        print("[SUCCESS] Task completed", flush=True)
        return 0

    logger.error(msg)
    print(f"[FAILED] {msg}", flush=True)
    return int(result.get("exit_code") or 1)


def main() -> None:
    args = _parse_args()
    try:
        rc = asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()

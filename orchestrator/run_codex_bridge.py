"""
DEPRECATED: superseded by orchestrator/codex_bridge_service.py (Phase 7).
The Tauri subprocess spawn now points at codex_bridge_service.
This file is kept only for reference and will be deleted after smoke testing.

Spawn `codex` and relay its LLM calls through Kim's BrowserProvider so users
can run Code-tab tasks without an API key.

This uses Codex's native HTTP proxy approach to relay LLM calls through
Codex talks to a local HTTP server that Kim starts, which translates requests
through BrowserProvider.complete().

Usage (invoked by the Tauri shell, not by humans):

    python -m orchestrator.run_codex_bridge \
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
# launched via `python -m orchestrator.run_codex_bridge` from the kim-pro root.
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from mcp_server.tools.codex_bridge import run_codex_subtask  # noqa: E402
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


logger = logging.getLogger("kim.run_codex_bridge")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run a Codex task via Kim's browser provider.")
    p.add_argument("--task", required=True, help="The coding task to hand to Codex.")
    p.add_argument("--cwd", required=True, help="Working directory Codex should run in (the user's project).")
    p.add_argument(
        "--provider",
        default=os.environ.get("KIM_PROVIDER", "browser"),
        help="Browser provider identifier, e.g. 'browser:gemini' or 'browser:claude'.",
    )
    p.add_argument("--model", default=None, help="Model name to pass to Codex.")
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
        # If the user picked an API provider, the Tauri side should call Codex
        # directly with the API key, not through us.
        print(
            f"[ERROR] run_codex_bridge requires a browser provider; got {args.provider!r}.",
            file=sys.stderr,
        )
        return 2

    config = _load_config(args.config)
    config["provider"] = args.provider

    # Make sure BrowserProvider's project_root resolution stays anchored to
    # the user's *project* (not the Kim repo) so any logging that references
    # cwd is accurate.
    os.environ["PROJECT_ROOT"] = args.cwd

    print(f"[STATUS] ✓ Using Codex via browser bridge ({args.provider})", flush=True)

    provider = create_provider(args.provider, config)

    # Honor CODEX_BIN so the Tauri side can hand us the exact binary path it
    # already resolved, instead of run_codex_subtask repeating the search.
    codex_bin_env = os.environ.get("CODEX_BIN", "").strip() or None

    try:
        result = await run_codex_subtask(
            task=args.task,
            browser_provider=provider,  # type: ignore[arg-type]
            cwd=args.cwd,
            codex_binary=codex_bin_env,
            model=args.model,
            provider_name=args.provider,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Codex bridge crashed")
        print(f"[FAILED] Codex bridge error: {e}", flush=True)
        return 1

    # JSONL output is already streamed to stdout line-by-line by run_codex_subtask.
    # Only emit a [FAILED] line if Codex itself exited with an error, so Tauri
    # can surface a user-facing message.
    msg = result.get("message", "")
    if result.get("success"):
        logger.info(msg)
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

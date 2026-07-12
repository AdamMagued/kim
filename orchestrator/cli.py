"""
Kim CLI entry point.

Extracted from orchestrator/agent.py to keep the agent class file focused.

Usage:
    python -m orchestrator.agent --task "open Notepad"
    python -m orchestrator.agent --task "..." --provider claude
    python -m orchestrator.agent --task "..." --provider browser:chatgpt
"""

import argparse
import logging
import sys
import tempfile
from pathlib import Path

from orchestrator.agent_config import DEFAULT_PROVIDER
from orchestrator.events_gen import (
    emit_activity,
    emit_answer,
    emit_run_done,
    emit_agent_done,
    emit_agent_cancelled,
    emit_agent_error,
)


def resolve_log_dir() -> Path:
    """D3: pick the first writable log directory.

    Repo `logs/` is preferred, but a packaged/installed checkout may be
    read-only, so fall back to a per-user dir and finally the temp dir. Never
    raises — logging must not crash startup.
    """
    candidates = [
        Path(__file__).resolve().parent.parent / "logs",
        Path.home() / ".kim" / "logs",
        Path(tempfile.gettempdir()) / "kim-logs",
    ]
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write_test"
            probe.write_text("x")
            probe.unlink()
            return d
        except Exception:
            continue
    return Path(tempfile.gettempdir())


def _cli_provider_type(value: str) -> str:
    """Allow `browser:claude` / `browser:chatgpt` (desktop) as well as plain provider names."""
    s = (value or "").strip().lower()
    base = {"claude", "openai", "gemini", "deepseek", "browser", "ollama"}
    if s in base:
        return s
    if s.startswith("browser:") and len(s) > len("browser:"):
        return s
    raise argparse.ArgumentTypeError(
        f"unknown provider {value!r}; use claude, openai, gemini, deepseek, browser, "
        "ollama, or browser:<site> (e.g. browser:chatgpt)"
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m orchestrator.agent", description="Kim — autonomous AI agent")
    p.add_argument("--task", "-t", help="Task to execute")
    p.add_argument("--provider", "-p", type=_cli_provider_type, metavar="NAME")
    p.add_argument("--config", "-c", help="Path to config.yaml")
    p.add_argument("--max-iter", type=int)
    p.add_argument("--resume", "-r", metavar="SESSION_ID",
                   help="Resume a previous session by ID (loads saved messages)")
    p.add_argument("--session-dir", help="Directory to save session files")
    p.add_argument("--verbose", "-v", action="store_true")
    return p


async def _cli_main(args: argparse.Namespace) -> None:
    # Import here to avoid circular import (cli <- agent <- cli would be circular)
    from orchestrator.agent import load_config, mcp_agent_context

    config = load_config(args.config)
    if args.provider:
        config["provider"] = args.provider
    # L13: use `is not None` so an explicit --max-iter 0 is honored rather than
    # silently dropped by a falsy check.
    if args.max_iter is not None:
        config["max_iterations"] = args.max_iter

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        stream=sys.stderr,
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Wire structured rotating file logs (logs/kim-YYYY-MM-DD.jsonl, 7-day retention)
    try:
        from mcp_server.logger import setup_structured_logging, apply_log_retention
        # D3: repo logs/ may be read-only (packaged/installed); fall back to a
        # user-writable dir so logging never crashes startup.
        _log_dir = resolve_log_dir()
        setup_structured_logging(log_dir=str(_log_dir), level=log_level, also_stderr=False)
        apply_log_retention(log_dir=str(_log_dir), keep_days=7)
    except Exception as _log_err:
        logging.getLogger(__name__).debug(f"Could not set up structured logs: {_log_err}")

    task = args.task or input("Task: ").strip()
    print(f"Running: {task!r}  provider={config.get('provider', DEFAULT_PROVIDER)}", file=sys.stderr)

    result = None
    try:
        async with mcp_agent_context(
            config,
            resume_session_id=args.resume,
            session_dir=args.session_dir,
        ) as agent:
            result = await agent.run(task)
    except Exception as e:
        result = {"success": False, "summary": f"Error: {e}", "termination": "failed"}

    status = "SUCCESS" if result["success"] else "FAILED"
    termination = result.get('termination', 'unknown')
    # Typed JSON line: parsed by KimEvent::RunDone in Rust subprocess.rs and
    # forwarded as kim:run-done to the frontend.  Emitted before the human-readable
    # [STATUS] line so Rust sees it while the process is still running.
    emit_run_done(termination, bool(result["success"]))
    if result["success"]:
        answer = str(result.get("summary", "")).strip()
        if answer.upper().startswith("TASK_COMPLETE:"):
            answer = answer[len("TASK_COMPLETE:"):].strip()
        if answer:
            emit_answer(answer)
        emit_agent_done(True)
    elif termination == "cancelled":
        emit_agent_cancelled(False)
    else:
        # #26/#27: in typed IPC mode Rust drops the legacy `[FAILED] <summary>`
        # text line, so failed runs (including NEED_HELP questions) lost their
        # detail — the UI only saw the bare termination code via kim:run-failed.
        # Emit the summary as a typed activity event: the frontend's kim:activity
        # error handler surfaces the real text and preserves it instead of
        # overwriting with a generic banner. User cancels are excluded — they
        # are not errors and already flow through kim-agent-cancelled.
        failure_detail = str(result.get("summary", "")).strip()
        if failure_detail:
            emit_activity("error", failure_detail)
        emit_agent_error(failure_detail or "unknown error")
        emit_agent_done(False)
    # Human-readable line for legacy kim-agent-output path and activity feed.
    print(f"[STATUS] run ended: {termination}", flush=True)
    print(f"\n[{status}] {result['summary']}")

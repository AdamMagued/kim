"""
Explicit state machine types for the KimAgent run loop.

Phase 10 of the ai-safety restructuring: replacing implicit
bare-string terminal conditions with typed enum values.

The run() method returns a result dict; AgentTermination names
each possible exit path so callers and readers can reason about
control flow without parsing string summaries.
"""

from __future__ import annotations

from enum import Enum


class AgentTermination(Enum):
    """Every way the KimAgent run loop can terminate."""
    TASK_COMPLETE = "task_complete"
    NEED_HELP = "need_help"
    CANCELLED = "cancelled"
    MAX_ITERATIONS = "max_iterations"
    PROVIDER_FAILED = "provider_failed"
    CONVERSATIONAL_LOOP = "conversational_loop"
    STUCK = "stuck"


def make_run_result(
    termination: AgentTermination,
    summary: str,
    screenshot: str = "",
) -> dict:
    """Build the standard run() return dict from a typed termination reason.

    All callers (tray/ui.py, task_queue.py, cli.py) expect exactly:
        {"success": bool, "summary": str, "screenshot": str}
    This helper keeps that contract in one place.
    """
    success = termination == AgentTermination.TASK_COMPLETE
    return {
        "success": success,
        "termination": termination.value,
        "summary": summary,
        "screenshot": screenshot,
    }


def run_failure_event(
    termination: AgentTermination,
    summary: str,
    provider_error_code: str = "",
) -> dict | None:
    """Build a kim:run_failed event payload for failed (non-success) runs.

    Returns None for TASK_COMPLETE and CANCELLED (not failures).
    """
    if termination in (AgentTermination.TASK_COMPLETE, AgentTermination.CANCELLED):
        return None

    # Determine recoverability and suggested action
    t = termination
    code = provider_error_code.lower()

    # CDP / browser provider errors detected from summary text
    is_browser_error = any(
        marker in summary.lower()
        for marker in ("cdp", "remote-debugging", "playwright", "browser provider")
    )

    if t == AgentTermination.PROVIDER_FAILED:
        if is_browser_error:
            recoverable = True
            suggestion = "Start Chrome with --remote-debugging-port=9222 and retry."
        elif code in ("auth", "permission"):
            recoverable = False
            suggestion = "Re-authenticate in Settings → AI Provider."
        elif code in ("rate_limit",):
            recoverable = True
            suggestion = "Wait a moment, then retry the task."
        elif code in ("network", "timeout"):
            recoverable = True
            suggestion = "Check your internet connection and retry."
        elif code in ("server_error",):
            recoverable = True
            suggestion = "Provider server error — retry in a moment."
        else:
            recoverable = True
            suggestion = "Check Settings → AI Provider and retry."
    elif t == AgentTermination.STUCK:
        recoverable = True
        suggestion = "Kim may be confused. Try rephrasing your task or breaking it into smaller steps."
    elif t == AgentTermination.MAX_ITERATIONS:
        recoverable = True
        suggestion = "Task hit the iteration limit. Try breaking it into smaller steps."
    elif t == AgentTermination.NEED_HELP:
        recoverable = True
        suggestion = "Kim needs clarification. Add more details to your task description."
    elif t == AgentTermination.CONVERSATIONAL_LOOP:
        recoverable = True
        suggestion = "Kim got stuck in a loop. Try rephrasing your task."
    else:
        recoverable = False
        suggestion = "An unexpected error occurred. Check Settings → Feedback → Reveal logs."

    return {
        "type": "run_failed",
        "reason": t.value,
        "recoverable": recoverable,
        "suggestion": suggestion,
    }

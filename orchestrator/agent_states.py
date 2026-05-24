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
    return {"success": success, "summary": summary, "screenshot": screenshot}

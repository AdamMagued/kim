# flake8: noqa
# events_gen.py -- DO NOT HAND-EDIT
# Generated from desktop/src/types/events.schema.json via npm run gen:events.

from __future__ import annotations

import json
import os
import sys
from typing import Any

STATUS = "kim:status"
PLAN = "kim:plan"
STEP = "kim:step"
DONE = "kim:done"
CONTEXT = "kim:context"
STATS = "kim:stats"
UI = "kim:ui"
RUN_DONE = "kim:run-done"
RUN_FAILED = "kim:run-failed"
PROVIDER_ERROR = "kim:provider-error"
RATE_LIMITED = "kim:rate-limited"
HITL_APPROVAL_REQUEST = "kim:hitl-approval-request"
HITL_APPROVAL_RESULT = "kim:hitl-approval-result"
TOOL = "kim:tool"
ANSWER = "kim:answer"
DIFF = "kim:diff"
ACTIVITY = "kim:activity"
COMMAND_APPROVAL_REQUEST = "kim:command-approval-request"
FILE_CHANGE_APPROVAL_REQUEST = "kim:file-change-approval-request"
COMMAND_OUTPUT = "kim:command-output"
ASSISTANT_DELTA = "kim:assistant-delta"
REASONING_DELTA = "kim:reasoning-delta"
PLAN_UPDATE = "kim:plan-update"
DIFF_UPDATE = "kim:diff-update"
TOKEN_USAGE = "kim:token-usage"
ITEM_LIFECYCLE = "kim:item-lifecycle"
TURN_LIFECYCLE = "kim:turn-lifecycle"

# K5: named bracket-tag constants — the single source of truth for the
# text-protocol vocabulary. Emitters (codex_engine, codex_bridge_service)
# must reference these instead of re-typing the literals.
LOG_TAG_STATUS = "[STATUS]"
LOG_TAG_PLAN = "[PLAN]"
LOG_TAG_STEP = "[STEP]"
LOG_TAG_DONE = "[DONE]"
LOG_TAG_CONTEXT = "[CONTEXT]"
LOG_TAG_STATS = "[STATS]"
LOG_TAG_TOOL = "[TOOL]"
LOG_TAG_ANSWER = "[ANSWER]"
LOG_TAG_DIFF = "[DIFF]"
LOG_TAG_SUCCESS = "[SUCCESS]"
LOG_TAG_FAILED = "[FAILED]"
LOG_TAG_ERROR = "[ERROR]"
LOG_TAG_TASK_COMPLETE = "TASK_COMPLETE:"
LOG_TAG_NEED_HELP = "NEED_HELP:"

LEGACY_LOG_TAGS = {
    "[STATUS]": {"tag":"[STATUS]","event":"kim:status"},
    "[PLAN]": {"tag":"[PLAN]","event":"kim:plan"},
    "[STEP]": {"tag":"[STEP]","event":"kim:step"},
    "[DONE]": {"tag":"[DONE]","event":"kim:done"},
    "[CONTEXT]": {"tag":"[CONTEXT]","event":"kim:context"},
    "[STATS]": {"tag":"[STATS]","event":"kim:stats"},
    "[TOOL]": {"tag":"[TOOL]","event":"kim:tool"},
    "[ANSWER]": {"tag":"[ANSWER]","event":"kim:answer"},
    "[DIFF]": {"tag":"[DIFF]","event":"kim:diff"},
    "[SUCCESS]": {"tag":"[SUCCESS]","event":"kim:activity","kind":"success"},
    "[FAILED]": {"tag":"[FAILED]","event":"kim:activity","kind":"error"},
    "[ERROR]": {"tag":"[ERROR]","event":"kim:activity","kind":"error"},
    "TASK_COMPLETE:": {"tag":"TASK_COMPLETE:","event":"kim:answer"},
    "NEED_HELP:": {"tag":"NEED_HELP:","event":"kim:activity","kind":"error"},
}


def emit_event(event_type: str, **payload: Any) -> None:
    line = json.dumps({"type": event_type, **payload}, separators=(",", ":"), ensure_ascii=False)
    data = (line + "\n").encode("utf-8", errors="replace")

    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        try:
            buffer.write(data)
            buffer.flush()
            return
        except OSError:
            pass

    try:
        os.write(sys.stdout.fileno(), data)
        return
    except (AttributeError, OSError, ValueError):
        pass

    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        return
    except Exception:
        pass

    fallback = getattr(sys.__stdout__, "buffer", None)
    if fallback is not None:
        fallback.write(data)
        fallback.flush()


def emit_status(message: str) -> None:
    emit_event("status", message=message)


def emit_plan(steps: list[object]) -> None:
    emit_event("plan", steps=steps)


def emit_step(n: float, data: dict[str, object]) -> None:
    emit_event("step", n=n, data=data)


def emit_done(n: float) -> None:
    emit_event("done", n=n)


def emit_context(cumulative_input: float, budget: float, phase: str, percent: float, last_input: float, last_output: float, source: str, estimate: bool) -> None:
    emit_event("context", cumulative_input=cumulative_input, budget=budget, phase=phase, percent=percent, last_input=last_input, last_output=last_output, source=source, estimate=estimate)


def emit_stats(input: float, output: float, total: float) -> None:
    emit_event("stats", input=input, output=output, total=total)


def emit_ui(action: str) -> None:
    wire_type = {
        "screenshot_flash": "ui_screenshot_flash",
        "show": "ui_show",
    }[action]
    emit_event(wire_type)


def emit_run_done(termination: str, success: bool) -> None:
    emit_event("run_done", termination=termination, success=success)


def emit_run_failed(reason: str, recoverable: bool, suggestion: str) -> None:
    emit_event("run_failed", reason=reason, recoverable=recoverable, suggestion=suggestion)


def emit_provider_error(code: str, retryable: bool) -> None:
    emit_event("provider_error", code=code, retryable=retryable)


def emit_rate_limited(delay: float, attempt: float, max_retries: float) -> None:
    emit_event("rate_limited", delay=delay, attempt=attempt, max_retries=max_retries)


def emit_hitl_approval_request(tool: str, risk: str, reason: str, preview: str) -> None:
    emit_event("hitl_approval_request", tool=tool, risk=risk, reason=reason, preview=preview)


def emit_hitl_approval_result(tool: str, approved: bool) -> None:
    emit_event("hitl_approval_result", tool=tool, approved=approved)


def emit_tool(name: str, args: dict[str, object]) -> None:
    emit_event("tool", name=name, args=args)


def emit_answer(text: str) -> None:
    emit_event("answer", text=text)


def emit_diff(path: str, added: float, removed: float) -> None:
    emit_event("diff", path=path, added=added, removed=removed)


def emit_activity(kind: str, text: str) -> None:
    emit_event("activity", kind=kind, text=text)


def emit_command_approval_request(id: str, command: str, cwd: str, reason: str, risk: str, network: bool, amendment: list[object]) -> None:
    emit_event("command_approval_request", id=id, command=command, cwd=cwd, reason=reason, risk=risk, network=network, amendment=amendment)


def emit_file_change_approval_request(id: str, files: list[object], reason: str) -> None:
    emit_event("file_change_approval_request", id=id, files=files, reason=reason)


def emit_command_output(item_id: str, chunk: str) -> None:
    emit_event("command_output", item_id=item_id, chunk=chunk)


def emit_assistant_delta(chunk: str) -> None:
    emit_event("assistant_delta", chunk=chunk)


def emit_reasoning_delta(chunk: str) -> None:
    emit_event("reasoning_delta", chunk=chunk)


def emit_plan_update(steps: list[object]) -> None:
    emit_event("plan_update", steps=steps)


def emit_diff_update(unified_diff: str) -> None:
    emit_event("diff_update", unified_diff=unified_diff)


def emit_token_usage(input: float, output: float, total: float) -> None:
    emit_event("token_usage", input=input, output=output, total=total)


def emit_item_lifecycle(item_id: str, kind: str, phase: str, title: str) -> None:
    emit_event("item_lifecycle", item_id=item_id, kind=kind, phase=phase, title=title)


def emit_turn_lifecycle(phase: str, turn_id: str) -> None:
    emit_event("turn_lifecycle", phase=phase, turn_id=turn_id)

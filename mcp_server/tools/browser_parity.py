"""Browser-provider parity tools that do not require a second agent runtime.

``web_search`` is a provider-native affordance: its result asks the browser LLM
itself to use the search capability exposed by the current chat product.

Background commands deliberately delegate to ``handle_run_command`` so the
existing shell deny-list, sandbox checks, encoding behavior, and process cleanup
remain the single implementation of command execution.

``ask_user`` converts a structured tool call into Kim's existing ``NEED_HELP``
text protocol. The browser prompt tells the model to echo that result exactly,
which pauses the current run without introducing a second IPC protocol.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from mcp_server.tools.shell import MAX_SHELL_TIMEOUT_S, handle_run_command

# Background commands run through ``handle_run_command``, which hard-clamps every
# timeout to ``MAX_SHELL_TIMEOUT_S`` (600s). Advertising a larger bound here would
# be dishonest, so the background cap is the single shell cap.
_MAX_BACKGROUND_TIMEOUT_SECONDS = MAX_SHELL_TIMEOUT_S
_COMPLETED_JOB_TTL_SECONDS = 15 * 60


@dataclass
class _BackgroundJob:
    """In-memory state for one command owned by the MCP server event loop."""

    task: asyncio.Task[str]
    created_at: float
    status: str = "running"
    finished_at: float | None = None
    result: str | None = None
    error: str | None = None


_BACKGROUND_JOBS: dict[str, _BackgroundJob] = {}
_BACKGROUND_JOBS_LOCK = threading.RLock()


def _error(message: str) -> str:
    return f"ERROR: {message}"


def _prune_background_jobs() -> None:
    cutoff = time.monotonic() - _COMPLETED_JOB_TTL_SECONDS
    with _BACKGROUND_JOBS_LOCK:
        expired = [
            job_id
            for job_id, job in _BACKGROUND_JOBS.items()
            if job.finished_at is not None and job.finished_at < cutoff
        ]
        for job_id in expired:
            _BACKGROUND_JOBS.pop(job_id, None)


def _finish_background_job(job_id: str, task: asyncio.Future[str]) -> None:
    """Capture task completion so exceptions are retrieved even if never polled."""
    status = "completed"
    result: str | None = None
    error: str | None = None
    try:
        result = task.result()
    except asyncio.CancelledError:
        status = "cancelled"
    except Exception as exc:  # noqa: BLE001 - tool errors are returned to the model
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"

    with _BACKGROUND_JOBS_LOCK:
        job = _BACKGROUND_JOBS.get(job_id)
        if job is None:
            return
        job.status = status
        job.result = result
        job.error = error
        job.finished_at = time.monotonic()


def _job_payload(job_id: str, job: _BackgroundJob) -> dict[str, Any]:
    payload: dict[str, Any] = {"job_id": job_id, "status": job.status}
    if job.result is not None:
        payload["result"] = job.result
    if job.error is not None:
        payload["error"] = job.error
    return payload


async def handle_web_search(args: dict) -> str:
    """Ask the current browser LLM to use its native web-search capability."""
    query = str(args.get("query", "")).strip()
    if not query:
        return _error("query must not be empty")

    max_results_raw = args.get("max_results", 5)
    recency_days_raw = args.get("recency_days")
    try:
        max_results = int(max_results_raw)
    except (TypeError, ValueError):
        return _error("max_results must be an integer")
    if not 1 <= max_results <= 20:
        return _error("max_results must be between 1 and 20")

    recency_days: int | None = None
    if recency_days_raw is not None:
        try:
            recency_days = int(recency_days_raw)
        except (TypeError, ValueError):
            return _error("recency_days must be an integer")
        if not 0 <= recency_days <= 3650:
            return _error("recency_days must be between 0 and 3650")

    request: dict[str, Any] = {
        "action": "provider_native_web_search",
        "query": query,
        "max_results": max_results,
        "instructions": (
            "Use the current browser chat provider's built-in web search now. "
            "Synthesize the findings, include source names and URLs, and then "
            "continue the original task. Do not call web_search again for this query."
        ),
    }
    if recency_days is not None:
        request["recency_days"] = recency_days
    return json.dumps(request, ensure_ascii=False)


async def handle_background_start(args: dict) -> str:
    """Start ``run_command`` without blocking the MCP request until completion."""
    _prune_background_jobs()
    cmd = str(args.get("cmd", "")).strip()
    if not cmd:
        return _error("cmd must not be empty")

    timeout_raw = args.get("timeout", 300)
    try:
        timeout = int(timeout_raw)
    except (TypeError, ValueError):
        return _error("timeout must be an integer")
    if not 1 <= timeout <= _MAX_BACKGROUND_TIMEOUT_SECONDS:
        return _error(
            f"timeout must be between 1 and {_MAX_BACKGROUND_TIMEOUT_SECONDS} seconds"
        )

    command_args: dict[str, Any] = {"cmd": cmd, "timeout": timeout}
    if args.get("cwd") is not None:
        command_args["cwd"] = args["cwd"]

    job_id = uuid.uuid4().hex[:12]
    task = asyncio.create_task(
        handle_run_command(command_args),
        name=f"kim-background-{job_id}",
    )
    job = _BackgroundJob(task=task, created_at=time.monotonic())
    with _BACKGROUND_JOBS_LOCK:
        _BACKGROUND_JOBS[job_id] = job
    task.add_done_callback(lambda done_task: _finish_background_job(job_id, done_task))
    return json.dumps(_job_payload(job_id, job), ensure_ascii=False)


async def handle_background_poll(args: dict) -> str:
    """Return the current state and, when finished, captured command output."""
    _prune_background_jobs()
    job_id = str(args.get("job_id", "")).strip()
    if not job_id:
        return _error("job_id must not be empty")
    with _BACKGROUND_JOBS_LOCK:
        job = _BACKGROUND_JOBS.get(job_id)
        if job is None:
            return _error(f"background job {job_id!r} not found")
        task = job.task
        should_finish = job.status == "running" and task.done()
    if should_finish:
        _finish_background_job(job_id, task)
    with _BACKGROUND_JOBS_LOCK:
        current = _BACKGROUND_JOBS.get(job_id)
        if current is None:
            return _error(f"background job {job_id!r} not found")
        payload = _job_payload(job_id, current)
    return json.dumps(payload, ensure_ascii=False)


async def handle_background_cancel(args: dict) -> str:
    """Cancel a running background command and wait for handler cleanup."""
    _prune_background_jobs()
    job_id = str(args.get("job_id", "")).strip()
    if not job_id:
        return _error("job_id must not be empty")

    with _BACKGROUND_JOBS_LOCK:
        job = _BACKGROUND_JOBS.get(job_id)
        if job is None:
            return _error(f"background job {job_id!r} not found")
        task = job.task
        if job.status != "running":
            return json.dumps(_job_payload(job_id, job), ensure_ascii=False)
        task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 - done callback records the failure
        pass
    _finish_background_job(job_id, task)

    with _BACKGROUND_JOBS_LOCK:
        current = _BACKGROUND_JOBS.get(job_id)
        if current is None:
            return _error(f"background job {job_id!r} not found")
        payload = _job_payload(job_id, current)
    return json.dumps(payload, ensure_ascii=False)


async def handle_ask_user(args: dict) -> str:
    """Render a structured question through Kim's existing NEED_HELP protocol."""
    question = str(args.get("question", "")).strip()
    if not question:
        return _error("question must not be empty")
    if len(question) > 2000:
        return _error("question must be at most 2000 characters")

    raw_choices = args.get("choices")
    choices: list[str] = []
    if raw_choices is not None:
        if not isinstance(raw_choices, list):
            return _error("choices must be an array of strings")
        if len(raw_choices) > 10:
            return _error("choices must contain at most 10 items")
        for raw_choice in raw_choices:
            choice = str(raw_choice).strip()
            if not choice:
                return _error("choices must not contain empty items")
            choices.append(choice)

    output = f"NEED_HELP: {question}"
    if choices:
        rendered = "\n".join(f"{index}. {choice}" for index, choice in enumerate(choices, 1))
        output = f"{output}\n{rendered}"
    return output

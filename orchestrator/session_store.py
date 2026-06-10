"""
Session persistence — JSONL session files + AI-generated summaries.

Session storage pattern (also used by Codex code sessions):
    kim_sessions/<date>/<session-id>.jsonl   — incremental JSONL messages
    kim_sessions/<date>/<session-id>.summary.txt — 1-paragraph AI summary

Usage:
    store = SessionStore()
    store.append_message({"role": "user", "content": "..."})
    store.save_summary("User asked Kim to open Chrome and navigate to...")

    # Resume:
    messages = SessionStore.load_session("abc123")

    # Recent context:
    summaries = SessionStore.recent_summaries(count=3)
"""

from __future__ import annotations

import copy
import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

# Default base directory relative to the project root
_DEFAULT_BASE_DIR = Path(__file__).resolve().parent.parent / "kim_sessions"


class SessionStore:
    """
    Manages JSONL session files with incremental append.

    Each session produces two files:
        <base_dir>/<YYYY-MM-DD>/<session_id>.jsonl
        <base_dir>/<YYYY-MM-DD>/<session_id>.summary.txt
    """

    def __init__(
        self,
        base_dir: Optional[Path] = None,
        session_id: Optional[str] = None,
    ) -> None:
        self.base_dir = Path(base_dir) if base_dir else _DEFAULT_BASE_DIR
        self.session_id = session_id or uuid4().hex[:8]

        # When resuming a session, append to the original date dir instead of
        # creating a parallel file under today's date. Otherwise the same
        # session_id ends up split across two .jsonl files (one in the original
        # day's dir, one in today's), which surfaces as a duplicate "new chat"
        # in the sidebar containing only the latest turn.
        existing = SessionStore.find_session_file(self.session_id, base_dir=self.base_dir)
        if existing is not None:
            self.session_dir = existing.parent
            self.session_date = existing.parent.name
        else:
            self.session_date = date.today().isoformat()
            self.session_dir = self.base_dir / self.session_date

        self.session_file = self.session_dir / f"{self.session_id}.jsonl"
        self.summary_file = self.session_dir / f"{self.session_id}.summary.txt"
        self.context_file = self.session_dir / f"{self.session_id}.context.json"
        self._message_count = 0

        # Create directory on first use
        self.session_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            f"SessionStore initialized: {self.session_file} "
            f"(id={self.session_id})"
        )

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def append_message(self, message: dict) -> None:
        """
        Append one message as a JSONL line.

        Base64 image data is stripped to keep files manageable — replaced
        with a placeholder string so the structure is preserved.
        """
        cleaned = _strip_images_for_disk(message)
        line = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))

        with open(self.session_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")

        self._message_count += 1

    def flush(self) -> None:
        """Sync barrier — no-op because append_message opens/closes the file on
        every call.  Exists so callers can call flush() before returning without
        needing to know the write strategy of the underlying store."""

    def append_run_started(self, task: str, cwd: Optional[str] = None) -> None:
        """Append a run_started record to the session JSONL.

        Bookends append_run_result so the session file shows both when a run
        began and when (and how) it ended.  Useful for duration estimation and
        filtering sessions by task text.

        The record has no "role" key so ConversationMemory.load_from_messages
        skips it during session resume (same mechanism as run_result).
        """
        record = {
            "type": "run_started",
            "session_id": self.session_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "task": task,
            "cwd": cwd if cwd is not None else os.getcwd(),
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with open(self.session_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        logger.info("Run started: task=%r", task[:60] if len(task) > 60 else task)

    def append_run_result(self, result: dict, cwd: Optional[str] = None) -> None:
        """Append a typed run_result record to the session JSONL.

        Records why the run ended so the session can be audited or replayed
        without re-running the agent.  Screenshots are not stored (only a
        presence flag), keeping file sizes manageable.

        The record has ``"type": "run_result"`` (no ``"role"`` key) so that
        ConversationMemory.load_from_messages skips it during session resume.
        """
        record = {
            "type": "run_result",
            "session_id": self.session_id,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "success": result.get("success"),
            "termination": result.get("termination"),
            "summary": result.get("summary", ""),
            "had_screenshot": bool(result.get("screenshot")),
            "cwd": cwd if cwd is not None else os.getcwd(),
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with open(self.session_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        logger.info(
            "Run result appended to session: termination=%r success=%r",
            record["termination"],
            record["success"],
        )

    def append_tool_event(
        self,
        tool_name: str,
        phase: str,
        arg_keys: Optional[list] = None,
        duration_ms: Optional[int] = None,
        error: Optional[str] = None,
        error_code: Optional[str] = None,
        risk_level: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> None:
        """Append a typed tool_call trace record to the session JSONL.

        Only argument keys are persisted, never argument values or tool output.
        This keeps traces useful for debugging without recording credentials,
        screenshots, or large command results.

        error_code is a stable classifier code from orchestrator.tool_errors
        (e.g. "permission_denied", "blocked", "timeout") present on errored events.

        risk_level is a stable tier from orchestrator.tool_risk
        ("high", "medium", "low") present on started events.
        """
        record = {
            "type": "tool_call",
            "session_id": self.session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool_name": tool_name,
            "phase": phase,
            "arg_keys": sorted(str(k) for k in (arg_keys or [])),
            "cwd": cwd if cwd is not None else os.getcwd(),
        }
        if duration_ms is not None:
            record["duration_ms"] = int(duration_ms)
        if error:
            record["error"] = str(error)[:500]
        if error_code:
            record["error_code"] = error_code
        if risk_level:
            record["risk_level"] = risk_level
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with open(self.session_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        logger.info(
            "Tool trace appended: tool=%r phase=%r duration_ms=%r",
            tool_name,
            phase,
            duration_ms,
        )

    def append_llm_event(
        self,
        phase: str,
        provider: Optional[str] = None,
        attempt: Optional[int] = None,
        message_count: Optional[int] = None,
        tool_count: Optional[int] = None,
        duration_ms: Optional[int] = None,
        usage: Optional[dict] = None,
        error_code: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> None:
        """Append a typed llm_turn trace record to the session JSONL.

        Prompt text, tool schemas, and model output are intentionally omitted.
        Only counts, timing, usage metadata, and normalized error code are kept.
        """
        record = {
            "type": "llm_turn",
            "session_id": self.session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "phase": phase,
            "cwd": cwd if cwd is not None else os.getcwd(),
        }
        if provider is not None:
            record["provider"] = provider
        if attempt is not None:
            record["attempt"] = int(attempt)
        if message_count is not None:
            record["message_count"] = int(message_count)
        if tool_count is not None:
            record["tool_count"] = int(tool_count)
        if duration_ms is not None:
            record["duration_ms"] = int(duration_ms)
        if usage:
            record["usage"] = _compact_usage_for_trace(usage)
        if error_code:
            record["error_code"] = error_code
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with open(self.session_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        logger.info(
            "LLM trace appended: provider=%r phase=%r attempt=%r duration_ms=%r",
            provider,
            phase,
            attempt,
            duration_ms,
        )

    def append_checkpoint(
        self,
        iteration: int,
        phase: str,
        last_tool_name: Optional[str] = None,
        result_type: Optional[str] = None,
        consecutive_continues: Optional[int] = None,
    ) -> None:
        """Append a lightweight run_checkpoint record to the session JSONL.

        Checkpoints mark where the agent loop was at key points so that future
        durable-execution code can determine where a crashed run left off and
        decide whether resume is safe.

        No prompt text, tool arguments, tool output, or screenshots are stored —
        only iteration metadata.

        Integration point: call from agent.run() at the top of the iteration
        body, after the cancellation check and before the LLM call (~line 534),
        to capture a deterministic single-fire marker per iteration that covers
        all early-exit branches (batch, task_complete intercept, stuck, etc.).
        At that point pass ``last_tool_name`` from the previous iteration and
        ``phase="iteration_start"``.  Example::

            try:
                self._session_store.append_checkpoint(
                    iteration=iteration,
                    phase="iteration_start",
                    last_tool_name=_last_tool_name,
                )
            except Exception:
                pass  # trace write must never abort the agent run
        """
        record: dict = {
            "type": "run_checkpoint",
            "session_id": self.session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "iteration": int(iteration),
            "phase": str(phase),
        }
        if last_tool_name is not None:
            record["last_tool_name"] = str(last_tool_name)
        if result_type is not None:
            record["result_type"] = str(result_type)
        if consecutive_continues is not None:
            record["consecutive_continues"] = int(consecutive_continues)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with open(self.session_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        logger.info(
            "Checkpoint appended: iteration=%d phase=%r last_tool=%r",
            iteration,
            phase,
            last_tool_name,
        )

    def save_summary(self, summary: str) -> None:
        """Write a human-readable summary alongside the JSONL file."""
        self.summary_file.write_text(summary.strip() + "\n", encoding="utf-8")
        logger.info(f"Session summary saved: {self.summary_file}")

    def load_context_state(self) -> dict:
        """Read this session's context-meter sidecar, if present."""
        try:
            if self.context_file.exists():
                data = json.loads(self.context_file.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.debug(f"Failed to read context sidecar {self.context_file}: {e}")
        return {}

    def save_context_state(self, state: dict) -> None:
        """Atomically write this session's context-meter sidecar."""
        if not isinstance(state, dict):
            raise TypeError("context state must be a dict")
        self.session_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.context_file.with_suffix(self.context_file.suffix + ".tmp")
        payload = dict(state)
        payload.setdefault("session_id", self.session_id)
        payload.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.context_file)

    def save_compact_artifact(self, artifact: dict) -> Path:
        """Persist a compact-summary artifact and return its path.

        The artifact is intentionally separate from the rolling context meter so
        fresh chats can point back to the compact checkpoint without carrying the
        full pre-compact transcript in the active thread.
        """
        if not isinstance(artifact, dict):
            raise TypeError("compact artifact must be a dict")
        self.session_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.session_dir / f"{self.session_id}.compact.{stamp}.json"
        payload = dict(artifact)
        payload.setdefault("session_id", self.session_id)
        payload.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        logger.info(f"Compact artifact saved: {path}")
        return path

    # ------------------------------------------------------------------
    # Read API (class methods — work without an active session)
    # ------------------------------------------------------------------

    @staticmethod
    def find_session_file(
        session_id: str,
        base_dir: Optional[Path] = None,
    ) -> Optional[Path]:
        """Return the JSONL path for a session ID if it exists."""
        base = Path(base_dir) if base_dir else _DEFAULT_BASE_DIR
        if not base.exists():
            return None

        for date_dir in sorted(base.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue
            candidate = date_dir / f"{session_id}.jsonl"
            if candidate.exists():
                return candidate
        return None

    @staticmethod
    def session_exists(
        session_id: str,
        base_dir: Optional[Path] = None,
    ) -> bool:
        """Return True if a session JSONL file exists for this ID."""
        return SessionStore.find_session_file(session_id, base_dir=base_dir) is not None

    @staticmethod
    def load_session(
        session_id: str,
        base_dir: Optional[Path] = None,
        warn_if_missing: bool = True,
    ) -> list[dict]:
        """
        Load all messages from a session JSONL file.

        Searches all date directories for the given session_id.
        Returns the messages in order, ready to be loaded into
        ConversationMemory.
        """
        candidate = SessionStore.find_session_file(session_id, base_dir=base_dir)
        if candidate:
            return _read_jsonl(candidate)

        if warn_if_missing:
            logger.info(f"Session not found: {session_id}")
        return []

    @staticmethod
    def load_trace_events(
        session_id: str,
        base_dir: Optional[Path] = None,
        event_type: Optional[str] = None,
        tool_name: Optional[str] = None,
        phase: Optional[str] = None,
    ) -> list[dict]:
        """Load typed trace records from a session JSONL file.

        Trace records are JSONL entries with a "type" key but no "role" key,
        such as run_started, tool_call, and run_result.  Optional filters allow
        callers to inspect a specific event type, tool name, or tool phase
        without replaying the session.
        """
        records = []
        for record in SessionStore.load_session(session_id, base_dir=base_dir, warn_if_missing=False):
            if "role" in record:
                continue
            record_type = record.get("type")
            if not record_type:
                continue
            if event_type is not None and record_type != event_type:
                continue
            if tool_name is not None and record.get("tool_name") != tool_name:
                continue
            if phase is not None and record.get("phase") != phase:
                continue
            records.append(record)
        return records

    @staticmethod
    def iter_trace_events(
        base_dir: Optional[Path] = None,
        event_type: Optional[str] = None,
        tool_name: Optional[str] = None,
        phase: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        """Load typed trace records across sessions, newest date dirs first.

        Adds ``session_id`` and ``date`` when missing so records can be traced
        back to their JSONL source.  This is a lightweight query layer over the
        existing inspectable files, not a separate index.
        """
        base = Path(base_dir) if base_dir else _DEFAULT_BASE_DIR
        if not base.exists():
            return []

        events = []
        for date_dir in sorted(base.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue
            for session_file in sorted(date_dir.glob("*.jsonl"), reverse=True):
                session_id = session_file.stem
                for record in _read_jsonl(session_file):
                    if "role" in record:
                        continue
                    record_type = record.get("type")
                    if not record_type:
                        continue
                    if event_type is not None and record_type != event_type:
                        continue
                    if tool_name is not None and record.get("tool_name") != tool_name:
                        continue
                    if phase is not None and record.get("phase") != phase:
                        continue

                    event = dict(record)
                    event.setdefault("session_id", session_id)
                    event.setdefault("date", date_dir.name)
                    events.append(event)
                    if limit is not None and len(events) >= limit:
                        return events
        return events

    @staticmethod
    def load_checkpoints(
        session_id: str,
        base_dir: Optional[Path] = None,
    ) -> list:
        """Return all run_checkpoint records for a session in file order.

        Uses the existing load_trace_events filter so no new I/O path is
        introduced.  Returns an empty list for missing or empty sessions.
        """
        return SessionStore.load_trace_events(
            session_id,
            base_dir=base_dir,
            event_type="run_checkpoint",
        )

    @staticmethod
    def latest_checkpoint(
        session_id: str,
        base_dir: Optional[Path] = None,
    ) -> Optional[dict]:
        """Return the most recent run_checkpoint record, or None.

        The most recent checkpoint is the last entry in file order, which
        corresponds to the highest iteration number when checkpoints are written
        once per iteration in forward order.
        """
        checkpoints = SessionStore.load_checkpoints(session_id, base_dir=base_dir)
        return checkpoints[-1] if checkpoints else None

    @staticmethod
    def summarize_trace_events(
        session_id: Optional[str] = None,
        base_dir: Optional[Path] = None,
    ) -> dict:
        """Return a compact summary of trace records for one session or all sessions.

        For single-session summaries (session_id provided) ``run.success`` and
        ``run.termination`` reflect the sole run_result record.  For cross-session
        summaries they reflect the *most recently completed* run (highest
        ``completed_at`` timestamp), so the values are always well-defined
        regardless of session-file ordering within a date directory.
        """
        if session_id:
            events = SessionStore.load_trace_events(session_id, base_dir=base_dir)
        else:
            events = SessionStore.iter_trace_events(base_dir=base_dir)

        summary = {
            "session_id": session_id,
            "event_count": len(events),
            "by_type": {},
            "tool_calls": {
                "started": 0,
                "completed": 0,
                "errored": 0,
                "by_tool": {},
                "error_codes": {},
                "risk_levels": {},
            },
            "llm_turns": {
                "started": 0,
                "completed": 0,
                "errored": 0,
                "by_provider": {},
                "error_codes": {},
            },
            "run": {
                "started": 0,
                "result": 0,
                "success": None,
                "termination": None,
            },
        }
        _latest_run_completed_at: Optional[str] = None

        for event in events:
            event_type = event.get("type", "unknown")
            summary["by_type"][event_type] = summary["by_type"].get(event_type, 0) + 1

            if event_type == "run_started":
                summary["run"]["started"] += 1
            elif event_type == "run_result":
                summary["run"]["result"] += 1
                completed_at = event.get("completed_at") or ""
                if _latest_run_completed_at is None or completed_at > _latest_run_completed_at:
                    _latest_run_completed_at = completed_at
                    summary["run"]["success"] = event.get("success")
                    summary["run"]["termination"] = event.get("termination")
            elif event_type == "tool_call":
                phase = event.get("phase", "unknown")
                if phase in summary["tool_calls"]:
                    summary["tool_calls"][phase] += 1
                tool_name = event.get("tool_name", "unknown")
                by_tool = summary["tool_calls"]["by_tool"]
                by_tool[tool_name] = by_tool.get(tool_name, 0) + 1
                error_code = event.get("error_code")
                if error_code:
                    ec = summary["tool_calls"]["error_codes"]
                    ec[error_code] = ec.get(error_code, 0) + 1
                risk_level = event.get("risk_level")
                if risk_level:
                    rl = summary["tool_calls"]["risk_levels"]
                    rl[risk_level] = rl.get(risk_level, 0) + 1
            elif event_type == "llm_turn":
                phase = event.get("phase", "unknown")
                if phase in summary["llm_turns"]:
                    summary["llm_turns"][phase] += 1
                provider = event.get("provider", "unknown")
                by_provider = summary["llm_turns"]["by_provider"]
                by_provider[provider] = by_provider.get(provider, 0) + 1
                error_code = event.get("error_code")
                if error_code:
                    error_codes = summary["llm_turns"]["error_codes"]
                    error_codes[error_code] = error_codes.get(error_code, 0) + 1

        return summary

    @staticmethod
    def recent_summaries(
        count: int = 3,
        base_dir: Optional[Path] = None,
    ) -> list[dict]:
        """
        Read the last N session summaries, newest first.

        Returns list of:
            {"session_id": str, "date": str, "summary": str}
        """
        base = Path(base_dir) if base_dir else _DEFAULT_BASE_DIR
        if not base.exists():
            return []

        summaries = []
        # Walk date directories newest first
        for date_dir in sorted(base.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue
            for summary_file in sorted(date_dir.glob("*.summary.txt"), reverse=True):
                session_id = summary_file.stem.replace(".summary", "")
                try:
                    text = summary_file.read_text(encoding="utf-8").strip()
                    if text:
                        summaries.append({
                            "session_id": session_id,
                            "date": date_dir.name,
                            "summary": text,
                        })
                except Exception as e:
                    logger.debug(f"Failed to read summary {summary_file}: {e}")

                if len(summaries) >= count:
                    return summaries

        return summaries

    @staticmethod
    def list_sessions(
        base_dir: Optional[Path] = None,
    ) -> list[dict]:
        """
        List all sessions with metadata.

        Returns list of:
            {"session_id": str, "date": str, "path": str,
             "message_count": int, "has_summary": bool}
        """
        base = Path(base_dir) if base_dir else _DEFAULT_BASE_DIR
        if not base.exists():
            return []

        sessions = []
        for date_dir in sorted(base.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue
            for jsonl_file in sorted(date_dir.glob("*.jsonl"), reverse=True):
                session_id = jsonl_file.stem
                summary_file = date_dir / f"{session_id}.summary.txt"
                context_file = date_dir / f"{session_id}.context.json"
                try:
                    with open(jsonl_file, encoding="utf-8") as f:
                        msg_count = sum(1 for _ in f)
                except Exception:
                    msg_count = 0

                sessions.append({
                    "session_id": session_id,
                    "date": date_dir.name,
                    "path": str(jsonl_file),
                    "message_count": msg_count,
                    "has_summary": summary_file.exists(),
                    "has_context": context_file.exists(),
                })

        return sessions


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _strip_images_for_disk(message: dict) -> dict:
    """
    Deep-copy a message and replace base64 image data with a placeholder.
    This keeps JSONL files from ballooning to hundreds of megabytes.
    """
    content = message.get("content")
    if content is None:
        return message

    # Simple string content — nothing to strip
    if isinstance(content, str):
        # Remove internal metadata keys
        return {k: v for k, v in message.items() if not k.startswith("_")}

    # List content (multimodal) — strip image blocks
    if isinstance(content, list):
        cleaned = copy.deepcopy(message)
        new_content = []
        for item in cleaned["content"]:
            if isinstance(item, dict) and item.get("type") == "image":
                new_content.append({
                    "type": "text",
                    "text": "(screenshot — stripped for disk)",
                })
            else:
                new_content.append(item)
        cleaned["content"] = new_content
        # Remove internal metadata keys
        return {k: v for k, v in cleaned.items() if not k.startswith("_")}

    return {k: v for k, v in message.items() if not k.startswith("_")}


def _read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file into a list of dicts."""
    messages = []
    with open(path, encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError as e:
                logger.warning(
                    f"Skipping malformed JSONL line {line_num} in {path}: {e}"
                )
    return messages


def _compact_usage_for_trace(usage: dict) -> dict:
    """Return a compact, JSON-safe subset of provider usage metadata."""
    if not isinstance(usage, dict):
        return {}

    keep_keys = {
        "input",
        "input_tokens",
        "prompt_tokens",
        "output",
        "output_tokens",
        "completion_tokens",
        "total",
        "total_tokens",
        "cache_creation_tokens",
        "cache_read_tokens",
        "estimated",
        "estimate",
        "is_estimate",
        "source",
        "provider",
        "model",
    }
    compact = {}
    for key in keep_keys:
        if key not in usage:
            continue
        value = usage.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            compact[key] = value
    return compact

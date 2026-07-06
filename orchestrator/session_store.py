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
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

# Default base directory relative to the project root
_DEFAULT_BASE_DIR = Path(__file__).resolve().parent.parent / "kim_sessions"

# Rotate the active JSONL when it exceeds this size (finding 4: size cap)
_MAX_SESSION_BYTES = 50 * 1024 * 1024  # 50 MB

# The prune screenshot-strip pass rewrites session files in place. A resumed
# session appends into its ORIGINAL (old) date dir, so a file touched within
# this window may still be live — skip it to avoid racing a concurrent append
# and dropping messages (finding 4.1).
_STRIP_SKIP_RECENT_SECONDS = 3600  # 1 hour


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
        if session_id:
            # Explicit id (resume, or caller-chosen): used as-is; the block
            # below adopts its existing date dir.
            self.session_id = session_id
        else:
            # Fresh session: pick an 8-hex id that does not already exist on
            # disk, so a birthday collision in the ~4.3B space cannot silently
            # append this session into an unrelated one and merge transcripts on
            # resume (finding 4.2). Fall back to a full-length id in the
            # astronomically unlikely event every probe collides.
            self.session_id = uuid4().hex
            for _ in range(1000):
                candidate = uuid4().hex[:8]
                if SessionStore.find_session_file(candidate, base_dir=self.base_dir) is None:
                    self.session_id = candidate
                    break

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
        # Lock that serialises all append writes within this process (finding 3)
        self._lock: threading.Lock = threading.Lock()

        # Create directory on first use
        self.session_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            f"SessionStore initialized: {self.session_file} "
            f"(id={self.session_id})"
        )

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def _append_line(self, line: str) -> None:
        """Write a single JSONL line with a process-level lock, flush, and fsync.

        Uses a threading.Lock so two threads sharing the same SessionStore
        instance cannot interleave writes (finding 3).  Also rotates the
        session file when it exceeds _MAX_SESSION_BYTES so individual sessions
        cannot grow without bound (finding 4).  Rolled files keep the session
        date-dir so age-based pruning covers them automatically.
        """
        with self._lock:
            # Rotate before writing if the current file is over the size cap
            if (
                self.session_file.exists()
                and self.session_file.stat().st_size >= _MAX_SESSION_BYTES
            ):
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                rolled = self.session_dir / f"{self.session_id}.roll.{stamp}.jsonl"
                try:
                    self.session_file.rename(rolled)
                    logger.info(
                        "Session file rotated: %s -> %s", self.session_file.name, rolled.name
                    )
                except OSError as exc:
                    logger.warning("Could not rotate session file %s: %s", self.session_file, exc)

            with open(self.session_file, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())

    def append_message(self, message: dict) -> None:
        """
        Append one message as a JSONL line.

        Base64 image data is stripped to keep files manageable — replaced
        with a placeholder string so the structure is preserved.
        """
        cleaned = _strip_images_for_disk(message)
        line = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))
        self._append_line(line)
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
        self._append_line(line)
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
        self._append_line(line)
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
        self._append_line(line)
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
        record: dict[str, object] = {
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
        self._append_line(line)
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
        self._append_line(line)
        logger.info(
            "Checkpoint appended: iteration=%d phase=%r last_tool=%r",
            iteration,
            phase,
            last_tool_name,
        )

    def save_summary(self, summary: str) -> None:
        """Write a human-readable summary alongside the JSONL file (atomic)."""
        tmp = self.summary_file.with_suffix(self.summary_file.suffix + ".tmp")
        tmp.write_text(summary.strip() + "\n", encoding="utf-8")
        os.replace(tmp, self.summary_file)
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
        """Return the JSONL path for a session ID if it exists.

        Also returns the expected live-file path when only rolled segments
        exist (i.e. every append crossed the 50 MB cap and the live file has
        been rotated away) so callers can still resolve the session directory.
        """
        base = Path(base_dir) if base_dir else _DEFAULT_BASE_DIR
        if not base.exists():
            return None

        for date_dir in sorted(base.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue
            candidate = date_dir / f"{session_id}.jsonl"
            if candidate.exists():
                return candidate
            # Session may consist entirely of rolled segments — return the
            # expected live-file path so callers can locate the date directory.
            if any(date_dir.glob(f"{session_id}.roll.*.jsonl")):
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
        Rolled segments (<id>.roll.<stamp>.jsonl, produced when the live file
        exceeds the 50 MB cap) are concatenated in chronological stamp order
        before the live file so the full transcript is recovered on resume.
        Returns the messages in order, ready to be loaded into
        ConversationMemory.
        """
        candidate = SessionStore.find_session_file(session_id, base_dir=base_dir)
        if candidate is None:
            if warn_if_missing:
                logger.info(f"Session not found: {session_id}")
            return []

        session_dir = candidate.parent
        # Collect rolled segments in chronological order (stamp sorts lexicographically)
        roll_files = sorted(session_dir.glob(f"{session_id}.roll.*.jsonl"))
        messages: list[dict] = []
        for roll_file in roll_files:
            messages.extend(_read_jsonl(roll_file))
        # Append the live file if it exists (may not exist if every write was rolled)
        if candidate.exists():
            messages.extend(_read_jsonl(candidate))
        return messages

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
                if ".roll." in session_file.name:
                    continue  # rolled segments are read via load_session, not enumerated separately
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
            # L1: within a day, order by mtime (newest first). The filename is a
            # random-hex session id, so sorting by name gave an arbitrary order
            # and "recent context" was not actually the newest session.
            for summary_file in sorted(
                date_dir.glob("*.summary.txt"),
                key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
                reverse=True,
            ):
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
                if ".roll." in jsonl_file.name:
                    continue  # rolled segments are not standalone sessions
                session_id = jsonl_file.stem
                summary_file = date_dir / f"{session_id}.summary.txt"
                context_file = date_dir / f"{session_id}.context.json"
                try:
                    msg_count = 0
                    with open(jsonl_file, encoding="utf-8") as f:
                        for _line in f:
                            _stripped = _line.strip()
                            if _stripped:
                                try:
                                    if "role" in json.loads(_stripped):
                                        msg_count += 1
                                except json.JSONDecodeError:
                                    pass
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

    @staticmethod
    def prune_old_sessions(
        max_age_days: int = 30,
        screenshot_strip_age_days: int = 2,
        base_dir: Optional[Path] = None,
    ) -> dict:
        """
        Retention policy:
          - Strip screenshot payloads from sessions older than `screenshot_strip_age_days`.
          - Delete session files (JSONL + summary + context) older than `max_age_days`.

        Returns {"stripped": int, "deleted": int} counts.
        """
        from datetime import timedelta
        base = Path(base_dir) if base_dir else _DEFAULT_BASE_DIR
        if not base.exists():
            return {"stripped": 0, "deleted": 0}

        today = date.today()
        strip_cutoff = today - timedelta(days=screenshot_strip_age_days)
        delete_cutoff = today - timedelta(days=max_age_days)

        stripped = 0
        deleted = 0

        for date_dir in sorted(base.iterdir()):
            if not date_dir.is_dir():
                continue
            try:
                dir_date = date.fromisoformat(date_dir.name)
            except ValueError:
                continue  # skip non-date dirs

            for jsonl_file in date_dir.glob("*.jsonl"):
                if dir_date <= delete_cutoff:
                    # Delete the session entirely
                    session_id = jsonl_file.stem
                    for ext in (".jsonl", ".summary.txt", ".context.json"):
                        candidate = date_dir / f"{session_id}{ext}"
                        if candidate.exists():
                            try:
                                candidate.unlink()
                            except OSError as e:
                                logger.warning(f"Could not delete {candidate}: {e}")
                    # Also remove compact artifacts (finding 1: save_compact_artifact
                    # writes <id>.compact.<stamp>.json which were never pruned)
                    for compact_file in date_dir.glob(f"{session_id}.compact.*.json"):
                        try:
                            compact_file.unlink()
                        except OSError as e:
                            logger.warning(f"Could not delete {compact_file}: {e}")
                    deleted += 1
                elif dir_date <= strip_cutoff:
                    # Skip files touched recently: a resumed session appends into
                    # its ORIGINAL date dir, so a read-all → rewrite → replace
                    # here could race a live append and drop messages written
                    # between the read and the rename (finding 4.1). A truly-dead
                    # old session won't have a fresh mtime.
                    try:
                        if (time.time() - jsonl_file.stat().st_mtime) < _STRIP_SKIP_RECENT_SECONDS:
                            continue
                    except OSError:
                        pass
                    # Strip screenshot data in place
                    try:
                        messages = _read_jsonl(jsonl_file)
                        has_images = any(
                            isinstance(m.get("content"), list)
                            and any(
                                isinstance(b, dict) and b.get("type") == "image"
                                for b in m.get("content", [])
                            )
                            for m in messages
                        )
                        if has_images:
                            stripped_messages = [_strip_images_for_disk(m) for m in messages]
                            tmp = jsonl_file.with_suffix(".jsonl.tmp")
                            with open(tmp, "w", encoding="utf-8") as f:
                                for msg in stripped_messages:
                                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")
                            tmp.replace(jsonl_file)
                            stripped += 1
                    except Exception as e:
                        logger.warning(f"Could not strip screenshots from {jsonl_file}: {e}")

            # Remove empty date dirs
            try:
                if not any(date_dir.iterdir()):
                    date_dir.rmdir()
            except OSError:
                pass

        return {"stripped": stripped, "deleted": deleted}

    @staticmethod
    def delete_all_sessions(base_dir: Optional[Path] = None) -> int:
        """
        Delete all session data. Returns the number of session files removed.

        Used by Settings → Data → "Delete all my data".
        """
        import shutil
        base = Path(base_dir) if base_dir else _DEFAULT_BASE_DIR
        if not base.exists():
            return 0

        count = 0
        for date_dir in base.iterdir():
            if not date_dir.is_dir():
                continue
            count += sum(1 for _ in date_dir.glob("*.jsonl"))
            try:
                shutil.rmtree(date_dir)
            except OSError as e:
                logger.warning(f"Could not remove session dir {date_dir}: {e}")

        logger.info(f"delete_all_sessions: removed {count} session files")
        return count


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
        # Strip internal metadata keys even when there is no content (finding 5)
        return {k: v for k, v in message.items() if not k.startswith("_")}

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

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

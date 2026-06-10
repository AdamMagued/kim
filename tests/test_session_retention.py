"""
Tests for session retention pruning (P1-5).

Tests:
  - prune_old_sessions strips screenshots from sessions older than the cutoff
  - prune_old_sessions deletes session files older than max_age_days
  - prune_old_sessions removes empty date dirs
  - delete_all_sessions removes all session data
  - delete_all_sessions returns correct count
"""
from __future__ import annotations

import json
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from orchestrator.session_store import SessionStore


def _write_session(base: Path, session_date: date, session_id: str, messages: list[dict]) -> Path:
    """Helper: write a JSONL session file at the given date."""
    date_dir = base / session_date.isoformat()
    date_dir.mkdir(parents=True, exist_ok=True)
    session_file = date_dir / f"{session_id}.jsonl"
    with open(session_file, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg) + "\n")
    return session_file


def _make_message_with_screenshot() -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abc123"}},
        ],
    }


def _make_text_message() -> dict:
    return {"role": "user", "content": "hello world"}


class TestPruneOldSessions:

    def test_strip_screenshots_older_than_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            old_date = date.today() - timedelta(days=3)
            path = _write_session(base, old_date, "abc", [_make_message_with_screenshot()])

            result = SessionStore.prune_old_sessions(
                max_age_days=30, screenshot_strip_age_days=2, base_dir=base
            )

            assert result["stripped"] == 1
            messages = [json.loads(line) for line in path.read_text().splitlines() if line]
            content = messages[0]["content"]
            assert isinstance(content, list)
            assert content[0]["type"] == "text"
            assert "stripped" in content[0]["text"]

    def test_no_strip_for_recent_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            recent_date = date.today()
            path = _write_session(base, recent_date, "abc", [_make_message_with_screenshot()])
            original_content = path.read_text()

            result = SessionStore.prune_old_sessions(
                max_age_days=30, screenshot_strip_age_days=2, base_dir=base
            )

            assert result["stripped"] == 0
            assert path.read_text() == original_content

    def test_delete_sessions_older_than_max_age(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            old_date = date.today() - timedelta(days=31)
            path = _write_session(base, old_date, "abc", [_make_text_message()])

            result = SessionStore.prune_old_sessions(
                max_age_days=30, screenshot_strip_age_days=2, base_dir=base
            )

            assert result["deleted"] == 1
            assert not path.exists()

    def test_preserve_sessions_within_retention(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            recent_date = date.today() - timedelta(days=5)
            path = _write_session(base, recent_date, "abc", [_make_text_message()])

            result = SessionStore.prune_old_sessions(
                max_age_days=30, screenshot_strip_age_days=2, base_dir=base
            )

            assert result["deleted"] == 0
            assert path.exists()

    def test_no_crash_on_empty_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "nonexistent"
            result = SessionStore.prune_old_sessions(base_dir=base)
            assert result == {"stripped": 0, "deleted": 0}

    def test_non_screenshot_messages_not_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            old_date = date.today() - timedelta(days=3)
            path = _write_session(base, old_date, "abc", [_make_text_message()])
            original = path.read_text()

            SessionStore.prune_old_sessions(
                max_age_days=30, screenshot_strip_age_days=2, base_dir=base
            )

            assert path.exists()
            assert path.read_text() == original  # unchanged (no screenshots to strip)


class TestDeleteAllSessions:

    def test_delete_all_returns_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for i in range(3):
                d = date.today() - timedelta(days=i)
                _write_session(base, d, f"session{i}", [_make_text_message()])

            count = SessionStore.delete_all_sessions(base_dir=base)
            assert count == 3

    def test_delete_all_removes_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_session(base, date.today(), "abc", [_make_text_message()])

            SessionStore.delete_all_sessions(base_dir=base)

            assert not any(base.rglob("*.jsonl"))

    def test_delete_all_empty_dir_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "nonexistent"
            count = SessionStore.delete_all_sessions(base_dir=base)
            assert count == 0

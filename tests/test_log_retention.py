"""Tests for apply_log_retention() in mcp_server/logger.py."""
from __future__ import annotations

import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from mcp_server.logger import apply_log_retention


def _utc_today() -> date:
    """Today in UTC.

    apply_log_retention() (and the logger that names files) both work in UTC
    (datetime.now(timezone.utc).date()). Using local ``date.today()`` here made
    the age-relative tests flake near the UTC/local date boundary — e.g. a file
    dated (local_today - 8) is only (utc_today - 7) when the local date has
    already ticked over, so it lands exactly on the keep boundary and is not
    deleted. Anchor the test's reference date to UTC to match the code.
    """
    return datetime.now(timezone.utc).date()


def _write_log(log_dir: Path, log_date: date) -> Path:
    """Write a minimal .jsonl log file for the given date."""
    f = log_dir / f"kim_{log_date.isoformat()}.jsonl"
    f.write_text('{"level":"INFO","message":"test"}\n', encoding="utf-8")
    return f


class TestApplyLogRetention:

    def test_deletes_old_log_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            old = _write_log(log_dir, _utc_today() - timedelta(days=8))
            deleted = apply_log_retention(log_dir=str(log_dir), keep_days=7)
            assert deleted == 1
            assert not old.exists()

    def test_keeps_recent_log_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            recent = _write_log(log_dir, _utc_today())
            deleted = apply_log_retention(log_dir=str(log_dir), keep_days=7)
            assert deleted == 0
            assert recent.exists()

    def test_keeps_log_on_boundary(self):
        """A log exactly `keep_days` old (not strictly older) is preserved."""
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            boundary = _write_log(log_dir, _utc_today() - timedelta(days=7))
            deleted = apply_log_retention(log_dir=str(log_dir), keep_days=7)
            assert deleted == 0
            assert boundary.exists()

    def test_returns_zero_for_nonexistent_dir(self):
        deleted = apply_log_retention(log_dir="/tmp/kim_logs_nonexistent_xyz", keep_days=7)
        assert deleted == 0

    def test_ignores_non_matching_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            other = log_dir / "something_else.log"
            other.write_text("irrelevant\n")
            deleted = apply_log_retention(log_dir=str(log_dir), keep_days=7)
            assert deleted == 0
            assert other.exists()

    def test_deletes_multiple_old_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            for offset in (10, 15, 20):
                _write_log(log_dir, _utc_today() - timedelta(days=offset))
            _write_log(log_dir, _utc_today() - timedelta(days=3))  # recent, kept
            deleted = apply_log_retention(log_dir=str(log_dir), keep_days=7)
            assert deleted == 3

    def test_returns_count_matches_deletions(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            _write_log(log_dir, _utc_today() - timedelta(days=9))
            _write_log(log_dir, _utc_today() - timedelta(days=11))
            deleted = apply_log_retention(log_dir=str(log_dir), keep_days=7)
            assert deleted == 2
            remaining = list(log_dir.glob("kim_*.jsonl"))
            assert len(remaining) == 0

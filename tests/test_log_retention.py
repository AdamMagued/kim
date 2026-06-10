"""Tests for apply_log_retention() in mcp_server/logger.py."""
from __future__ import annotations

import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from mcp_server.logger import apply_log_retention


def _write_log(log_dir: Path, log_date: date) -> Path:
    """Write a minimal .jsonl log file for the given date."""
    f = log_dir / f"kim_{log_date.isoformat()}.jsonl"
    f.write_text('{"level":"INFO","message":"test"}\n', encoding="utf-8")
    return f


class TestApplyLogRetention:

    def test_deletes_old_log_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            old = _write_log(log_dir, date.today() - timedelta(days=8))
            deleted = apply_log_retention(log_dir=str(log_dir), keep_days=7)
            assert deleted == 1
            assert not old.exists()

    def test_keeps_recent_log_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            recent = _write_log(log_dir, date.today())
            deleted = apply_log_retention(log_dir=str(log_dir), keep_days=7)
            assert deleted == 0
            assert recent.exists()

    def test_keeps_log_on_boundary(self):
        """A log exactly `keep_days` old (not strictly older) is preserved."""
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            boundary = _write_log(log_dir, date.today() - timedelta(days=7))
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
                _write_log(log_dir, date.today() - timedelta(days=offset))
            _write_log(log_dir, date.today() - timedelta(days=3))  # recent, kept
            deleted = apply_log_retention(log_dir=str(log_dir), keep_days=7)
            assert deleted == 3

    def test_returns_count_matches_deletions(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            _write_log(log_dir, date.today() - timedelta(days=9))
            _write_log(log_dir, date.today() - timedelta(days=11))
            deleted = apply_log_retention(log_dir=str(log_dir), keep_days=7)
            assert deleted == 2
            remaining = list(log_dir.glob("kim_*.jsonl"))
            assert len(remaining) == 0

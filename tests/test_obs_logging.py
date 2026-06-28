"""Regression tests for orchestrator/obs_logging.py.

Guards the three observable behaviours of init_logging():
  1. LogRecords carry run_id / session_id after init_logging().
  2. The test saves and restores the global LogRecord factory (no leak).
  3. Empty string IDs are safe (no exception, records still build).
"""

from __future__ import annotations

import logging

import pytest

import orchestrator.obs_logging as obs_logging


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record() -> logging.LogRecord:
    """Return a freshly created LogRecord using the currently installed factory."""
    return logging.getLogger("test_obs_logging").makeRecord(
        "test_obs_logging", logging.INFO, __file__, 0, "msg", (), None
    )


# ---------------------------------------------------------------------------
# Per-test isolation: reset module-level globals so tests are independent
# regardless of execution order.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_obs_logging_globals(monkeypatch):
    """Reset _INITIALIZED and _current_ids before every test in this module."""
    monkeypatch.setattr(obs_logging, "_INITIALIZED", False)
    # Replace the list object so the factory closure sees a fresh one.
    monkeypatch.setattr(obs_logging, "_current_ids", ["", ""])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_init_logging_attaches_run_and_session_id():
    """After init_logging(run_id='r1', session_id='s1') every new LogRecord
    carries run_id='r1' and session_id='s1' as attributes."""
    original_factory = logging.getLogRecordFactory()
    try:
        obs_logging.init_logging(run_id="r1", session_id="s1")
        record = _make_record()
        assert record.run_id == "r1"      # type: ignore[attr-defined]
        assert record.session_id == "s1"  # type: ignore[attr-defined]
    finally:
        logging.setLogRecordFactory(original_factory)


def test_factory_restored_in_teardown():
    """The global LogRecord factory must be saved before and restored after
    init_logging() so the mutation does not leak to unrelated tests."""
    original_factory = logging.getLogRecordFactory()
    try:
        obs_logging.init_logging(run_id="leak-check", session_id="leak-check")
        # The factory must have changed after init_logging().
        assert logging.getLogRecordFactory() is not original_factory
    finally:
        # Explicit teardown — this is the pattern every caller must follow.
        logging.setLogRecordFactory(original_factory)

    # After restoration the factory is back to the original.
    assert logging.getLogRecordFactory() is original_factory


def test_empty_ids_safe():
    """init_logging('', '') must not raise and LogRecords must still be
    constructible with empty string attributes."""
    original_factory = logging.getLogRecordFactory()
    try:
        obs_logging.init_logging("", "")   # must not raise
        record = _make_record()            # must not raise
        assert hasattr(record, "run_id")
        assert hasattr(record, "session_id")
        assert record.run_id == ""         # type: ignore[attr-defined]
        assert record.session_id == ""     # type: ignore[attr-defined]
    finally:
        logging.setLogRecordFactory(original_factory)

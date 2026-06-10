"""
Tests for the run_checkpoint durable-execution layer in SessionStore.

All tests use temporary directories — no real agents, providers, or network
connections are required.
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path

from orchestrator.session_store import SessionStore


# ---------------------------------------------------------------------------
# append_checkpoint — write path
# ---------------------------------------------------------------------------

def test_append_checkpoint_writes_typed_record():
    """append_checkpoint must append a {"type": "run_checkpoint"} line."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        store = SessionStore(base_dir=base, session_id="ckpt_basic")
        store.append_checkpoint(iteration=1, phase="iteration_start")

        lines = store.session_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["type"] == "run_checkpoint"


def test_checkpoint_record_required_fields():
    """Checkpoint must carry session_id, timestamp, iteration, and phase."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        store = SessionStore(base_dir=base, session_id="ckpt_fields")
        store.append_checkpoint(iteration=3, phase="iteration_start")

        record = json.loads(store.session_file.read_text(encoding="utf-8").strip())
        assert record["session_id"] == "ckpt_fields"
        assert record["iteration"] == 3
        assert record["phase"] == "iteration_start"
        assert "timestamp" in record


def test_checkpoint_timestamp_is_parseable_iso_utc():
    """timestamp must be a valid ISO-8601 datetime string with UTC tzinfo."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        store = SessionStore(base_dir=base, session_id="ckpt_ts")
        store.append_checkpoint(iteration=1, phase="iteration_start")

        record = json.loads(store.session_file.read_text(encoding="utf-8").strip())
        dt = datetime.fromisoformat(record["timestamp"])
        assert dt.tzinfo is not None


def test_checkpoint_optional_fields_present_when_supplied():
    """last_tool_name, result_type, and consecutive_continues are written when provided."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        store = SessionStore(base_dir=base, session_id="ckpt_optional")
        store.append_checkpoint(
            iteration=2,
            phase="iteration_start",
            last_tool_name="run_command",
            result_type="tool_call",
            consecutive_continues=0,
        )

        record = json.loads(store.session_file.read_text(encoding="utf-8").strip())
        assert record["last_tool_name"] == "run_command"
        assert record["result_type"] == "tool_call"
        assert record["consecutive_continues"] == 0


def test_checkpoint_optional_fields_absent_when_omitted():
    """Optional fields must be absent from the record when not supplied."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        store = SessionStore(base_dir=base, session_id="ckpt_absent")
        store.append_checkpoint(iteration=1, phase="iteration_start")

        record = json.loads(store.session_file.read_text(encoding="utf-8").strip())
        assert "last_tool_name" not in record
        assert "result_type" not in record
        assert "consecutive_continues" not in record


def test_checkpoint_contains_no_sensitive_payload():
    """Checkpoint records must not store tool args, output, content, or screenshots."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        store = SessionStore(base_dir=base, session_id="ckpt_safe")
        store.append_checkpoint(
            iteration=1,
            phase="iteration_start",
            last_tool_name="run_command",
            result_type="tool_call",
            consecutive_continues=0,
        )

        text = store.session_file.read_text(encoding="utf-8")
        record = json.loads(text.strip())

        allowed_keys = {
            "type", "session_id", "timestamp", "iteration", "phase",
            "last_tool_name", "result_type", "consecutive_continues",
        }
        extra_keys = set(record.keys()) - allowed_keys
        assert extra_keys == set(), f"Unexpected keys in checkpoint record: {extra_keys}"
        # Verify no arg content, command text, or image data appears
        assert "args" not in text
        assert "content" not in text
        assert "screenshot" not in text
        assert "output" not in text


def test_multiple_checkpoints_are_appended_in_order():
    """Each iteration appends one checkpoint; file order equals iteration order."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        store = SessionStore(base_dir=base, session_id="ckpt_order")
        for i in range(1, 5):
            store.append_checkpoint(iteration=i, phase="iteration_start",
                                    last_tool_name=f"tool_{i - 1}" if i > 1 else None)

        lines = store.session_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 4
        for idx, line in enumerate(lines, start=1):
            record = json.loads(line)
            assert record["iteration"] == idx


# ---------------------------------------------------------------------------
# load_checkpoints — read path
# ---------------------------------------------------------------------------

def test_load_checkpoints_returns_only_checkpoint_records():
    """load_checkpoints filters to run_checkpoint records only."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        store = SessionStore(base_dir=base, session_id="ckpt_load")
        store.append_run_started("do the thing")
        store.append_message({"role": "user", "content": "Task: do the thing"})
        store.append_checkpoint(iteration=1, phase="iteration_start")
        store.append_tool_event("run_command", "completed", duration_ms=10)
        store.append_checkpoint(iteration=2, phase="iteration_start",
                                last_tool_name="run_command")
        store.append_run_result({"success": True, "termination": "task_complete",
                                 "summary": "done", "screenshot": ""})

        checkpoints = SessionStore.load_checkpoints("ckpt_load", base_dir=base)
        assert len(checkpoints) == 2
        assert all(c["type"] == "run_checkpoint" for c in checkpoints)
        assert checkpoints[0]["iteration"] == 1
        assert checkpoints[1]["iteration"] == 2
        assert checkpoints[1]["last_tool_name"] == "run_command"


def test_load_checkpoints_missing_session_returns_empty():
    """Missing sessions return an empty list without raising or warning."""
    with tempfile.TemporaryDirectory() as tmp:
        checkpoints = SessionStore.load_checkpoints("no_such_session", base_dir=Path(tmp))
        assert checkpoints == []


def test_load_checkpoints_session_with_no_checkpoints_returns_empty():
    """Sessions that wrote no checkpoints return an empty list."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        store = SessionStore(base_dir=base, session_id="ckpt_none")
        store.append_run_started("no checkpoints")
        store.append_run_result({"success": True, "termination": "task_complete",
                                 "summary": "done", "screenshot": ""})

        checkpoints = SessionStore.load_checkpoints("ckpt_none", base_dir=base)
        assert checkpoints == []


# ---------------------------------------------------------------------------
# latest_checkpoint — convenience accessor
# ---------------------------------------------------------------------------

def test_latest_checkpoint_returns_last_record():
    """latest_checkpoint returns the checkpoint with the highest iteration."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        store = SessionStore(base_dir=base, session_id="ckpt_latest")
        store.append_checkpoint(iteration=1, phase="iteration_start")
        store.append_checkpoint(iteration=2, phase="iteration_start",
                                last_tool_name="read_file")
        store.append_checkpoint(iteration=3, phase="iteration_start",
                                last_tool_name="write_file")

        latest = SessionStore.latest_checkpoint("ckpt_latest", base_dir=base)
        assert latest is not None
        assert latest["iteration"] == 3
        assert latest["last_tool_name"] == "write_file"


def test_latest_checkpoint_returns_none_for_missing_session():
    """latest_checkpoint returns None when no session exists."""
    with tempfile.TemporaryDirectory() as tmp:
        result = SessionStore.latest_checkpoint("ghost", base_dir=Path(tmp))
        assert result is None


def test_latest_checkpoint_returns_none_when_no_checkpoints_written():
    """latest_checkpoint returns None when the session exists but has no checkpoints."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        store = SessionStore(base_dir=base, session_id="ckpt_zero")
        store.append_run_started("no checkpoints")
        store.append_run_result({"success": True, "termination": "task_complete",
                                 "summary": "done", "screenshot": ""})

        result = SessionStore.latest_checkpoint("ckpt_zero", base_dir=base)
        assert result is None


# ---------------------------------------------------------------------------
# ConversationMemory skip test (codebase convention for every trace type)
# ---------------------------------------------------------------------------

def test_load_from_messages_skips_run_checkpoint_records():
    """ConversationMemory.load_from_messages must ignore run_checkpoint records on resume.

    A run_checkpoint record written into a session JSONL must not appear in the
    message stack when the session is resumed — load_from_messages filters on
    presence of 'role', so any record without it is dropped automatically.
    """
    from orchestrator.memory import ConversationMemory

    mem = ConversationMemory()
    records = [
        {"type": "run_started", "task": "do the task"},
        {"role": "user", "content": "Task: do the task"},
        {"type": "run_checkpoint", "session_id": "x", "iteration": 1,
         "phase": "iteration_start", "timestamp": "2026-06-06T00:00:00+00:00"},
        {"role": "assistant", "content": "TASK_COMPLETE: done"},
        {"type": "run_checkpoint", "session_id": "x", "iteration": 2,
         "phase": "iteration_start", "last_tool_name": "run_command",
         "timestamp": "2026-06-06T00:01:00+00:00"},
        {"type": "run_result", "session_id": "x", "success": True,
         "termination": "task_complete"},
    ]
    mem.load_from_messages(records)
    msgs = mem.get_messages()

    assert len(msgs) == 2
    assert all("role" in m for m in msgs)
    assert not any(m.get("type") == "run_checkpoint" for m in msgs)


# ---------------------------------------------------------------------------
# Integration with existing trace query path
# ---------------------------------------------------------------------------

def test_load_trace_events_can_filter_by_run_checkpoint_type():
    """load_trace_events with event_type='run_checkpoint' returns only checkpoints."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        store = SessionStore(base_dir=base, session_id="ckpt_filter")
        store.append_run_started("filter test")
        store.append_checkpoint(iteration=1, phase="iteration_start")
        store.append_tool_event("read_file", "completed", duration_ms=2)
        store.append_checkpoint(iteration=2, phase="iteration_start",
                                last_tool_name="read_file")

        events = SessionStore.load_trace_events(
            "ckpt_filter", base_dir=base, event_type="run_checkpoint"
        )
        assert len(events) == 2
        assert all(e["type"] == "run_checkpoint" for e in events)


def test_summarize_trace_events_counts_run_checkpoint_in_by_type():
    """summarize_trace_events must count run_checkpoint records under by_type."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        store = SessionStore(base_dir=base, session_id="ckpt_summary")
        store.append_run_started("summary test")
        store.append_checkpoint(iteration=1, phase="iteration_start")
        store.append_checkpoint(iteration=2, phase="iteration_start",
                                last_tool_name="run_command")
        store.append_run_result({"success": True, "termination": "task_complete",
                                 "summary": "done", "screenshot": ""})

        summary = SessionStore.summarize_trace_events("ckpt_summary", base_dir=base)
        assert summary["by_type"]["run_checkpoint"] == 2
        assert summary["event_count"] == 4  # run_started + 2 checkpoints + run_result


def test_kimctl_trace_json_includes_run_checkpoint_in_by_type(capsys, monkeypatch, tmp_path):
    """kimctl trace --json surfaces run_checkpoint in by_type with no CLI changes."""
    import os
    from argparse import Namespace
    from kimctl.__main__ import cmd_trace

    base = tmp_path
    store = SessionStore(base_dir=base, session_id="ckpt_cli")
    store.append_checkpoint(iteration=1, phase="iteration_start")
    store.append_checkpoint(iteration=2, phase="iteration_start",
                            last_tool_name="write_file")

    monkeypatch.setenv("KIM_SESSIONS_DIR", str(base))
    cmd_trace(Namespace(session_id="ckpt_cli", json=True))

    data = json.loads(capsys.readouterr().out)
    assert data["by_type"]["run_checkpoint"] == 2
    assert data["event_count"] == 2

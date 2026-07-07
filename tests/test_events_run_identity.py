"""Run-identity envelope tests for the schema-generated Python IPC emitter.

Verifies the RUN-IDENTITY fix: every event carries the owning run_id +
session_id (read from KIM_RUN_ID / KIM_SESSION_ID) so the desktop frontend can
file a run's output under ITS session regardless of the currently-viewed view,
while staying byte-for-byte backward compatible when the env is unset.
"""

from __future__ import annotations

import json

import pytest

from orchestrator import events_gen


@pytest.fixture(autouse=True)
def _clear_identity_env(monkeypatch):
    monkeypatch.delenv("KIM_RUN_ID", raising=False)
    monkeypatch.delenv("KIM_SESSION_ID", raising=False)


def _emit_and_read(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_no_env_keeps_legacy_wire_shape(capsys):
    """Backward compatibility: no env -> no envelope, identical bytes."""
    events_gen.emit_status("working")
    events_gen.emit_answer("done")
    assert _emit_and_read(capsys) == [
        {"type": "status", "message": "working"},
        {"type": "answer", "text": "done"},
    ]


def test_env_stamps_run_and_session_on_every_event(capsys, monkeypatch):
    monkeypatch.setenv("KIM_RUN_ID", "sessA-123")
    monkeypatch.setenv("KIM_SESSION_ID", "sessA")
    events_gen.emit_status("working")
    events_gen.emit_tool("read_file", {})
    events_gen.emit_answer("done")
    for line in _emit_and_read(capsys):
        assert line["run_id"] == "sessA-123"
        assert line["session_id"] == "sessA"


def test_partial_env_stamps_only_present_ids(capsys, monkeypatch):
    monkeypatch.setenv("KIM_SESSION_ID", "sessB")
    events_gen.emit_status("working")
    (line,) = _emit_and_read(capsys)
    assert line["session_id"] == "sessB"
    assert "run_id" not in line


def test_envelope_never_overrides_a_real_payload_field(capsys, monkeypatch):
    """A payload that legitimately owns a field named run_id keeps its value."""
    monkeypatch.setenv("KIM_RUN_ID", "sessC-9")
    monkeypatch.setenv("KIM_SESSION_ID", "sessC")
    events_gen.emit_event("custom", run_id="payload-wins")
    (line,) = _emit_and_read(capsys)
    assert line["run_id"] == "payload-wins"
    assert line["session_id"] == "sessC"


def test_envelope_keys_are_last(capsys, monkeypatch):
    """Envelope keys append after the payload so type/message stay first."""
    monkeypatch.setenv("KIM_RUN_ID", "r1")
    monkeypatch.setenv("KIM_SESSION_ID", "s1")
    events_gen.emit_status("hi")
    line = capsys.readouterr().out.splitlines()[0]
    assert line.index('"type"') < line.index('"message"') < line.index('"run_id"')

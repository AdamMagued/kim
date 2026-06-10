"""
Tests for `kimctl trace` subcommand.

All tests use temporary directories and fake session files — no real agents,
providers, or bridge connections are required.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from argparse import Namespace
from pathlib import Path

import pytest

from kimctl.__main__ import build_parser, cmd_trace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, separators=(",", ":")) for r in records) + "\n",
        encoding="utf-8",
    )


def _make_args(session_id=None, json_out=False):
    return Namespace(session_id=session_id, json=json_out)


def _run_trace(args, sessions_dir: Path, capsys):
    """Run cmd_trace with KIM_SESSIONS_DIR pointed at a temp dir."""
    old = os.environ.get("KIM_SESSIONS_DIR")
    os.environ["KIM_SESSIONS_DIR"] = str(sessions_dir)
    try:
        cmd_trace(args)
    finally:
        if old is None:
            os.environ.pop("KIM_SESSIONS_DIR", None)
        else:
            os.environ["KIM_SESSIONS_DIR"] = old


# ---------------------------------------------------------------------------
# Parser wiring
# ---------------------------------------------------------------------------

def test_trace_parser_registered():
    """build_parser must include a 'trace' subcommand."""
    p = build_parser()
    args = p.parse_args(["trace"])
    assert args.command == "trace"
    assert args.session_id is None
    assert args.json is False


def test_trace_parser_with_session_id():
    p = build_parser()
    args = p.parse_args(["trace", "abc12345"])
    assert args.session_id == "abc12345"


def test_trace_parser_json_flag():
    p = build_parser()
    args = p.parse_args(["trace", "--json"])
    assert args.json is True


def test_trace_parser_session_and_json():
    p = build_parser()
    args = p.parse_args(["trace", "abc12345", "--json"])
    assert args.session_id == "abc12345"
    assert args.json is True


# ---------------------------------------------------------------------------
# JSON output — single session
# ---------------------------------------------------------------------------

def test_trace_json_single_session(capsys):
    """--json output for a single session is valid JSON with expected keys."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_jsonl(base / "2026-06-05" / "sess01.jsonl", [
            {"type": "run_started", "task": "do stuff"},
            {"role": "user", "content": "do stuff"},
            {"type": "tool_call", "tool_name": "run_command", "phase": "started"},
            {"type": "tool_call", "tool_name": "run_command", "phase": "completed", "duration_ms": 5},
            {"type": "run_result", "completed_at": "2026-06-05T10:00:00+00:00",
             "success": True, "termination": "task_complete"},
        ])

        _run_trace(_make_args("sess01", json_out=True), base, capsys)
        out = capsys.readouterr().out
        data = json.loads(out)

    assert data["session_id"] == "sess01"
    assert data["event_count"] == 4  # role records excluded
    assert data["by_type"]["run_started"] == 1
    assert data["by_type"]["tool_call"] == 2
    assert data["by_type"]["run_result"] == 1
    assert data["tool_calls"]["started"] == 1
    assert data["tool_calls"]["completed"] == 1
    assert data["run"]["success"] is True
    assert data["run"]["termination"] == "task_complete"


def test_trace_json_all_sessions(capsys):
    """--json output without session_id summarizes across all sessions."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_jsonl(base / "2026-06-05" / "s1.jsonl", [
            {"type": "tool_call", "tool_name": "run_command", "phase": "completed"},
        ])
        _write_jsonl(base / "2026-06-04" / "s2.jsonl", [
            {"type": "llm_turn", "phase": "completed", "provider": "OllamaProvider"},
        ])

        _run_trace(_make_args(None, json_out=True), base, capsys)
        out = capsys.readouterr().out
        data = json.loads(out)

    assert data["session_id"] is None
    assert data["event_count"] == 2
    assert data["by_type"]["tool_call"] == 1
    assert data["by_type"]["llm_turn"] == 1
    assert data["tool_calls"]["by_tool"]["run_command"] == 1
    assert data["llm_turns"]["by_provider"]["OllamaProvider"] == 1


def test_trace_json_error_codes_and_risk_levels(capsys):
    """Summary includes error_codes and risk_levels when present."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_jsonl(base / "2026-06-05" / "enrich.jsonl", [
            {"type": "tool_call", "tool_name": "run_command", "phase": "started",
             "risk_level": "high"},
            {"type": "tool_call", "tool_name": "run_command", "phase": "errored",
             "error": "BLOCKED", "error_code": "blocked"},
            {"type": "tool_call", "tool_name": "read_file", "phase": "started",
             "risk_level": "low"},
            {"type": "tool_call", "tool_name": "read_file", "phase": "completed",
             "duration_ms": 3},
        ])

        _run_trace(_make_args("enrich", json_out=True), base, capsys)
        data = json.loads(capsys.readouterr().out)

    assert data["tool_calls"]["error_codes"]["blocked"] == 1
    assert data["tool_calls"]["risk_levels"]["high"] == 1
    assert data["tool_calls"]["risk_levels"]["low"] == 1


def test_trace_json_empty_base_dir_all_sessions(capsys):
    """All-sessions summary on an empty directory returns zero counts."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "no_sessions"
        # base does not exist yet — should be handled gracefully
        _run_trace(_make_args(None, json_out=True), base, capsys)
        data = json.loads(capsys.readouterr().out)

    assert data["event_count"] == 0
    assert data["session_id"] is None


# ---------------------------------------------------------------------------
# Human-readable output
# ---------------------------------------------------------------------------

def test_trace_human_readable_single_session(capsys):
    """Human-readable output includes header, counts, and run outcome."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_jsonl(base / "2026-06-05" / "abc1.jsonl", [
            {"type": "run_started", "task": "human test"},
            {"type": "tool_call", "tool_name": "run_command", "phase": "started"},
            {"type": "tool_call", "tool_name": "run_command", "phase": "errored",
             "error": "BLOCKED", "error_code": "blocked"},
            {"type": "llm_turn", "phase": "completed", "provider": "OllamaProvider"},
            {"type": "run_result", "completed_at": "2026-06-05T09:00:00+00:00",
             "success": False, "termination": "cancelled"},
        ])

        _run_trace(_make_args("abc1"), base, capsys)
        out = capsys.readouterr().out

    assert "abc1" in out
    assert "Events:" in out
    assert "Tool calls:" in out
    assert "LLM turns:" in out
    assert "Run:" in out
    assert "False" in out
    assert "cancelled" in out


def test_trace_human_readable_all_sessions(capsys):
    """Human-readable output without session_id shows 'all sessions' label."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_jsonl(base / "2026-06-05" / "xyz.jsonl", [
            {"type": "tool_call", "tool_name": "web_open", "phase": "completed"},
        ])

        _run_trace(_make_args(None), base, capsys)
        out = capsys.readouterr().out

    assert "all sessions" in out
    assert "Events:" in out


# ---------------------------------------------------------------------------
# Missing session error path
# ---------------------------------------------------------------------------

def test_trace_missing_session_exits_nonzero(capsys):
    """cmd_trace exits with a non-zero code when the session is not found."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        with pytest.raises(SystemExit) as exc_info:
            _run_trace(_make_args("ghost_session"), base, capsys)
    assert exc_info.value.code != 0


def test_trace_missing_session_json_error(capsys):
    """Missing session with --json emits {"ok": false, "error": ...} to stdout."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        with pytest.raises(SystemExit):
            _run_trace(_make_args("ghost_session", json_out=True), base, capsys)
        out = capsys.readouterr().out
        data = json.loads(out)
    assert data["ok"] is False
    assert "ghost_session" in data["error"]


# ---------------------------------------------------------------------------
# Idempotency / read-only guarantee
# ---------------------------------------------------------------------------

def test_trace_does_not_write_files(capsys):
    """cmd_trace must not create or modify any files in the sessions directory."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        date_dir = base / "2026-06-05"
        date_dir.mkdir(parents=True)
        session_file = date_dir / "ro.jsonl"
        _write_jsonl(session_file, [
            {"type": "run_started", "task": "read only check"},
        ])
        mtime_before = session_file.stat().st_mtime
        files_before = set(base.rglob("*"))

        _run_trace(_make_args("ro", json_out=True), base, capsys)

        files_after = set(base.rglob("*"))
        assert files_after == files_before, "cmd_trace created unexpected files"
        assert session_file.stat().st_mtime == mtime_before, "cmd_trace modified session file"


# ---------------------------------------------------------------------------
# LLM error_codes in summary
# ---------------------------------------------------------------------------

def test_trace_json_llm_error_codes(capsys):
    """LLM error_codes are counted and exposed in summary."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_jsonl(base / "2026-06-05" / "llm_ec.jsonl", [
            {"type": "llm_turn", "phase": "started", "provider": "BrowserProvider"},
            {"type": "llm_turn", "phase": "errored", "provider": "BrowserProvider",
             "error_code": "auth"},
            {"type": "llm_turn", "phase": "started", "provider": "BrowserProvider"},
            {"type": "llm_turn", "phase": "errored", "provider": "BrowserProvider",
             "error_code": "rate_limit"},
        ])

        _run_trace(_make_args("llm_ec", json_out=True), base, capsys)
        data = json.loads(capsys.readouterr().out)

    assert data["llm_turns"]["error_codes"]["auth"] == 1
    assert data["llm_turns"]["error_codes"]["rate_limit"] == 1
    assert data["llm_turns"]["errored"] == 2

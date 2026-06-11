"""
Tests for kimctl compare subcommand.

All tests inject a fake _session_factory so no real MCP server or provider
credentials are needed.  Two test layers:
  - Namespace-based: call cmd_compare with a hand-crafted argparse.Namespace
    to verify handler logic without subprocess overhead.
  - Parser-wiring: call build_parser().parse_args([...]) to verify that argparse
    dest names and flag names match what cmd_compare expects.
"""
from __future__ import annotations

import json
from argparse import Namespace
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from kimctl.__main__ import build_parser, cmd_compare


# ---------------------------------------------------------------------------
# Fake session factory (no real MCP server)
# ---------------------------------------------------------------------------

def _success_factory(providers=None, summary="done"):
    result_map = {
        p: {"success": True, "summary": summary, "termination": "task_complete"}
        for p in (providers or ["ollama", "claude", "browser", "gemini"])
    }

    @asynccontextmanager
    async def _factory(config, provider_name):
        class _FakeAgent:
            async def run(self, task):
                return result_map.get(
                    provider_name,
                    {"success": True, "summary": "ok", "termination": "task_complete"},
                )
        yield _FakeAgent()

    return _factory


def _fail_factory(provider_name: str, message: str = "provider error"):
    @asynccontextmanager
    async def _factory(config, name):
        if name == provider_name:
            raise RuntimeError(message)

        class _FakeAgent:
            async def run(self, task):
                return {"success": True, "summary": "ok", "termination": "task_complete"}

        yield _FakeAgent()

    return _factory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ns(**kwargs) -> Namespace:
    defaults = {
        "task": "check disk usage",
        "providers": "ollama,claude",
        "timeout": 30.0,
        "save_dir": None,
        "no_save": True,  # suppress disk writes by default in tests
        "config": None,
        "json": False,
        "_session_factory": None,
    }
    defaults.update(kwargs)
    return Namespace(**defaults)


def _run_compare(capsys, **kwargs):
    """Run cmd_compare via namespace and return (stdout, stderr)."""
    args = _ns(**kwargs)
    cmd_compare(args)
    cap = capsys.readouterr()
    return cap.out, cap.err


# ---------------------------------------------------------------------------
# Basic success cases
# ---------------------------------------------------------------------------

def test_compare_prints_table(capsys):
    out, _ = _run_compare(
        capsys,
        providers="ollama,claude",
        _session_factory=_success_factory(["ollama", "claude"], summary="disk: 42%"),
    )
    assert "ollama" in out
    assert "claude" in out
    assert "yes" in out


def test_compare_prints_saved_path(capsys, tmp_path):
    """Human-readable output includes 'Saved: <path>' when a file was written."""
    out, _ = _run_compare(
        capsys,
        providers="ollama",
        no_save=False,
        save_dir=str(tmp_path),
        _session_factory=_success_factory(["ollama"]),
    )
    assert "Saved:" in out
    assert str(tmp_path) in out


def test_compare_no_save_omits_saved_line(capsys):
    """Human-readable output has no 'Saved:' line when --no-save is set."""
    out, _ = _run_compare(
        capsys,
        providers="ollama",
        no_save=True,
        _session_factory=_success_factory(["ollama"]),
    )
    assert "Saved:" not in out


def test_compare_json_output(capsys):
    out, _ = _run_compare(
        capsys,
        providers="ollama",
        json=True,
        _session_factory=_success_factory(["ollama"]),
    )
    data = json.loads(out)
    assert data["ok"] is True
    assert len(data["results"]) == 1
    assert data["results"][0]["provider"] == "ollama"
    assert data["results"][0]["success"] is True


def test_compare_json_output_includes_saved_key(capsys, tmp_path):
    """JSON output always includes a 'saved' key (path string or null)."""
    out, _ = _run_compare(
        capsys,
        providers="ollama",
        json=True,
        no_save=False,
        save_dir=str(tmp_path),
        _session_factory=_success_factory(["ollama"]),
    )
    data = json.loads(out)
    assert "saved" in data
    assert data["saved"] is not None
    assert str(tmp_path) in data["saved"]


def test_compare_json_no_save_null_key(capsys):
    """JSON output has saved=null when --no-save is set."""
    out, _ = _run_compare(
        capsys,
        providers="ollama",
        json=True,
        no_save=True,
        _session_factory=_success_factory(["ollama"]),
    )
    data = json.loads(out)
    assert data["saved"] is None


def test_compare_multiple_providers_ordered(capsys):
    out, _ = _run_compare(
        capsys,
        providers="claude,ollama",
        json=True,
        _session_factory=_success_factory(["claude", "ollama"]),
    )
    data = json.loads(out)
    assert [r["provider"] for r in data["results"]] == ["claude", "ollama"]


def test_compare_partial_failure_still_returns_all(capsys):
    out, _ = _run_compare(
        capsys,
        providers="ollama,bad-provider",
        json=True,
        _session_factory=_fail_factory("bad-provider", "no creds"),
    )
    data = json.loads(out)
    assert data["ok"] is True
    assert len(data["results"]) == 2
    assert data["results"][0]["success"] is True
    assert data["results"][1]["success"] is False
    assert "no creds" in data["results"][1]["error"]


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def test_compare_empty_providers_exits(capsys):
    with pytest.raises(SystemExit) as exc:
        cmd_compare(_ns(providers=""))
    assert exc.value.code == 1
    cap = capsys.readouterr()
    assert "providers" in cap.err.lower()


def test_compare_empty_providers_json_error(capsys):
    with pytest.raises(SystemExit) as exc:
        cmd_compare(_ns(providers="", json=True))
    assert exc.value.code == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert "providers" in data["error"].lower()


def test_compare_blank_provider_name_exits(capsys):
    with pytest.raises(SystemExit) as exc:
        cmd_compare(_ns(providers="ollama,   ", _session_factory=_success_factory()))
    assert exc.value.code == 1
    assert "provider names" in capsys.readouterr().err.lower()


def test_compare_duplicate_providers_exits(capsys):
    with pytest.raises(SystemExit) as exc:
        cmd_compare(_ns(providers="ollama,OLLAMA", _session_factory=_success_factory()))
    assert exc.value.code == 1
    assert "duplicate providers" in capsys.readouterr().err.lower()


def test_compare_too_many_providers_exits(capsys):
    nine = ",".join(f"p{i}" for i in range(9))
    with pytest.raises(SystemExit) as exc:
        cmd_compare(_ns(providers=nine, _session_factory=_success_factory()))
    assert exc.value.code == 1


def test_compare_too_many_providers_json_error(capsys):
    nine = ",".join(f"p{i}" for i in range(9))
    with pytest.raises(SystemExit) as exc:
        cmd_compare(_ns(providers=nine, json=True, _session_factory=_success_factory()))
    assert exc.value.code == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert "8" in data["error"]


# ---------------------------------------------------------------------------
# Save-dir options
# ---------------------------------------------------------------------------

def test_compare_no_save_suppresses_file(capsys, tmp_path):
    _run_compare(
        capsys,
        providers="ollama",
        no_save=True,
        _session_factory=_success_factory(["ollama"]),
    )
    assert list(tmp_path.glob("compare_*.json")) == []


def test_compare_saves_to_save_dir(capsys, tmp_path):
    _run_compare(
        capsys,
        providers="ollama",
        no_save=False,
        save_dir=str(tmp_path),
        _session_factory=_success_factory(["ollama"]),
    )
    files = list(tmp_path.glob("compare_*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["task"] == "check disk usage"


# ---------------------------------------------------------------------------
# Parser wiring: verify argparse dest names match what cmd_compare uses
# ---------------------------------------------------------------------------

def test_parser_wiring_basic():
    parser = build_parser()
    args = parser.parse_args([
        "compare",
        "--task", "check logs",
        "--providers", "ollama,claude",
        "--timeout", "45",
        "--no-save",
        "--json",
    ])
    assert args.command == "compare"
    assert args.task == "check logs"
    assert args.providers == "ollama,claude"
    assert args.timeout == 45.0
    assert args.no_save is True
    assert args.json is True


def test_parser_wiring_save_dir():
    parser = build_parser()
    args = parser.parse_args([
        "compare",
        "--task", "t",
        "--providers", "ollama",
        "--save-dir", "/tmp/kim_cmp",
    ])
    assert args.save_dir == "/tmp/kim_cmp"
    assert args.no_save is False


def test_parser_wiring_config():
    parser = build_parser()
    args = parser.parse_args([
        "compare",
        "--task", "t",
        "--providers", "ollama",
        "--config", "/etc/kim/config.yaml",
    ])
    assert args.config == "/etc/kim/config.yaml"


def test_parser_wiring_defaults():
    parser = build_parser()
    args = parser.parse_args(["compare", "--task", "t", "--providers", "ollama"])
    assert args.timeout == 120.0
    assert args.no_save is False
    assert args.save_dir is None
    assert args.config is None
    assert args.json is False

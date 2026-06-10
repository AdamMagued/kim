"""
Tests for kimctl schedule subcommands.

All tests are local-only (no bridge/HTTP).  An isolated CronStore is provided
via KIM_SCHEDULES_FILE env var so the real kim_schedules.json is never touched.

Two layers of coverage:
  - Namespace-based: call cmd_schedule with a hand-crafted argparse.Namespace
    to verify handler logic (fast, no subprocess).
  - Parser-wiring: call build_parser().parse_args([...]) + cmd_schedule to
    verify that argparse dest names, positionals, and flag names match what
    the handlers expect (catches mismatches the Namespace tests would miss).
"""
from __future__ import annotations

import json
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kimctl.__main__ import build_parser, cmd_schedule


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def schedule_store(tmp_path, monkeypatch):
    """Provide an isolated schedule store path via env var."""
    path = tmp_path / "test_schedules.json"
    monkeypatch.setenv("KIM_SCHEDULES_FILE", str(path))
    return path


def _ns(**kwargs) -> Namespace:
    """Build a Namespace with all schedule-command defaults set."""
    defaults = {
        "schedule_action": None,
        "json": False,
        "enabled_only": False,
        "task": None,
        "id": None,
        "provider": None,
        "disabled": False,
        "enable": False,
        "disable": False,
        "task_text": None,
        "expr": None,
        "as_of": None,
        "at": None,
        "dry_run": False,
    }
    defaults.update(kwargs)
    return Namespace(**defaults)


def _add_json(task: str, expr: str, capsys, **kwargs) -> dict:
    """Add a task via cmd_schedule --json and return the parsed response dict."""
    args = _ns(schedule_action="add", task=task, expr=expr, json=True, **kwargs)
    cmd_schedule(args)
    return json.loads(capsys.readouterr().out)


def _add_id(task: str, expr: str, capsys, **kwargs) -> str:
    """Add a task and return its ID."""
    return _add_json(task, expr, capsys, **kwargs)["task"]["id"]


# ---------------------------------------------------------------------------
# Namespace-based tests: list
# ---------------------------------------------------------------------------

def test_list_empty_human(schedule_store, capsys):
    cmd_schedule(_ns(schedule_action="list"))
    assert "No scheduled tasks" in capsys.readouterr().out


def test_list_empty_json(schedule_store, capsys):
    cmd_schedule(_ns(schedule_action="list", json=True))
    assert json.loads(capsys.readouterr().out) == []


def test_list_shows_added_task(schedule_store, capsys):
    _add_id("check disk usage", "@daily", capsys)
    cmd_schedule(_ns(schedule_action="list"))
    out = capsys.readouterr().out
    assert "check disk usage" in out
    assert "@daily" in out


def test_list_enabled_only_filters_disabled(schedule_store, capsys):
    _add_id("active task", "@hourly", capsys)
    _add_id("disabled task", "@daily", capsys, disabled=True)

    cmd_schedule(_ns(schedule_action="list", enabled_only=True))
    out = capsys.readouterr().out
    assert "active task" in out
    assert "disabled task" not in out


# ---------------------------------------------------------------------------
# Namespace-based tests: add
# ---------------------------------------------------------------------------

def test_add_returns_ok_json(schedule_store, capsys):
    data = _add_json("ping check", "@hourly", capsys)
    assert data["ok"] is True
    t = data["task"]
    assert t["task"] == "ping check"
    assert t["schedule_expr"] == "@hourly"
    assert t["enabled"] is True
    assert t["run_count"] == 0
    assert t["id"]


def test_add_disabled_flag(schedule_store, capsys):
    data = _add_json("silent task", "@weekly", capsys, disabled=True)
    assert data["task"]["enabled"] is False


def test_add_invalid_expr_exits_1(schedule_store, capsys):
    with pytest.raises(SystemExit) as exc:
        cmd_schedule(_ns(schedule_action="add", task="t", expr="@monthly"))
    assert exc.value.code == 1
    assert "Unrecognised" in capsys.readouterr().err


def test_add_invalid_expr_json_reports_error(schedule_store, capsys):
    with pytest.raises(SystemExit) as exc:
        cmd_schedule(_ns(schedule_action="add", task="t", expr="@monthly", json=True))
    assert exc.value.code == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert "Unrecognised" in data["error"]


def test_add_openai_provider_warns(schedule_store, capsys):
    cmd_schedule(_ns(schedule_action="add", task="t", expr="@daily", provider="openai"))
    capsys.readouterr()  # clear stdout
    # Re-run to capture warning (warning printed to stderr during add)
    cmd_schedule(_ns(schedule_action="add", task="t2", expr="@daily", provider="gpt-5"))
    err = capsys.readouterr().err
    assert "Warning" in err
    assert "constraint" in err


def test_add_browser_provider_no_warning(schedule_store, capsys):
    cmd_schedule(_ns(schedule_action="add", task="t", expr="@daily", provider="browser"))
    err = capsys.readouterr().err
    assert "Warning" not in err


@pytest.mark.parametrize("provider", ["claude", "gemini", "deepseek", "anthropic"])
def test_add_non_openai_forbidden_provider_warns(schedule_store, capsys, provider):
    """Providers like claude/gemini/deepseek must also warn at add time.

    Before the fix, _warn_if_forbidden_provider only checked for 'openai'/'gpt'
    prefixes, so these providers were stored silently and then skipped forever
    at runtime — giving the user no indication of the problem at add time.
    """
    cmd_schedule(_ns(schedule_action="add", task="t", expr="@daily", provider=provider))
    err = capsys.readouterr().err
    assert "Warning" in err, f"expected warning for provider {provider!r}, got: {err!r}"
    assert "constraint" in err


# ---------------------------------------------------------------------------
# Namespace-based tests: update
# ---------------------------------------------------------------------------

def test_update_task_text(schedule_store, capsys):
    tid = _add_id("old text", "@daily", capsys)
    cmd_schedule(_ns(schedule_action="update", id=tid, task_text="new text", json=True))
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["task"]["task"] == "new text"


def test_update_enable_disable_cycle(schedule_store, capsys):
    tid = _add_id("t", "@daily", capsys, disabled=True)

    cmd_schedule(_ns(schedule_action="update", id=tid, enable=True, json=True))
    assert json.loads(capsys.readouterr().out)["task"]["enabled"] is True

    cmd_schedule(_ns(schedule_action="update", id=tid, disable=True, json=True))
    assert json.loads(capsys.readouterr().out)["task"]["enabled"] is False


def test_update_enable_and_disable_conflict(schedule_store, capsys):
    tid = _add_id("t", "@daily", capsys)
    with pytest.raises(SystemExit) as exc:
        cmd_schedule(_ns(schedule_action="update", id=tid, enable=True, disable=True))
    assert exc.value.code == 1
    assert "--enable and --disable" in capsys.readouterr().err


def test_update_nothing_to_update_exits(schedule_store, capsys):
    tid = _add_id("t", "@daily", capsys)
    with pytest.raises(SystemExit) as exc:
        cmd_schedule(_ns(schedule_action="update", id=tid))
    assert exc.value.code == 1
    assert "Nothing to update" in capsys.readouterr().err


def test_update_missing_id_exits(schedule_store, capsys):
    with pytest.raises(SystemExit) as exc:
        cmd_schedule(_ns(schedule_action="update", id="ghost", task_text="x", json=True))
    assert exc.value.code == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert "not found" in data["error"]


# ---------------------------------------------------------------------------
# Namespace-based tests: delete
# ---------------------------------------------------------------------------

def test_delete_existing(schedule_store, capsys):
    tid = _add_id("to remove", "@hourly", capsys)
    cmd_schedule(_ns(schedule_action="delete", id=tid, json=True))
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["id"] == tid

    # confirm gone
    cmd_schedule(_ns(schedule_action="list", json=True))
    assert json.loads(capsys.readouterr().out) == []


def test_delete_missing_exits(schedule_store, capsys):
    with pytest.raises(SystemExit) as exc:
        cmd_schedule(_ns(schedule_action="delete", id="phantom", json=True))
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


# ---------------------------------------------------------------------------
# Namespace-based tests: due
# ---------------------------------------------------------------------------

def test_due_empty(schedule_store, capsys):
    cmd_schedule(_ns(schedule_action="due", as_of="2026-06-05T12:00:00+00:00"))
    assert "No tasks due" in capsys.readouterr().out


def test_due_json_returns_list(schedule_store, capsys):
    cmd_schedule(_ns(schedule_action="due", json=True))
    assert isinstance(json.loads(capsys.readouterr().out), list)


def test_due_returns_overdue_task(schedule_store, monkeypatch, capsys):
    """A task run in the past with next_run_at in the past shows up in due."""
    from orchestrator.cron_store import CronStore
    import os
    store = CronStore(store_file=Path(os.environ["KIM_SCHEDULES_FILE"]))
    t = store.add("check disk", "@daily")
    # Record a run far in the past so next_run_at is well before now.
    ran = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    store.record_run(t.id, ran_at=ran)  # next_run_at = 2026-01-02T10:00:00+00:00

    as_of = "2026-06-05T12:00:00+00:00"
    cmd_schedule(_ns(schedule_action="due", as_of=as_of, json=True))
    tasks = json.loads(capsys.readouterr().out)
    assert len(tasks) == 1
    assert tasks[0]["id"] == t.id


def test_due_invalid_as_of_exits(schedule_store, capsys):
    with pytest.raises(SystemExit) as exc:
        cmd_schedule(_ns(schedule_action="due", as_of="not-a-date"))
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# Namespace-based tests: record-run
# ---------------------------------------------------------------------------

def test_record_run_basic(schedule_store, capsys):
    tid = _add_id("t", "@hourly", capsys)
    cmd_schedule(_ns(schedule_action="record-run", id=tid, at="2026-06-05T10:00:00+00:00", json=True))
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["task"]["run_count"] == 1
    assert "10:00:00" in data["task"]["last_run_at"]
    assert "11:00:00" in data["task"]["next_run_at"]


def test_record_run_missing_exits(schedule_store, capsys):
    with pytest.raises(SystemExit) as exc:
        cmd_schedule(_ns(schedule_action="record-run", id="ghost", json=True))
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_record_run_invalid_at_exits(schedule_store, capsys):
    tid = _add_id("t", "@daily", capsys)
    with pytest.raises(SystemExit) as exc:
        cmd_schedule(_ns(schedule_action="record-run", id=tid, at="bad-date"))
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# Namespace-based tests: unknown/missing action
# ---------------------------------------------------------------------------

def test_no_action_exits(schedule_store, capsys):
    with pytest.raises(SystemExit) as exc:
        cmd_schedule(_ns(schedule_action=None))
    assert exc.value.code == 1
    assert "Usage" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Parser-wiring tests: verify argparse dest names match handler getattr calls
# ---------------------------------------------------------------------------

def test_parser_wiring_list(schedule_store, capsys):
    ns = build_parser().parse_args(["schedule", "list", "--json"])
    cmd_schedule(ns)
    assert isinstance(json.loads(capsys.readouterr().out), list)


def test_parser_wiring_list_enabled_only(schedule_store, capsys):
    # First add a disabled task via Namespace so we have something to filter
    _add_id("disabled task", "@daily", capsys, disabled=True)
    _add_id("enabled task", "@hourly", capsys)
    # Use real parser for --enabled-only
    ns = build_parser().parse_args(["schedule", "list", "--enabled-only", "--json"])
    cmd_schedule(ns)
    tasks = json.loads(capsys.readouterr().out)
    ids_enabled = [t["enabled"] for t in tasks]
    assert all(ids_enabled)
    assert len(tasks) == 1


def test_parser_wiring_add(schedule_store, capsys):
    ns = build_parser().parse_args(
        ["schedule", "add", "my task text", "@every 30m", "--provider", "ollama", "--json"]
    )
    cmd_schedule(ns)
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    t = data["task"]
    assert t["task"] == "my task text"
    assert t["schedule_expr"] == "@every 30m"
    assert t["provider"] == "ollama"
    assert t["enabled"] is True


def test_parser_wiring_add_disabled(schedule_store, capsys):
    ns = build_parser().parse_args(["schedule", "add", "t", "@daily", "--disabled", "--json"])
    cmd_schedule(ns)
    assert json.loads(capsys.readouterr().out)["task"]["enabled"] is False


def test_parser_wiring_update(schedule_store, capsys):
    tid = _add_id("original", "@daily", capsys)
    ns = build_parser().parse_args(
        ["schedule", "update", tid, "--task", "updated text", "--expr", "@weekly", "--json"]
    )
    cmd_schedule(ns)
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["task"]["task"] == "updated text"
    assert data["task"]["schedule_expr"] == "@weekly"


def test_parser_wiring_update_enable(schedule_store, capsys):
    tid = _add_id("t", "@daily", capsys, disabled=True)
    ns = build_parser().parse_args(["schedule", "update", tid, "--enable", "--json"])
    cmd_schedule(ns)
    assert json.loads(capsys.readouterr().out)["task"]["enabled"] is True


def test_parser_wiring_delete(schedule_store, capsys):
    tid = _add_id("to delete", "@hourly", capsys)
    ns = build_parser().parse_args(["schedule", "delete", tid, "--json"])
    cmd_schedule(ns)
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_parser_wiring_due(schedule_store, capsys):
    ns = build_parser().parse_args(
        ["schedule", "due", "--as-of", "2026-06-05T12:00:00+00:00", "--json"]
    )
    cmd_schedule(ns)
    assert isinstance(json.loads(capsys.readouterr().out), list)


def test_parser_wiring_record_run(schedule_store, capsys):
    tid = _add_id("t", "@hourly", capsys)
    ns = build_parser().parse_args(
        ["schedule", "record-run", tid, "--at", "2026-06-05T09:00:00+00:00", "--json"]
    )
    cmd_schedule(ns)
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["task"]["run_count"] == 1
    assert "09:00:00" in data["task"]["last_run_at"]


def test_parser_wiring_run_due_no_tasks(schedule_store, capsys):
    """run-due with no due tasks returns ok=True, launched=False."""
    ns = build_parser().parse_args(["schedule", "run-due", "--json"])
    cmd_schedule(ns)
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["launched"] is False


def test_parser_wiring_run_due_dry_run(schedule_store, capsys):
    """--dry-run flag is wired correctly (dest=dry_run)."""
    ns = build_parser().parse_args(["schedule", "run-due", "--dry-run", "--json"])
    assert ns.dry_run is True
    # Calling with no tasks should still return ok without spawning anything.
    cmd_schedule(ns)
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["launched"] is False

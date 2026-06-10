"""
Tests for orchestrator.cron_store -- Tier 3e scheduled-task foundation.

Covers:
  - parse_schedule_expr: valid expressions, invalid expressions, edge cases
  - next_run_after: interval calculation, UTC-awareness
  - CronStore.add: happy path, validation rejections
  - CronStore.get: found / not-found
  - CronStore.update: field updates, unknown-field guard, not-found
  - CronStore.delete: found / not-found
  - CronStore.list_tasks: empty, ordering by created_at, enabled_only filter
  - File resilience: missing file, corrupt JSON, wrong root type (dict vs list/str)
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.cron_store import (
    CronStore,
    ScheduledTask,
    next_run_after,
    parse_schedule_expr,
)


# -- parse_schedule_expr -------------------------------------------------------


@pytest.mark.parametrize("expr,expected", [
    ("@hourly",    timedelta(hours=1)),
    ("@HOURLY",    timedelta(hours=1)),
    ("@daily",     timedelta(days=1)),
    ("@DAILY",     timedelta(days=1)),
    ("@weekly",    timedelta(weeks=1)),
    ("@every 1m",  timedelta(minutes=1)),
    ("@every 30m", timedelta(minutes=30)),
    ("@every 2h",  timedelta(hours=2)),
    ("@every 3d",  timedelta(days=3)),
    ("@every 1H",  timedelta(hours=1)),
    ("@every 1D",  timedelta(days=1)),
])
def test_parse_schedule_expr_valid(expr, expected):
    assert parse_schedule_expr(expr) == expected


@pytest.mark.parametrize("expr,frag", [
    ("",             "empty"),
    ("  ",           "empty"),
    ("@monthly",     "Unrecognised"),
    ("every 5m",     "Unrecognised"),       # missing @
    ("@every 0m",    ">= 1"),
    ("@every -1h",   "Unrecognised"),
    ("@every m",     "Unrecognised"),       # no number
    ("@every 1x",    "Unrecognised"),       # bad unit
    ("cron:0 * * *", "Unrecognised"),
    ("x" * 65,       "too long"),
])
def test_parse_schedule_expr_invalid(expr, frag):
    with pytest.raises(ValueError, match=frag):
        parse_schedule_expr(expr)


def test_parse_schedule_expr_non_string():
    with pytest.raises(ValueError, match="string"):
        parse_schedule_expr(42)  # type: ignore[arg-type]


# -- next_run_after ------------------------------------------------------------


def test_next_run_after_interval_calculation():
    anchor = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
    result = next_run_after("@every 30m", after=anchor)
    assert result == anchor + timedelta(minutes=30)


def test_next_run_after_daily():
    anchor = datetime(2026, 6, 5, 9, 0, 0, tzinfo=timezone.utc)
    result = next_run_after("@daily", after=anchor)
    assert result == anchor + timedelta(days=1)


def test_next_run_after_naive_datetime_treated_as_utc():
    naive = datetime(2026, 6, 5, 0, 0, 0)
    result = next_run_after("@hourly", after=naive)
    assert result.tzinfo is not None
    assert result == datetime(2026, 6, 5, 1, 0, 0, tzinfo=timezone.utc)


def test_next_run_after_defaults_to_now(monkeypatch):
    fixed = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    import orchestrator.cron_store as cs
    monkeypatch.setattr(cs, "datetime", type("_DT", (), {
        "now": staticmethod(lambda tz=None: fixed),
        "utcnow": staticmethod(lambda: fixed),
    }))
    result = next_run_after("@every 1h")
    assert result == fixed + timedelta(hours=1)


# -- CronStore CRUD ------------------------------------------------------------


def _store(tmp: str) -> CronStore:
    return CronStore(store_file=Path(tmp) / "schedules.json")


def test_add_returns_scheduled_task():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        t = store.add("check disk usage", "@daily", provider="ollama")
        assert isinstance(t, ScheduledTask)
        assert t.task == "check disk usage"
        assert t.schedule_expr == "@daily"
        assert t.provider == "ollama"
        assert t.enabled is True
        assert t.id
        assert t.created_at
        assert t.updated_at == t.created_at


def test_add_persists_to_file():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        t = store.add("ping check", "@hourly")
        data = json.loads((Path(tmp) / "schedules.json").read_text())
        assert t.id in data
        assert data[t.id]["task"] == "ping check"


def test_add_validates_schedule_expr():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        with pytest.raises(ValueError, match="Unrecognised"):
            store.add("bad", "@monthly")


def test_add_rejects_empty_task():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        with pytest.raises(ValueError, match="empty"):
            store.add("   ", "@daily")


def test_add_rejects_task_too_long():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        with pytest.raises(ValueError, match="too long"):
            store.add("x" * 4097, "@daily")


def test_add_provider_none_by_default():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        t = store.add("task", "@hourly")
        assert t.provider is None


def test_add_disabled_task():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        t = store.add("silent task", "@weekly", enabled=False)
        assert t.enabled is False


def test_get_existing_task():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        t = store.add("find me", "@daily")
        found = store.get(t.id)
        assert found is not None
        assert found.id == t.id
        assert found.task == "find me"


def test_get_missing_task_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        assert store.get("nonexistent-id") is None


def test_update_task_text():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        t = store.add("old text", "@hourly")
        updated = store.update(t.id, task="new text")
        assert updated is not None
        assert updated.task == "new text"
        assert updated.updated_at >= t.updated_at


def test_update_schedule_expr():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        t = store.add("task", "@hourly")
        updated = store.update(t.id, schedule_expr="@daily")
        assert updated.schedule_expr == "@daily"


def test_update_invalid_schedule_expr_raises():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        t = store.add("task", "@hourly")
        with pytest.raises(ValueError, match="Unrecognised"):
            store.update(t.id, schedule_expr="bad_expr")


def test_update_enabled_flag():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        t = store.add("task", "@daily")
        assert t.enabled is True
        updated = store.update(t.id, enabled=False)
        assert updated.enabled is False


def test_update_unknown_field_raises():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        t = store.add("task", "@daily")
        with pytest.raises(ValueError, match="Unknown update fields"):
            store.update(t.id, nonexistent_field="value")


def test_update_missing_task_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        result = store.update("ghost-id", task="whatever")
        assert result is None


def test_delete_existing_task():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        t = store.add("to delete", "@hourly")
        assert store.delete(t.id) is True
        assert store.get(t.id) is None


def test_delete_missing_task_returns_false():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        assert store.delete("phantom") is False


def test_list_tasks_empty_store():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        assert store.list_tasks() == []


def test_list_tasks_multiple_entries():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        a = store.add("alpha", "@hourly")
        b = store.add("beta", "@daily")
        c = store.add("gamma", "@weekly")
        tasks = store.list_tasks()
        ids = [t.id for t in tasks]
        assert a.id in ids
        assert b.id in ids
        assert c.id in ids
        assert len(tasks) == 3


def test_list_tasks_ordered_by_created_at():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        a = store.add("first", "@hourly")
        b = store.add("second", "@daily")
        tasks = store.list_tasks()
        assert tasks[0].id == a.id
        assert tasks[1].id == b.id


def test_list_tasks_enabled_only_filter():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        active = store.add("active", "@hourly", enabled=True)
        store.add("inactive", "@daily", enabled=False)
        tasks = store.list_tasks(enabled_only=True)
        assert len(tasks) == 1
        assert tasks[0].id == active.id


def test_list_tasks_after_delete():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        a = store.add("keep", "@hourly")
        b = store.add("remove", "@daily")
        store.delete(b.id)
        tasks = store.list_tasks()
        assert len(tasks) == 1
        assert tasks[0].id == a.id


# -- File resilience -----------------------------------------------------------


def test_missing_file_returns_empty_list():
    with tempfile.TemporaryDirectory() as tmp:
        store = CronStore(store_file=Path(tmp) / "nonexistent" / "schedules.json")
        assert store.list_tasks() == []


def test_corrupt_json_returns_empty_list(caplog):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "schedules.json"
        path.write_text("{{not valid json}}", encoding="utf-8")
        store = CronStore(store_file=path)
        with caplog.at_level("WARNING"):
            tasks = store.list_tasks()
        assert tasks == []
        assert "failed to read" in caplog.text.lower() or len(tasks) == 0


def test_wrong_root_type_json_array_returns_empty_list(caplog):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "schedules.json"
        path.write_text(json.dumps([{"id": "x", "task": "t"}]), encoding="utf-8")
        store = CronStore(store_file=path)
        with caplog.at_level("WARNING"):
            tasks = store.list_tasks()
        assert tasks == []
        assert "expected dict" in caplog.text


def test_wrong_root_type_string_returns_empty_list(caplog):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "schedules.json"
        path.write_text(json.dumps("just a string"), encoding="utf-8")
        store = CronStore(store_file=path)
        with caplog.at_level("WARNING"):
            tasks = store.list_tasks()
        assert tasks == []


def test_get_with_non_bool_enabled_in_json_returns_none(caplog):
    """get() must return None when the stored entry has a non-bool enabled value."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "schedules.json"
        # Simulate a hand-edited entry where "false" was written as a string.
        entry = {
            "id": "aaa",
            "task": "ping",
            "schedule_expr": "@hourly",
            "provider": None,
            "enabled": "false",
            "created_at": "2026-06-05T10:00:00+00:00",
            "updated_at": "2026-06-05T10:00:00+00:00",
        }
        path.write_text(json.dumps({"aaa": entry}), encoding="utf-8")
        store = CronStore(store_file=path)
        with caplog.at_level("WARNING"):
            result = store.get("aaa")
        assert result is None
        assert "corrupt" in caplog.text.lower()


def test_list_tasks_skips_entry_with_non_bool_enabled(caplog):
    """list_tasks() skips entries whose stored enabled field is not a JSON bool."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "schedules.json"
        good_entry = {
            "id": "good",
            "task": "valid task",
            "schedule_expr": "@daily",
            "provider": None,
            "enabled": True,
            "created_at": "2026-06-05T10:00:00+00:00",
            "updated_at": "2026-06-05T10:00:00+00:00",
        }
        bad_entry = {
            "id": "bad",
            "task": "integer enabled",
            "schedule_expr": "@hourly",
            "provider": None,
            "enabled": 1,
            "created_at": "2026-06-05T10:01:00+00:00",
            "updated_at": "2026-06-05T10:01:00+00:00",
        }
        path.write_text(json.dumps({"good": good_entry, "bad": bad_entry}),
                        encoding="utf-8")
        store = CronStore(store_file=path)
        with caplog.at_level("WARNING"):
            tasks = store.list_tasks()
        assert len(tasks) == 1
        assert tasks[0].id == "good"
        assert "corrupt" in caplog.text.lower()


def test_add_after_corrupt_file_recovers():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "schedules.json"
        path.write_text("NOT JSON", encoding="utf-8")
        store = CronStore(store_file=path)
        t = store.add("recovery task", "@hourly")
        assert t.task == "recovery task"
        tasks = store.list_tasks()
        assert len(tasks) == 1


# -- ScheduledTask serialisation -----------------------------------------------


def test_scheduled_task_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        original = store.add("roundtrip", "@every 15m", provider="browser")
        loaded = store.get(original.id)
        assert loaded is not None
        assert loaded.task == original.task
        assert loaded.schedule_expr == original.schedule_expr
        assert loaded.provider == original.provider
        assert loaded.enabled == original.enabled
        assert loaded.created_at == original.created_at


def test_to_dict_from_dict_symmetry():
    t = ScheduledTask(
        id="abc",
        task="ping",
        schedule_expr="@hourly",
        provider=None,
        enabled=True,
        created_at="2026-06-05T10:00:00+00:00",
        updated_at="2026-06-05T10:00:00+00:00",
    )
    assert ScheduledTask.from_dict(t.to_dict()).id == t.id
    assert ScheduledTask.from_dict(t.to_dict()).task == t.task


def test_json_file_is_human_readable_dict():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        t = store.add("readable", "@daily")
        data = json.loads((Path(tmp) / "schedules.json").read_text())
        assert isinstance(data, dict)
        assert data[t.id]["task"] == "readable"
        assert data[t.id]["schedule_expr"] == "@daily"


# -- Input normalisation and bool guard ----------------------------------------


def test_add_strips_whitespace_from_schedule_expr():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        t = store.add("task", "  @daily  ")
        assert t.schedule_expr == "@daily"
        # Verify the stripped form is persisted, not the padded original.
        loaded = store.get(t.id)
        assert loaded.schedule_expr == "@daily"


def test_update_strips_whitespace_from_schedule_expr():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        t = store.add("task", "@hourly")
        updated = store.update(t.id, schedule_expr="  @daily  ")
        assert updated.schedule_expr == "@daily"
        loaded = store.get(t.id)
        assert loaded.schedule_expr == "@daily"


@pytest.mark.parametrize("bad_enabled", ["false", "true", "1", 1, 0, None])
def test_add_rejects_non_bool_enabled(bad_enabled):
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        with pytest.raises(ValueError, match="bool"):
            store.add("task", "@daily", enabled=bad_enabled)


@pytest.mark.parametrize("bad_enabled", ["false", "true", "1", 1, 0, None])
def test_update_rejects_non_bool_enabled(bad_enabled):
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        t = store.add("task", "@daily")
        with pytest.raises(ValueError, match="bool"):
            store.update(t.id, enabled=bad_enabled)


# -- Execution state: ScheduledTask new fields ---------------------------------


def _raw_entry(
    task_id: str = "tid",
    task: str = "do something",
    schedule_expr: str = "@hourly",
    created_at: str = "2026-06-05T10:00:00+00:00",
    **extra,
) -> dict:
    """Build a minimal valid raw entry dict for direct JSON injection."""
    base = {
        "id": task_id,
        "task": task,
        "schedule_expr": schedule_expr,
        "provider": None,
        "enabled": True,
        "created_at": created_at,
        "updated_at": created_at,
    }
    base.update(extra)
    return base


def _write_entries(path: Path, entries: dict) -> None:
    path.write_text(json.dumps(entries), encoding="utf-8")


def test_backward_compat_old_entry_loads_with_defaults():
    """Old JSON entries without run_count/last_run_at/next_run_at load cleanly."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "schedules.json"
        _write_entries(path, {"tid": _raw_entry()})
        store = CronStore(store_file=path)
        t = store.get("tid")
        assert t is not None
        assert t.run_count == 0
        assert t.last_run_at is None
        assert t.next_run_at is None


# -- record_run ----------------------------------------------------------------


def test_record_run_basic():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        t = store.add("task", "@hourly")
        ran = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
        updated = store.record_run(t.id, ran_at=ran)
        assert updated is not None
        assert updated.run_count == 1
        assert updated.last_run_at == ran.isoformat()
        assert updated.next_run_at == (ran + timedelta(hours=1)).isoformat()


def test_record_run_increments_run_count():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        t = store.add("task", "@daily")
        ran1 = datetime(2026, 6, 5, 9, 0, 0, tzinfo=timezone.utc)
        ran2 = datetime(2026, 6, 6, 9, 0, 0, tzinfo=timezone.utc)
        store.record_run(t.id, ran_at=ran1)
        updated = store.record_run(t.id, ran_at=ran2)
        assert updated.run_count == 2
        assert updated.last_run_at == ran2.isoformat()
        assert updated.next_run_at == (ran2 + timedelta(days=1)).isoformat()


def test_record_run_naive_ran_at_treated_as_utc():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        t = store.add("task", "@hourly")
        naive = datetime(2026, 6, 5, 8, 0, 0)
        updated = store.record_run(t.id, ran_at=naive)
        assert updated is not None
        assert "08:00:00" in updated.last_run_at
        # next_run_at should be 1h after 08:00 UTC
        assert "09:00:00" in updated.next_run_at


def test_record_run_missing_task_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        assert store.record_run("ghost") is None


def test_record_run_corrupt_entry_returns_none_without_saving():
    """record_run must not modify the file when the entry is corrupt."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "schedules.json"
        corrupt = dict(_raw_entry(task_id="bad"), enabled="yes")
        _write_entries(path, {"bad": corrupt})
        original_bytes = path.read_bytes()

        store = CronStore(store_file=path)
        result = store.record_run("bad")
        assert result is None
        assert path.read_bytes() == original_bytes


def test_record_run_persists_to_file():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        t = store.add("task", "@every 30m")
        ran = datetime(2026, 6, 5, 10, 0, 0, tzinfo=timezone.utc)
        store.record_run(t.id, ran_at=ran)
        # Reload via a fresh store instance to confirm persistence.
        store2 = CronStore(store_file=Path(tmp) / "schedules.json")
        reloaded = store2.get(t.id)
        assert reloaded.run_count == 1
        assert reloaded.next_run_at == (ran + timedelta(minutes=30)).isoformat()


# -- due_tasks -----------------------------------------------------------------


def test_due_tasks_empty_store():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        assert store.due_tasks(as_of=datetime(2026, 6, 5, tzinfo=timezone.utc)) == []


def test_due_tasks_first_run_boundary_exact():
    """Never-run task: due at created_at + interval, not before.

    Policy: create at T with @hourly -> first due at T+1h exactly.
    T+59m59s -> not due; T+60m -> due.
    """
    T = datetime(2026, 6, 5, 10, 0, 0, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "schedules.json"
        _write_entries(path, {"t1": _raw_entry(task_id="t1", created_at=T.isoformat())})
        store = CronStore(store_file=path)

        not_due = store.due_tasks(as_of=T + timedelta(seconds=3599))
        assert not_due == []

        just_due = store.due_tasks(as_of=T + timedelta(hours=1))
        assert len(just_due) == 1
        assert just_due[0].id == "t1"


def test_due_tasks_with_next_run_at_due():
    """Task with explicit next_run_at: due when next_run_at <= as_of."""
    T = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "schedules.json"
        entry = _raw_entry(
            task_id="t1",
            next_run_at=T.isoformat(),
            run_count=1,
            last_run_at=(T - timedelta(hours=1)).isoformat(),
        )
        _write_entries(path, {"t1": entry})
        store = CronStore(store_file=path)
        assert len(store.due_tasks(as_of=T)) == 1
        assert store.due_tasks(as_of=T - timedelta(seconds=1)) == []


def test_due_tasks_disabled_excluded():
    T = datetime(2026, 6, 5, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "schedules.json"
        entry = dict(_raw_entry(created_at=(T - timedelta(days=1)).isoformat()), enabled=False)
        _write_entries(path, {"t1": entry})
        store = CronStore(store_file=path)
        assert store.due_tasks(as_of=T) == []


def test_due_tasks_corrupt_entry_skipped(caplog):
    T = datetime(2026, 6, 5, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "schedules.json"
        good = _raw_entry(task_id="good", created_at=(T - timedelta(days=1)).isoformat())
        bad = dict(_raw_entry(task_id="bad"), enabled=1)
        _write_entries(path, {"good": good, "bad": bad})
        store = CronStore(store_file=path)
        with caplog.at_level("WARNING"):
            tasks = store.due_tasks(as_of=T)
        assert len(tasks) == 1
        assert tasks[0].id == "good"


def test_due_tasks_ordering_ascending_by_due_time():
    """due_tasks returns tasks ordered by effective due time, oldest first."""
    base = datetime(2026, 6, 5, 10, 0, 0, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "schedules.json"
        # t_late: next_run_at is 30 min after base (more recent due time)
        # t_early: next_run_at is 1 min after base (older due time, should come first)
        t_early = _raw_entry(
            task_id="early",
            next_run_at=(base + timedelta(minutes=1)).isoformat(),
            run_count=1,
            last_run_at=base.isoformat(),
        )
        t_late = _raw_entry(
            task_id="late",
            next_run_at=(base + timedelta(minutes=30)).isoformat(),
            run_count=1,
            last_run_at=base.isoformat(),
        )
        _write_entries(path, {"early": t_early, "late": t_late})
        store = CronStore(store_file=path)
        tasks = store.due_tasks(as_of=base + timedelta(hours=1))
        assert [t.id for t in tasks] == ["early", "late"]


def test_due_tasks_id_tiebreak_deterministic():
    """Tasks with identical due times are sorted by id for stable ordering."""
    T = datetime(2026, 6, 5, 10, 0, 0, tzinfo=timezone.utc)
    due_time = T + timedelta(hours=1)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "schedules.json"
        # Both tasks created at T with @hourly -> both due at T+1h.
        a = _raw_entry(task_id="aaa_task", created_at=T.isoformat())
        z = _raw_entry(task_id="zzz_task", created_at=T.isoformat())
        _write_entries(path, {"aaa_task": a, "zzz_task": z})
        store = CronStore(store_file=path)
        tasks = store.due_tasks(as_of=due_time)
        assert [t.id for t in tasks] == ["aaa_task", "zzz_task"]


def test_due_tasks_naive_as_of_treated_as_utc():
    T = datetime(2026, 6, 5, 10, 0, 0, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "schedules.json"
        _write_entries(path, {"t1": _raw_entry(created_at=(T - timedelta(days=1)).isoformat())})
        store = CronStore(store_file=path)
        naive_now = datetime(2026, 6, 5, 10, 0, 0)  # no tzinfo
        tasks = store.due_tasks(as_of=naive_now)
        assert len(tasks) == 1


# -- UTC normalisation (aware non-UTC inputs) ----------------------------------


def test_parse_utc_iso_aware_non_utc_normalised_to_utc():
    """_parse_utc_iso must convert aware non-UTC strings to UTC."""
    from orchestrator.cron_store import _parse_utc_iso
    # 10:00 -05:00 == 15:00 UTC
    dt = _parse_utc_iso("2026-06-05T10:00:00-05:00")
    assert dt.tzinfo == timezone.utc
    assert dt.hour == 15
    assert dt.minute == 0


def test_parse_utc_iso_utc_aware_unchanged():
    from orchestrator.cron_store import _parse_utc_iso
    dt = _parse_utc_iso("2026-06-05T12:00:00+00:00")
    assert dt.tzinfo == timezone.utc
    assert dt.hour == 12


def test_record_run_aware_non_utc_stored_as_utc():
    """record_run with an aware non-UTC ran_at must persist +00:00 timestamps."""
    from datetime import timezone as tz
    minus5 = tz(timedelta(hours=-5))
    ran_local = datetime(2026, 6, 5, 10, 0, 0, tzinfo=minus5)  # == 15:00 UTC
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        t = store.add("task", "@hourly")
        updated = store.record_run(t.id, ran_at=ran_local)
        assert updated is not None
        # stored last_run_at should be UTC 15:00
        assert "+00:00" in updated.last_run_at
        assert "15:00:00" in updated.last_run_at
        # next_run_at should be UTC 16:00
        assert "+00:00" in updated.next_run_at
        assert "16:00:00" in updated.next_run_at


def test_due_tasks_aware_non_utc_as_of_normalised():
    """due_tasks with a non-UTC aware as_of must compare correctly after normalisation."""
    from datetime import timezone as tz
    # Task created 2 days ago (UTC), so definitely due.
    T_utc = datetime(2026, 6, 5, 10, 0, 0, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "schedules.json"
        _write_entries(path, {"t1": _raw_entry(created_at=(T_utc - timedelta(days=2)).isoformat())})
        store = CronStore(store_file=path)
        # as_of expressed in +05:30 (IST) == T_utc in UTC
        plus530 = tz(timedelta(hours=5, minutes=30))
        as_of_ist = T_utc.astimezone(plus530)
        tasks = store.due_tasks(as_of=as_of_ist)
        assert len(tasks) == 1


# -- run_count bool guard ------------------------------------------------------


def test_run_count_bool_in_json_rejected():
    """JSON true/false for run_count must be rejected (bool is int subclass)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "schedules.json"
        entry = dict(_raw_entry(), run_count=True)  # JSON true -> Python True
        _write_entries(path, {"tid": entry})
        store = CronStore(store_file=path)
        result = store.get("tid")
        assert result is None


def test_run_count_bool_false_in_json_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "schedules.json"
        entry = dict(_raw_entry(), run_count=False)
        _write_entries(path, {"tid": entry})
        store = CronStore(store_file=path)
        result = store.get("tid")
        assert result is None

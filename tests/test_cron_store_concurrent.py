"""
Concurrency / durability tests for orchestrator.cron_store.

Proves that simultaneous callers (threads sharing a single process address space,
which exercises the same file-lock path as separate OS processes) cannot:
  - lose add() entries (each add must persist its own task)
  - lose record_run() increments (each call must advance run_count by exactly 1)
  - leave the store file in a partially-written state (concurrent writes)

Thread-level parallelism is sufficient because Python threads share the GIL
but do release it around I/O syscalls (open, read, write, os.replace, fcntl).
The lock + reload-inside-lock pattern is identical whether callers are threads
or separate OS processes: the file descriptor is per-thread, the flock is
per-fd, so threads contend on the real kernel lock.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestrator.cron_store import CronStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store(tmp_path: Path) -> CronStore:
    return CronStore(store_file=tmp_path / "schedules.json")


def _run_threads(fns: list, timeout: float = 30.0) -> list[Exception]:
    """Run callables concurrently; return any exceptions raised."""
    barrier = threading.Barrier(len(fns))
    errors: list[Exception] = []
    lock = threading.Lock()

    def wrapper(fn):
        barrier.wait()  # maximize contention by starting simultaneously
        try:
            fn()
        except Exception as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=wrapper, args=(fn,)) for fn in fns]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=timeout)
    return errors


# ---------------------------------------------------------------------------
# concurrent add — no lost entries
# ---------------------------------------------------------------------------

def test_concurrent_add_all_entries_persisted(tmp_path):
    """N concurrent add() calls must each persist their own entry."""
    n = 12
    ids: list[str] = []
    id_lock = threading.Lock()

    def do_add(i: int):
        store = _store(tmp_path)
        t = store.add(f"task {i}", "@hourly")
        with id_lock:
            ids.append(t.id)

    errors = _run_threads([lambda i=i: do_add(i) for i in range(n)])

    assert not errors, f"threads raised: {errors}"
    store = _store(tmp_path)
    tasks = store.list_tasks()
    assert len(tasks) == n, (
        f"Expected {n} tasks in store after {n} concurrent add() calls, "
        f"got {len(tasks)} — {n - len(tasks)} entries were lost"
    )
    # Every returned id must be in the store.
    stored_ids = {t.id for t in tasks}
    for tid in ids:
        assert tid in stored_ids, f"add() returned id {tid!r} but it is not in the store"


def test_concurrent_add_file_is_valid_json(tmp_path):
    """Store file must be valid JSON after concurrent adds (no partial write)."""
    import json

    n = 16

    def do_add(i: int):
        _store(tmp_path).add(f"task {i}", "@daily")

    errors = _run_threads([lambda i=i: do_add(i) for i in range(n)])
    assert not errors

    path = tmp_path / "schedules.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    assert len(raw) == n


# ---------------------------------------------------------------------------
# concurrent record_run — no lost increments
# ---------------------------------------------------------------------------

def test_concurrent_record_run_no_lost_updates(tmp_path):
    """N concurrent record_run() calls must each increment run_count by 1."""
    n = 12
    store = _store(tmp_path)
    t = store.add("concurrent task", "@hourly")

    def do_record():
        s = _store(tmp_path)
        s.record_run(t.id, ran_at=datetime.now(timezone.utc))

    errors = _run_threads([do_record] * n)

    assert not errors, f"threads raised: {errors}"
    final = _store(tmp_path).get(t.id)
    assert final is not None
    assert final.run_count == n, (
        f"Expected run_count={n} after {n} concurrent record_run() calls, "
        f"got {final.run_count} — {n - final.run_count} increment(s) were lost"
    )


def test_concurrent_record_run_monotone_next_run_at(tmp_path):
    """After N concurrent record_run() calls, next_run_at must be strictly in the future."""
    n = 8
    store = _store(tmp_path)
    t = store.add("monotone task", "@every 5m")

    errors = _run_threads([lambda: _store(tmp_path).record_run(t.id)] * n)
    assert not errors

    final = _store(tmp_path).get(t.id)
    assert final is not None
    assert final.next_run_at is not None
    from orchestrator.cron_store import _parse_utc_iso
    next_dt = _parse_utc_iso(final.next_run_at)
    last_dt = _parse_utc_iso(final.last_run_at)
    assert next_dt > last_dt, "next_run_at must be strictly after last_run_at"


# ---------------------------------------------------------------------------
# concurrent add + record_run mixed
# ---------------------------------------------------------------------------

def test_concurrent_add_and_record_run_no_corruption(tmp_path):
    """Mixed concurrent adds and record_run calls must leave a coherent store."""
    store = _store(tmp_path)
    existing = store.add("existing task", "@daily")

    fns = []
    for i in range(6):
        fns.append(lambda i=i: _store(tmp_path).add(f"new task {i}", "@hourly"))
    for _ in range(6):
        fns.append(lambda: _store(tmp_path).record_run(existing.id))

    errors = _run_threads(fns)
    assert not errors

    final_store = _store(tmp_path)
    tasks = final_store.list_tasks()
    # 1 existing + 6 newly added
    assert len(tasks) == 7, f"Expected 7 tasks, got {len(tasks)}"

    reloaded = final_store.get(existing.id)
    assert reloaded is not None
    assert reloaded.run_count == 6, (
        f"Expected run_count=6 for existing task, got {reloaded.run_count}"
    )


# ---------------------------------------------------------------------------
# lock released on exception inside critical section
# ---------------------------------------------------------------------------

def test_lock_released_after_internal_exception(tmp_path):
    """If an exception occurs inside the lock, it must be released so the next
    caller can proceed without deadlocking."""
    import json
    from unittest.mock import patch

    store = _store(tmp_path)
    t = store.add("task", "@hourly")

    # Make _save raise on the first call, simulating a mid-write crash.
    call_count = {"n": 0}
    original_save = CronStore._save

    def patched_save(self, data):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("simulated disk full")
        original_save(self, data)

    with patch.object(CronStore, "_save", patched_save):
        with pytest.raises(OSError, match="simulated disk full"):
            store.record_run(t.id)

    # After the exception the lock must be released — the next call must succeed.
    updated = store.record_run(t.id)
    assert updated is not None
    assert updated.run_count == 1


# ---------------------------------------------------------------------------
# stale-read protection: reload inside lock sees latest state
# ---------------------------------------------------------------------------

def test_record_run_sees_external_write_before_save(tmp_path):
    """record_run must reload the file inside the lock so it sees changes made
    by a concurrent writer before it acquired the lock."""
    store = _store(tmp_path)
    t = store.add("task", "@hourly")

    # Simulate a concurrent writer that bumps run_count to 5 just before the
    # lock is acquired by the caller under test.  We do this by pre-writing
    # the file directly (bypassing the lock) to simulate what another process
    # would have done.
    import json
    path = tmp_path / "schedules.json"
    data = json.loads(path.read_text())
    data[t.id]["run_count"] = 5
    path.write_text(json.dumps(data))

    # Now call record_run; it must reload and see run_count=5, producing 6.
    updated = store.record_run(t.id)
    assert updated is not None
    assert updated.run_count == 6, (
        f"record_run must reload inside the lock and see the pre-written "
        f"run_count=5, so it should produce 6, not {updated.run_count}"
    )

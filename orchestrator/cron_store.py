"""
Persistent store for scheduled agent tasks (Tier 3e foundation).

Each entry records a task text, schedule expression, provider hint, and
lifecycle metadata.  Storage is a single JSON file (dict keyed by id) written
atomically so it is safe to inspect, edit, and version-control.

No background executor lives here.  Scheduling evaluation is intentionally
separated from execution so the store can be tested and inspected independently.
The executor (Tauri / CLI cron runner) will consult this store and must enforce
the standing constraint: Code-tab tasks may only use ollama-cloud or browser
providers, never openai/gpt-5.5.

Supported schedule expressions
-------------------------------
    @hourly          every 60 minutes
    @daily           every 24 hours
    @weekly          every 7 days
    @every <N>m      every N minutes  (N >= 1)
    @every <N>h      every N hours    (N >= 1)
    @every <N>d      every N days     (N >= 1)

Public helpers
--------------
parse_schedule_expr(expr) -> timedelta
    Returns a timedelta for any valid expression.
    Raises ValueError with a clear message for invalid ones.

next_run_after(expr, after=None) -> datetime
    Returns (after + interval) in UTC.  after defaults to now(UTC).
    Purely interval-based -- no calendar alignment (midnight snapping, etc.).
    Calendar alignment is an executor concern, deferred to a later slice.

First-run / due-time policy
----------------------------
A never-run task (run_count == 0, next_run_at not set) is first due at
created_at + interval.  After each record_run() call, next_run_at is stored
as last_run_at + interval so subsequent due times are always explicit.

In other words: create at T with @hourly -> first due at T+1h, not immediately.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

_LOCK_TIMEOUT = 10.0  # seconds before giving up on lock acquisition


@contextlib.contextmanager
def _exclusive_lock(lock_path: Path, timeout: float = _LOCK_TIMEOUT):
    """Exclusive advisory lock around a store read-modify-write cycle.

    POSIX (macOS / Linux): fcntl.flock — blocks until acquired; released
    automatically when the file descriptor closes, so a crashed process never
    leaves a stale lock.

    Windows: spin on O_CREAT|O_EXCL.  The lock file is removed in the finally
    block.  A crashed process may leave a stale lock file; if it persists
    longer than *timeout* seconds the next caller raises TimeoutError.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except (FileExistsError, PermissionError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"cron_store: could not acquire lock {lock_path} within {timeout}s"
                    )
                time.sleep(0.02)
        try:
            yield
        finally:
            try:
                lock_path.unlink()
            except OSError:
                pass
    else:
        import fcntl  # noqa: PLC0415 — POSIX only
        with open(lock_path, "a") as fh:  # 'a' creates without truncating
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


_DEFAULT_STORE_FILE = Path(__file__).resolve().parent.parent / "kim_schedules.json"

_MAX_TASK_LEN = 4096
_MAX_EXPR_LEN = 64
_MAX_PROVIDER_LEN = 64

_NAMED: dict[str, timedelta] = {
    "@hourly": timedelta(hours=1),
    "@daily":  timedelta(days=1),
    "@weekly": timedelta(weeks=1),
}
_EVERY_RE = re.compile(r"^@every\s+(\d+)(m|h|d)$", re.IGNORECASE)


# -- Schedule expression helpers -----------------------------------------------


def parse_schedule_expr(expr: str) -> timedelta:
    """Parse a schedule expression into a timedelta.

    Raises ValueError for invalid or out-of-range expressions.
    """
    if not isinstance(expr, str):
        raise ValueError(f"schedule_expr must be a string, got {type(expr).__name__!r}")
    expr = expr.strip()
    if not expr:
        raise ValueError("schedule_expr must not be empty")
    if len(expr) > _MAX_EXPR_LEN:
        raise ValueError(f"schedule_expr too long (max {_MAX_EXPR_LEN} chars)")

    lower = expr.lower()
    if lower in _NAMED:
        return _NAMED[lower]

    m = _EVERY_RE.match(expr)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        if n < 1:
            raise ValueError(f"@every interval must be >= 1, got {n}")
        try:
            if unit == "m":
                return timedelta(minutes=n)
            if unit == "h":
                return timedelta(hours=n)
            return timedelta(days=n)
        except OverflowError:
            raise ValueError(f"@every interval out of range: {n}{unit}")

    raise ValueError(
        f"Unrecognised schedule expression {expr!r}. "
        "Supported: @hourly, @daily, @weekly, @every <N>m/h/d"
    )


def next_run_after(schedule_expr: str, after: Optional[datetime] = None) -> datetime:
    """Return the next run time for schedule_expr after the given datetime.

    after defaults to now(UTC).  The result is always UTC-aware.
    Calculation is strictly interval-based (after + interval); calendar
    alignment (e.g. snapping @daily to midnight) is an executor concern.
    """
    interval = parse_schedule_expr(schedule_expr)
    base = after if after is not None else datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base + interval


def _parse_utc_iso(s: str) -> datetime:
    """Parse an ISO-8601 string to a UTC-aware datetime.

    Naive strings (no tzinfo) are treated as UTC.
    Aware strings with non-UTC offsets are converted to UTC.
    Raises ValueError if s is not parseable by datetime.fromisoformat.
    """
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _effective_next_run(task: "ScheduledTask") -> datetime:
    """Return the UTC datetime when task is next due.

    Policy (see module docstring):
      - If task.next_run_at is set (from a previous record_run call), use it.
      - Otherwise (never-run task): compute created_at + interval.

    Raises ValueError if the schedule expression is invalid or created_at is
    unparseable (caller should treat as corrupt).
    """
    if task.next_run_at:
        return _parse_utc_iso(task.next_run_at)
    base = _parse_utc_iso(task.created_at) if task.created_at else datetime.now(timezone.utc)
    return next_run_after(task.schedule_expr, base)


# -- Data model ----------------------------------------------------------------


@dataclasses.dataclass
class ScheduledTask:
    # -- Required fields (no defaults; must be supplied explicitly) -------------
    id: str
    task: str
    schedule_expr: str
    provider: Optional[str]
    enabled: bool
    created_at: str
    updated_at: str
    # -- Execution state (optional; default to "never run") --------------------
    # run_count: how many times record_run() has been called for this task.
    # last_run_at: ISO UTC timestamp of the most recent record_run() call.
    # next_run_at: ISO UTC timestamp of the next due time (set by record_run).
    #   When absent, due_tasks() falls back to created_at + interval.
    run_count: int = 0
    last_run_at: Optional[str] = None
    next_run_at: Optional[str] = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ScheduledTask":
        enabled_raw = d.get("enabled", True)  # missing -> True for backward compat
        if not isinstance(enabled_raw, bool):
            raise ValueError(
                f"enabled in stored entry must be a JSON bool, "
                f"got {type(enabled_raw).__name__!r} ({enabled_raw!r})"
            )
        run_count_raw = d.get("run_count", 0)
        if isinstance(run_count_raw, bool):
            raise ValueError(
                f"run_count in stored entry must be an integer, "
                f"got bool ({run_count_raw!r})"
            )
        try:
            run_count = int(run_count_raw)
        except (TypeError, ValueError):
            raise ValueError(
                f"run_count in stored entry must be an integer, "
                f"got {type(run_count_raw).__name__!r} ({run_count_raw!r})"
            )
        return ScheduledTask(
            id=str(d["id"]),
            task=str(d["task"]),
            schedule_expr=str(d["schedule_expr"]),
            provider=str(d["provider"]) if d.get("provider") else None,
            enabled=enabled_raw,
            created_at=str(d.get("created_at", "")),
            updated_at=str(d.get("updated_at", "")),
            run_count=run_count,
            last_run_at=str(d["last_run_at"]) if d.get("last_run_at") else None,
            next_run_at=str(d["next_run_at"]) if d.get("next_run_at") else None,
        )


# -- CronStore -----------------------------------------------------------------


class CronStore:
    """Persistent, inspectable JSON store for scheduled agent tasks.

    The backing file is a JSON object whose keys are task IDs.  Reads are
    defensive: a missing or corrupt file returns an empty store rather than
    raising.  Writes are atomic (temp-file + os.replace).

    All methods are synchronous; concurrent writes from multiple processes are
    serialised via `_exclusive_lock` (a file-system advisory lock).  For the
    current Tauri one-task-at-a-time model this is sufficient.
    """

    def __init__(self, store_file: Optional[Path] = None) -> None:
        self._file = Path(store_file) if store_file else _DEFAULT_STORE_FILE

    @property
    def _lock_path(self) -> Path:
        return self._file.with_suffix(".json.lock")

    # -- private ---------------------------------------------------------------

    def _load(self) -> dict[str, dict]:
        if not self._file.exists():
            return {}
        try:
            raw = self._file.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                logger.warning(
                    "cron_store: %s contains %s (expected dict) -- starting empty",
                    self._file, type(data).__name__,
                )
                return {}
            return data
        except Exception as e:
            logger.warning("cron_store: failed to read %s: %s -- starting empty", self._file, e)
            return {}

    def _save(self, data: dict[str, dict]) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".json.tmp")
        payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        # fsync the temp file before the atomic rename (finding 4.3). os.replace
        # is crash-atomic, but without fsync a power loss between write and
        # rename can leave a zero-byte schedules file — _load then "starts empty"
        # and the next _save makes the loss permanent.
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._file)

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    # -- public API ------------------------------------------------------------

    def add(
        self,
        task: str,
        schedule_expr: str,
        provider: Optional[str] = None,
        enabled: bool = True,
    ) -> ScheduledTask:
        """Persist a new scheduled task.  Raises ValueError for invalid inputs."""
        task = str(task).strip()
        if not task:
            raise ValueError("task must not be empty")
        if len(task) > _MAX_TASK_LEN:
            raise ValueError(f"task too long (max {_MAX_TASK_LEN} chars)")
        if provider is not None:
            provider = str(provider).strip() or None
            if provider and len(provider) > _MAX_PROVIDER_LEN:
                raise ValueError(f"provider too long (max {_MAX_PROVIDER_LEN} chars)")
        if not isinstance(enabled, bool):
            raise ValueError(
                f"enabled must be a bool, got {type(enabled).__name__!r}; "
                "pass True or False explicitly"
            )

        # Validate -- raises ValueError on bad expression before we touch the file.
        # Strip after validation so the stored expression is normalised.
        parse_schedule_expr(schedule_expr)
        schedule_expr = schedule_expr.strip()

        now = self._now_iso()
        entry = ScheduledTask(
            id=uuid4().hex,
            task=task,
            schedule_expr=schedule_expr,
            provider=provider,
            enabled=enabled,
            created_at=now,
            updated_at=now,
        )
        with _exclusive_lock(self._lock_path):
            data = self._load()
            data[entry.id] = entry.to_dict()
            self._save(data)
        logger.info("cron_store: added task %r (expr=%r)", entry.id, schedule_expr)
        return entry

    def get(self, task_id: str) -> Optional[ScheduledTask]:
        """Return a ScheduledTask by id, or None if not found."""
        data = self._load()
        raw = data.get(task_id)
        if raw is None:
            return None
        try:
            return ScheduledTask.from_dict(raw)
        except Exception as e:
            logger.warning("cron_store: corrupt entry %r: %s", task_id, e)
            return None

    def update(self, task_id: str, **kwargs) -> Optional[ScheduledTask]:
        """Update allowed fields on an existing task.

        Allowed keys: task, schedule_expr, provider, enabled.
        Returns the updated entry, or None if task_id is not found.
        Raises ValueError for unknown keys or invalid field values.
        """
        allowed = {"task", "schedule_expr", "provider", "enabled"}
        unknown = set(kwargs) - allowed
        if unknown:
            raise ValueError(f"Unknown update fields: {sorted(unknown)}")

        # Validate field values before acquiring the lock (pure computation).
        if "task" in kwargs:
            task_val = str(kwargs["task"]).strip()
            if not task_val:
                raise ValueError("task must not be empty")
            if len(task_val) > _MAX_TASK_LEN:
                raise ValueError(f"task too long (max {_MAX_TASK_LEN} chars)")
        if "schedule_expr" in kwargs:
            parse_schedule_expr(kwargs["schedule_expr"])
        if "provider" in kwargs:
            prov = str(kwargs["provider"]).strip() if kwargs["provider"] else None
            if prov and len(prov) > _MAX_PROVIDER_LEN:
                raise ValueError(f"provider too long (max {_MAX_PROVIDER_LEN} chars)")
        if "enabled" in kwargs:
            if not isinstance(kwargs["enabled"], bool):
                raise ValueError(
                    f"enabled must be a bool, got {type(kwargs['enabled']).__name__!r}; "
                    "pass True or False explicitly"
                )

        with _exclusive_lock(self._lock_path):
            data = self._load()
            if task_id not in data:
                return None

            raw = dict(data[task_id])

            if "task" in kwargs:
                raw["task"] = str(kwargs["task"]).strip()
            if "schedule_expr" in kwargs:
                new_expr = str(kwargs["schedule_expr"]).strip()
                schedule_changed = new_expr != str(raw.get("schedule_expr", ""))
                raw["schedule_expr"] = new_expr
                # M8: recompute next_run_at so the new cadence takes effect now.
                # due_tasks reads the stored next_run_at, so leaving the old one
                # (e.g. @daily's "tomorrow") would ignore the new schedule until
                # then. Anchor to last_run_at when the task has run, else now.
                if schedule_changed:
                    base = (
                        _parse_utc_iso(str(raw["last_run_at"]))
                        if raw.get("last_run_at")
                        else datetime.now(timezone.utc)
                    )
                    try:
                        raw["next_run_at"] = next_run_after(new_expr, base).isoformat()
                    except ValueError:
                        # Invalid expr — clear next_run_at; from_dict below will
                        # reject the entry and this update returns None.
                        raw["next_run_at"] = None
            if "provider" in kwargs:
                raw["provider"] = str(kwargs["provider"]).strip() if kwargs["provider"] else None
            if "enabled" in kwargs:
                raw["enabled"] = kwargs["enabled"]

            raw["updated_at"] = self._now_iso()
            data[task_id] = raw
            self._save(data)

        try:
            return ScheduledTask.from_dict(raw)
        except Exception as e:
            logger.warning("cron_store: corrupt entry after update %r: %s", task_id, e)
            return None

    def delete(self, task_id: str) -> bool:
        """Delete a task by id.  Returns True if deleted, False if not found."""
        with _exclusive_lock(self._lock_path):
            data = self._load()
            if task_id not in data:
                return False
            del data[task_id]
            self._save(data)
        logger.info("cron_store: deleted task %r", task_id)
        return True

    def list_tasks(self, enabled_only: bool = False) -> list[ScheduledTask]:
        """Return all tasks, ordered by created_at ascending.

        Pass enabled_only=True to skip disabled entries.
        Corrupt individual entries are skipped with a warning.
        """
        data = self._load()
        tasks = []
        for tid, raw in data.items():
            try:
                t = ScheduledTask.from_dict(raw)
                if enabled_only and not t.enabled:
                    continue
                tasks.append(t)
            except Exception as e:
                logger.warning("cron_store: skipping corrupt entry %r: %s", tid, e)
        tasks.sort(key=lambda t: t.created_at)
        return tasks

    def record_run(
        self,
        task_id: str,
        ran_at: Optional[datetime] = None,
    ) -> Optional[ScheduledTask]:
        """Record that task_id was executed and update scheduling state.

        Increments run_count, sets last_run_at to ran_at (default: now UTC),
        and computes next_run_at as last_run_at + interval from schedule_expr.

        Returns the updated ScheduledTask, or None if task_id is not found.
        If the stored entry is corrupt (fails from_dict) or has an invalid
        schedule_expr, logs a warning and returns None *without* modifying the
        file -- consistent with the existing corrupt-entry skip policy.
        """
        # Normalise ran_at before acquiring the lock (pure computation, no I/O).
        if ran_at is None:
            ran_at_dt = datetime.now(timezone.utc)
        elif ran_at.tzinfo is None:
            ran_at_dt = ran_at.replace(tzinfo=timezone.utc)
        else:
            ran_at_dt = ran_at.astimezone(timezone.utc)

        with _exclusive_lock(self._lock_path):
            # Reload inside the lock so concurrent record_run calls each see
            # the latest run_count written by the previous caller.
            data = self._load()
            if task_id not in data:
                return None

            raw = dict(data[task_id])

            # Validate upfront -- corrupt entry -> warn and return without saving.
            try:
                ScheduledTask.from_dict(raw)
            except Exception as e:
                logger.warning(
                    "cron_store: record_run skipping corrupt entry %r: %s", task_id, e
                )
                return None

            # Compute next_run_at before touching the file.
            schedule_expr = str(raw.get("schedule_expr", ""))
            try:
                next_dt = next_run_after(schedule_expr, ran_at_dt)
            except ValueError as e:
                logger.warning(
                    "cron_store: record_run skipping task %r: bad schedule_expr %r: %s",
                    task_id, schedule_expr, e,
                )
                return None

            raw["run_count"] = int(raw.get("run_count", 0)) + 1
            raw["last_run_at"] = ran_at_dt.isoformat()
            raw["next_run_at"] = next_dt.isoformat()
            raw["updated_at"] = self._now_iso()

            data[task_id] = raw
            self._save(data)

        try:
            return ScheduledTask.from_dict(raw)
        except Exception as e:
            logger.warning(
                "cron_store: corrupt entry after record_run %r: %s", task_id, e
            )
            return None

    def due_tasks(self, as_of: Optional[datetime] = None) -> list[ScheduledTask]:
        """Return enabled tasks due at or before as_of (default: now UTC).

        Due-time policy (see module docstring):
          - Task with next_run_at set: due when next_run_at <= as_of.
          - Never-run task (next_run_at absent): due when
            created_at + interval <= as_of.

        Disabled and corrupt entries are silently skipped.
        Results are ordered by due time ascending (most overdue first); ties
        broken by task id for determinism.
        """
        if as_of is None:
            as_of = datetime.now(timezone.utc)
        elif as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
        else:
            as_of = as_of.astimezone(timezone.utc)

        data = self._load()
        due: list[tuple[datetime, ScheduledTask]] = []

        for tid, raw in data.items():
            try:
                task = ScheduledTask.from_dict(raw)
            except Exception as e:
                logger.warning(
                    "cron_store: due_tasks skipping corrupt entry %r: %s", tid, e
                )
                continue

            if not task.enabled:
                continue

            try:
                effective = _effective_next_run(task)
            except Exception as e:
                logger.warning(
                    "cron_store: due_tasks skipping task %r: bad schedule: %s", tid, e
                )
                continue

            if effective <= as_of:
                due.append((effective, task))

        due.sort(key=lambda x: (x[0], x[1].id))
        return [t for _, t in due]

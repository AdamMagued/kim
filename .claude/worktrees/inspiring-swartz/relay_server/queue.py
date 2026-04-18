"""
SQLite-backed task queue for the Kim relay server.

All public methods are async (via aiosqlite).  The DB file is created at the
path configured by the RELAY_DB_PATH env-var (default: relay.db in the CWD).

Schema
──────
tasks
  id            TEXT PRIMARY KEY      — UUIDv4
  task          TEXT NOT NULL         — raw task string from the phone
  status        TEXT DEFAULT 'pending'— pending | running | done | failed
  priority      INTEGER DEFAULT 0     — higher = dequeued first
  created_at    TIMESTAMP             — UTC, set on INSERT
  picked_up_at  TIMESTAMP             — set when PC dequeues
  completed_at  TIMESTAMP             — set when PC posts result
  summary       TEXT                  — human-readable outcome
  screenshot    TEXT                  — base64 PNG (may include data: URI prefix)
  success       BOOLEAN               — True = agent reported TASK_COMPLETE

Stale-task expiry
─────────────────
Tasks that stay in 'pending' longer than STALE_PENDING_S (default 5 min) or
in 'running' longer than STALE_RUNNING_S (default 10 min) are automatically
moved to 'failed' on every dequeue call.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from uuid import uuid4

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("RELAY_DB_PATH", "relay.db")

STALE_PENDING_S = int(os.environ.get("STALE_PENDING_S", 300))   # 5 min
STALE_RUNNING_S = int(os.environ.get("STALE_RUNNING_S", 600))   # 10 min

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    task          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    priority      INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMP NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    picked_up_at  TIMESTAMP,
    completed_at  TIMESTAMP,
    summary       TEXT,
    screenshot    TEXT,
    success       BOOLEAN
);
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_tasks_status_priority
ON tasks (status, priority DESC, created_at ASC);
"""


class TaskDB:
    """Async SQLite task queue.  Call `await db.init()` before any other method."""

    def __init__(self, path: str = DB_PATH):
        self._path = path
        self._db: aiosqlite.Connection | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute("PRAGMA foreign_keys=ON;")
        await self._db.execute(_CREATE_TABLE)
        await self._db.execute(_CREATE_INDEX)
        await self._db.commit()
        logger.info(f"TaskDB initialised: {self._path}")

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    # ── Write operations ───────────────────────────────────────────────────

    async def enqueue(self, task: str, priority: int = 0) -> str:
        """Insert a new pending task and return its UUID."""
        task_id = uuid4().hex
        await self._db.execute(
            "INSERT INTO tasks (id, task, priority) VALUES (?, ?, ?)",
            (task_id, task, priority),
        )
        await self._db.commit()
        logger.info(f"Enqueued task {task_id!r} priority={priority}: {task[:60]}")
        return task_id

    async def dequeue(self) -> dict | None:
        """
        Atomically fetch and mark as 'running' the highest-priority pending task.
        Expires stale tasks first.  Returns None if queue is empty.
        """
        await self._expire_stale()

        async with self._db.execute(
            """
            SELECT id, task FROM tasks
            WHERE status = 'pending'
            ORDER BY priority DESC, created_at ASC
            LIMIT 1
            """
        ) as cur:
            row = await cur.fetchone()

        if row is None:
            return None

        task_id = row["id"]
        now = _utcnow()
        await self._db.execute(
            "UPDATE tasks SET status='running', picked_up_at=? WHERE id=? AND status='pending'",
            (now, task_id),
        )
        await self._db.commit()

        # Verify we won the race (another process might have dequeued it)
        async with self._db.execute(
            "SELECT id, task FROM tasks WHERE id=? AND status='running'", (task_id,)
        ) as cur:
            row = await cur.fetchone()

        if row is None:
            return None  # lost the race — caller should retry

        logger.info(f"Dequeued task {task_id!r}")
        return {"task_id": task_id, "task": row["task"]}

    async def complete(
        self,
        task_id: str,
        summary: str,
        screenshot: str,
        success: bool,
    ) -> bool:
        """Mark a task as done or failed.  Returns True if the row was updated."""
        status = "done" if success else "failed"
        now = _utcnow()
        cur = await self._db.execute(
            """
            UPDATE tasks
            SET status=?, summary=?, screenshot=?, success=?, completed_at=?
            WHERE id=?
            """,
            (status, summary, screenshot, success, now, task_id),
        )
        await self._db.commit()
        updated = cur.rowcount > 0
        if updated:
            logger.info(f"Completed task {task_id!r} status={status}")
        else:
            logger.warning(f"complete() called for unknown task_id {task_id!r}")
        return updated

    # ── Read operations ────────────────────────────────────────────────────

    async def get(self, task_id: str) -> dict | None:
        async with self._db.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def queue_depth(self) -> int:
        async with self._db.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='pending'"
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    # ── Stale-task expiry ──────────────────────────────────────────────────

    async def _expire_stale(self) -> None:
        now = _utcnow()
        await self._db.execute(
            """
            UPDATE tasks SET status='failed', summary='Expired: not picked up in time'
            WHERE status='pending'
              AND (julianday(?) - julianday(created_at)) * 86400 > ?
            """,
            (now, STALE_PENDING_S),
        )
        await self._db.execute(
            """
            UPDATE tasks SET status='failed', summary='Expired: PC did not return result'
            WHERE status='running'
              AND (julianday(?) - julianday(picked_up_at)) * 86400 > ?
            """,
            (now, STALE_RUNNING_S),
        )
        await self._db.commit()


# ── Module-level singleton ─────────────────────────────────────────────────

db = TaskDB()


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

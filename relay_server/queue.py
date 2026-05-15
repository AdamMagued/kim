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

devices
  id            TEXT PRIMARY KEY      — UUIDv4 device id
  device_token  TEXT UNIQUE NOT NULL  — long random secret used in X-API-Key
  device_name   TEXT NOT NULL         — human-readable label ("My iPhone")
  paired_at     TIMESTAMP             — when pairing completed
  last_seen     TIMESTAMP             — refreshed on each authenticated request

pending_pairings
  pair_code     TEXT PRIMARY KEY      — 6-char code shown in PC QR
  created_at    TIMESTAMP             — UTC, set on INSERT
  expires_at    TIMESTAMP             — set on INSERT (default 5 min ahead)
  claimed_at    TIMESTAMP             — NULL until phone redeems
  device_id     TEXT                  — set to the resulting device row id

Stale-task expiry
─────────────────
Tasks that stay in 'pending' longer than STALE_PENDING_S (default 5 min) or
in 'running' longer than STALE_RUNNING_S (default 10 min) are automatically
moved to 'failed' on every dequeue call.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import aiosqlite

logger = logging.getLogger(__name__)

from pathlib import Path

# Default DB is created next to queue.py if RELAY_DB_PATH isn't set
_DEFAULT_DB = str(Path(__file__).resolve().parent / "relay.db")
DB_PATH = os.environ.get("RELAY_DB_PATH", _DEFAULT_DB)

STALE_PENDING_S = int(os.environ.get("STALE_PENDING_S", 300))   # 5 min
STALE_RUNNING_S = int(os.environ.get("STALE_RUNNING_S", 600))   # 10 min

# How long a pair_code stays valid before we drop it. Short enough that a stolen
# QR screenshot is worthless, long enough that a normal user has time to point
# their camera at it.
PAIR_CODE_TTL_S = int(os.environ.get("PAIR_CODE_TTL_S", 300))   # 5 min

# Length of the alphabet-only pairing code we render in the QR. Six chars from
# an unambiguous alphabet gives ~10^9 possibilities — collisions are negligible
# during the 5-minute window and the code is short enough to retype by hand.
_PAIR_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_PAIR_CODE_LEN = 6

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

_CREATE_DEVICES = """
CREATE TABLE IF NOT EXISTS devices (
    id            TEXT PRIMARY KEY,
    device_token  TEXT UNIQUE NOT NULL,
    device_name   TEXT NOT NULL,
    paired_at     TIMESTAMP NOT NULL,
    last_seen     TIMESTAMP
);
"""

_CREATE_DEVICES_INDEX = """
CREATE INDEX IF NOT EXISTS idx_devices_token ON devices(device_token);
"""

_CREATE_PAIRINGS = """
CREATE TABLE IF NOT EXISTS pending_pairings (
    pair_code   TEXT PRIMARY KEY,
    created_at  TIMESTAMP NOT NULL,
    expires_at  TIMESTAMP NOT NULL,
    claimed_at  TIMESTAMP,
    device_id   TEXT
);
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
        await self._db.execute(_CREATE_DEVICES)
        await self._db.execute(_CREATE_DEVICES_INDEX)
        await self._db.execute(_CREATE_PAIRINGS)
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
        await self._db.execute("BEGIN IMMEDIATE")
        try:
            await self._expire_stale(commit=False)

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
                await self._db.commit()
                return None

            task_id = row["id"]
            task_text = row["task"]
            now = _utcnow()
            cur = await self._db.execute(
                "UPDATE tasks SET status='running', picked_up_at=? WHERE id=? AND status='pending'",
                (now, task_id),
            )
            if cur.rowcount != 1:
                await self._db.rollback()
                return None

            await self._db.commit()
        except Exception:
            await self._db.rollback()
            raise

        logger.info(f"Dequeued task {task_id!r}")
        return {"task_id": task_id, "task": task_text}

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

    # ── Pairing ────────────────────────────────────────────────────────────

    async def create_pairing(self) -> dict:
        """
        Generate a fresh pair_code and store it in `pending_pairings`. Returns
        `{"pair_code": str, "expires_at": iso-string}`. Caller (PC) renders the
        code as a QR. The phone redeems it via `complete_pairing()` within
        PAIR_CODE_TTL_S seconds.
        """
        await self._expire_stale_pairings(commit=False)
        # Loop on the (astronomically unlikely) collision case so callers can
        # rely on getting a unique code without retrying.
        for _ in range(8):
            pair_code = "".join(
                secrets.choice(_PAIR_CODE_ALPHABET) for _ in range(_PAIR_CODE_LEN)
            )
            now = datetime.now(timezone.utc)
            expires = now + timedelta(seconds=PAIR_CODE_TTL_S)
            try:
                await self._db.execute(
                    """
                    INSERT INTO pending_pairings (pair_code, created_at, expires_at)
                    VALUES (?, ?, ?)
                    """,
                    (pair_code, _iso(now), _iso(expires)),
                )
                await self._db.commit()
                logger.info(f"Pairing code created: {pair_code} (expires {_iso(expires)})")
                return {"pair_code": pair_code, "expires_at": _iso(expires)}
            except aiosqlite.IntegrityError:
                continue
        raise RuntimeError("Failed to allocate a unique pair_code after 8 attempts")

    async def complete_pairing(self, pair_code: str, device_name: str) -> dict | None:
        """
        Redeem a `pair_code` and create a device record. Returns
        `{"device_token": str, "device_id": str}` on success, or `None` if the
        code is unknown / expired / already claimed.
        """
        await self._expire_stale_pairings(commit=False)
        async with self._db.execute(
            "SELECT pair_code, expires_at, claimed_at FROM pending_pairings WHERE pair_code=?",
            (pair_code,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        if row["claimed_at"] is not None:
            return None
        # SQLite stores ISO strings; compare lexically is safe for our format.
        if _iso(datetime.now(timezone.utc)) > str(row["expires_at"]):
            return None

        device_id = uuid4().hex
        device_token = secrets.token_urlsafe(32)
        now = _iso(datetime.now(timezone.utc))
        await self._db.execute(
            """
            INSERT INTO devices (id, device_token, device_name, paired_at, last_seen)
            VALUES (?, ?, ?, ?, ?)
            """,
            (device_id, device_token, device_name, now, now),
        )
        await self._db.execute(
            "UPDATE pending_pairings SET claimed_at=?, device_id=? WHERE pair_code=?",
            (now, device_id, pair_code),
        )
        await self._db.commit()
        logger.info(f"Pairing {pair_code!r} completed → device {device_id} ({device_name!r})")
        return {"device_token": device_token, "device_id": device_id}

    async def get_pairing_status(self, pair_code: str) -> dict | None:
        """
        Return `{"claimed": bool, "device_name": str|None, "claimed_at": str|None,
        "expired": bool}` for the given code, or `None` if it doesn't exist.
        Used by the PC's UI to know when to dismiss the QR.
        """
        async with self._db.execute(
            """
            SELECT p.pair_code, p.expires_at, p.claimed_at, p.device_id, d.device_name
              FROM pending_pairings p
              LEFT JOIN devices d ON d.id = p.device_id
             WHERE p.pair_code = ?
            """,
            (pair_code,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        now_iso = _iso(datetime.now(timezone.utc))
        return {
            "claimed":     row["claimed_at"] is not None,
            "device_name": row["device_name"],
            "claimed_at":  row["claimed_at"],
            "expired":     row["claimed_at"] is None and now_iso > str(row["expires_at"]),
        }

    async def lookup_device_by_token(self, token: str) -> dict | None:
        """
        Return `{"id", "device_name", "paired_at"}` if `token` matches a paired
        device, else None. Also refreshes `last_seen` as a side effect so the
        admin UI can show "last active 12s ago".
        """
        if not token:
            return None
        async with self._db.execute(
            "SELECT id, device_name, paired_at FROM devices WHERE device_token=?",
            (token,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        await self._db.execute(
            "UPDATE devices SET last_seen=? WHERE id=?",
            (_iso(datetime.now(timezone.utc)), row["id"]),
        )
        await self._db.commit()
        return {"id": row["id"], "device_name": row["device_name"], "paired_at": row["paired_at"]}

    async def list_devices(self) -> list[dict]:
        async with self._db.execute(
            "SELECT id, device_name, paired_at, last_seen FROM devices ORDER BY paired_at DESC"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def revoke_device(self, device_id: str) -> bool:
        cur = await self._db.execute("DELETE FROM devices WHERE id=?", (device_id,))
        await self._db.commit()
        return cur.rowcount > 0

    async def _expire_stale_pairings(self, *, commit: bool = True) -> None:
        now_iso = _iso(datetime.now(timezone.utc))
        # Delete expired, unclaimed codes. Claimed rows stick around as an
        # audit trail; they're tiny and the device_id FK is useful for support.
        await self._db.execute(
            "DELETE FROM pending_pairings WHERE claimed_at IS NULL AND expires_at < ?",
            (now_iso,),
        )
        if commit:
            await self._db.commit()

    # ── Stale-task expiry ──────────────────────────────────────────────────

    async def _expire_stale(self, *, commit: bool = True) -> None:
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
        if commit:
            await self._db.commit()


# ── Module-level singleton ─────────────────────────────────────────────────

db = TaskDB()


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _iso(dt: datetime) -> str:
    """Format a tz-aware datetime to the same ISO shape `_utcnow()` produces.
    Pairing TTLs compare these lexically, so the format has to be identical."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

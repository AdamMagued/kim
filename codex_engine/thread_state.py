"""Cross-task browser-thread state for the codex bridge route.

Each Code-tab / CLI code-mode task runs in its own ``codex_bridge_service``
process, so continuity of the browser thread (system prompt sent, running
token estimate, pending compact handoff) must live on disk. One JSON sidecar
per (project cwd, provider) pair, stored under the Kim repo's ``kim_sessions/``
— never inside the user's project.

State shape:
    {
        "sent_instructions": bool,  # codex system prompt already in the thread
        "turns": int,               # browser sends since the thread started
        "est_tokens": int,          # cumulative estimated in+out tokens
        "handoff": str | None,      # compact summary to seed the next fresh chat
        "sandbox": str,             # permission fingerprint ("default"/"bypass")
                                    # the thread's instructions described
        "burned": bool,             # thread ignored the tool protocol even
                                    # after a format nudge — never resume it
        "codex_thread_id": str|None,  # app-server transport: codex's own
                                    # thread id, resumed across messages
        "codex_thread_cwd": str|None,  # cwd the codex thread was started in
                                    # (resume only when it still matches)
        "repairs": dict,            # Rb1 visibility counters: how often the
                                    # proxy salvaged prose into tool calls
                                    # ("salvages") or re-asked for the format
                                    # ("nudges") this thread
        "updated_at": str,          # ISO timestamp of last write
    }

The ``codex_thread_*`` keys survive ``reset_thread_state``: the browser
thread (model transport) and the codex thread (tool/transcript state on the
app-server transport) are independent context budgets — resetting one must
not amnesia the other.

Locking (LOW-MEDIUM #7): each Code-tab / CLI task is its own OS process, and
a caller's typical usage is load -> mutate the dict in memory -> save some
time later. Without a lock spanning that whole window, two turns for the
same (cwd, provider) thread launched close together can interleave their
load/save and lose one side's update (e.g. a stale ``codex_thread_id``
clobbering a freshly-started one). ``thread_state_lock()`` below is an
advisory cross-process file lock — the same primitive
``orchestrator/cron_store.py`` uses for its own JSON sidecar — that a
caller doing a load-modify-save cycle should hold for the whole cycle:

    with thread_state_lock(cwd, provider):
        state = load_thread_state(cwd, provider)
        state["turns"] = state.get("turns", 0) + 1
        save_thread_state(cwd, provider, state)

``load_thread_state``/``save_thread_state`` also take this lock internally
around their own single operation, so calling them standalone (outside an
explicit ``thread_state_lock()`` block) is still safe against a concurrent
writer — just not against the broader lost-update race, which needs the
caller to hold the lock across its own read-modify-write span. The lock is
re-entrant within one thread (nesting ``load_thread_state``/
``save_thread_state`` inside an outer ``thread_state_lock()`` block does not
self-deadlock): flock/O_CREAT|O_EXCL are per-open-file-description at the OS
level, not per-process, so a second independent acquisition from the same
call stack would otherwise block on itself.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("kim.codex_thread_state")

_REPO = Path(__file__).resolve().parent.parent
_STATE_DIR = _REPO / "kim_sessions" / "codex_threads"

_LOCK_TIMEOUT = 10.0  # seconds before giving up on lock acquisition

# Per-thread reentrancy tracking (see module docstring). Keyed by resolved
# lock-file path so nested acquisitions of the SAME sidecar's lock within one
# call stack are no-ops, while a different (cwd, provider) pair or a genuine
# concurrent acquisition (another thread/process) still serializes normally.
_lock_local = threading.local()


@contextlib.contextmanager
def _exclusive_lock(lock_path: Path, timeout: float = _LOCK_TIMEOUT):
    """Exclusive advisory lock around a sidecar load/save (or load-modify-save
    cycle, for callers that hold it across all three steps).

    Mirrors ``orchestrator/cron_store.py``'s ``_exclusive_lock`` so both JSON
    sidecar stores in this codebase use the same locking primitive:

    POSIX (macOS / Linux): fcntl.flock — blocks until acquired; released
    automatically when the file descriptor closes, so a crashed process never
    leaves a stale lock.

    Windows: spin on O_CREAT|O_EXCL. The lock file is removed in the finally
    block. A crashed process may leave a stale lock file; if it persists
    longer than *timeout* seconds the next caller raises TimeoutError.
    """
    key = str(lock_path)
    held = getattr(_lock_local, "paths", None)
    if held is None:
        held = set()
        _lock_local.paths = held
    if key in held:
        # Re-entrant: an outer `with` in this thread's call stack already
        # holds the real OS-level lock for this sidecar.
        yield
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    held.add(key)
    try:
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
                            f"thread_state: could not acquire lock {lock_path} within {timeout}s"
                        )
                    time.sleep(0.02)
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    lock_path.unlink()
        else:
            import fcntl  # noqa: PLC0415 — POSIX only
            with open(lock_path, "a") as fh:  # 'a' creates without truncating
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        held.discard(key)


def _state_path(cwd: str, provider: str) -> Path:
    digest = hashlib.sha256(f"{cwd}|{provider}".encode("utf-8")).hexdigest()[:16]
    return _STATE_DIR / f"{digest}.json"


def _lock_path(cwd: str, provider: str) -> Path:
    return _state_path(cwd, provider).with_suffix(".json.lock")


def thread_state_lock(cwd: str, provider: str, timeout: float = _LOCK_TIMEOUT):
    """Public: hold across a load -> mutate -> save cycle for one (cwd,
    provider) thread to close the lost-update race (#7). See module
    docstring for usage.
    """
    return _exclusive_lock(_lock_path(cwd, provider), timeout)


def load_thread_state(cwd: str, provider: str) -> dict:
    path = _state_path(cwd, provider)
    try:
        with _exclusive_lock(_lock_path(cwd, provider)):
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
    except Exception as e:
        logger.warning("Could not read codex thread state %s: %s", path, e)
    return {}


def save_thread_state(cwd: str, provider: str, state: dict) -> None:
    path = _state_path(cwd, provider)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(state)
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        with _exclusive_lock(_lock_path(cwd, provider)):
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
    except Exception as e:
        logger.warning("Could not write codex thread state %s: %s", path, e)


def reset_thread_state(
    cwd: str,
    provider: str,
    handoff: Optional[str] = None,
    *,
    preserve_codex_thread: bool = True,
) -> dict:
    """Reset accounting for a fresh browser thread, optionally carrying a handoff.

    The codex-side thread identity (app-server transport) is preserved by
    default. A genuinely new CLI session passes ``preserve_codex_thread=False``
    so neither side can resurrect the previous conversation.
    """
    # Hold the lock across the whole load -> mutate -> save cycle (#7): this
    # function is itself a read-modify-write, not just a caller of one, and
    # load_thread_state()/save_thread_state() re-entering the same lock below
    # is safe (see module docstring).
    with thread_state_lock(cwd, provider):
        previous = load_thread_state(cwd, provider)
        state = {
            "sent_instructions": False,
            "turns": 0,
            "est_tokens": 0,
            "handoff": (handoff or "").strip() or None,
        }
        if preserve_codex_thread:
            for key in ("codex_thread_id", "codex_thread_cwd"):
                if previous.get(key):
                    state[key] = previous[key]
        save_thread_state(cwd, provider, state)
    return state

"""Regression tests for F-J-4: async fsync offload in SessionStore.

The append path offloads its per-line ``os.fsync`` to a single-worker
executor so it never blocks the async agent event loop.  The dangerous
failure mode this guards against is a *self-join deadlock*: an earlier
implementation had ``_sync_write`` (running ON the single executor thread)
read ``self.session_file``, and the ``session_file`` property called
``self.flush()``, which waits on the pending write futures — i.e. the one
worker thread waiting for itself.  On a ``max_workers=1`` pool that hangs
forever.

Every test here that touches the write path is wrapped in a wall-clock
deadline via ``concurrent.futures.Future.result(timeout=...)`` so a
regression surfaces as a **TimeoutError failure**, never an infinite hang
that wedges the whole suite.
"""

import concurrent.futures
import json
import tempfile
import threading
from pathlib import Path

import pytest

from orchestrator.session_store import SessionStore, _write_tls


# Any single operation exercised below completes in well under a second on a
# healthy machine; give generous slack but still bounded so a deadlock fails.
_DEADLINE_S = 20.0


def _run_with_deadline(fn, *args, **kwargs):
    """Run ``fn`` on a helper thread and fail (not hang) if it deadlocks."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(fn, *args, **kwargs)
        try:
            return fut.result(timeout=_DEADLINE_S)
        except concurrent.futures.TimeoutError:
            pytest.fail(
                "SessionStore operation did not finish within "
                f"{_DEADLINE_S}s — likely the F-J-4 self-join deadlock regressed."
            )


def test_flush_drains_queued_writes_durably():
    """append_message queues async; flush() must make the bytes readable."""
    def body():
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(base_dir=Path(tmp), session_id="offload_durable")
            for i in range(25):
                store.append_message({"role": "user", "content": f"m{i}"})
            store.flush()
            # Read the private path directly (no property flush) — everything
            # queued before flush() returned must be on disk now.
            lines = store._session_file.read_text(encoding="utf-8").strip().splitlines()
            assert len(lines) == 25
            assert json.loads(lines[0])["content"] == "m0"
            assert json.loads(lines[-1])["content"] == "m24"
            store.close()
    _run_with_deadline(body)


def test_session_file_property_flushes_without_deadlock():
    """Reading the session_file property drains pending writes and never hangs.

    On the broken implementation the property flush re-entered the single
    executor worker (self-join) — here it must return the fully-written file.
    """
    def body():
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(base_dir=Path(tmp), session_id="offload_prop")
            for i in range(15):
                store.append_message({"role": "user", "content": f"p{i}"})
            # No explicit flush — the property is the sync barrier.
            text = store.session_file.read_text(encoding="utf-8")
            assert text.count("\n") == 15
            store.close()
    _run_with_deadline(body)


def test_load_session_sees_queued_writes():
    """Static load_session() drains the live store so a just-appended line
    is visible even without an explicit flush()."""
    def body():
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = SessionStore(base_dir=base, session_id="offload_load")
            store.append_message({"role": "user", "content": "hello"})
            store.append_message({"role": "assistant", "content": "world"})
            loaded = SessionStore.load_session("offload_load", base_dir=base)
            assert [m["content"] for m in loaded] == ["hello", "world"]
            store.close()
    _run_with_deadline(body)


def test_rotation_path_does_not_deadlock():
    """The rotation branch runs inside _sync_write on the executor thread; it
    must use the private path and never wait on its own pool.

    Force the size cap tiny so the very next append rotates, exercising the
    exact code path that dereferenced the flushing property in the broken
    version.
    """
    def body(monkeypatched_cap):
        import orchestrator.session_store as ss
        original = ss._MAX_SESSION_BYTES
        ss._MAX_SESSION_BYTES = monkeypatched_cap
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = SessionStore(base_dir=Path(tmp), session_id="offload_roll")
                # First write pushes cached size over the (tiny) cap.
                store.append_message({"role": "user", "content": "x" * 200})
                store.flush()
                # Next write should trigger a rotation of the live file.
                store.append_message({"role": "user", "content": "y" * 200})
                store.flush()
                rolls = list(Path(store.session_dir).glob("offload_roll.roll.*.jsonl"))
                assert rolls, "expected a rolled segment after crossing the size cap"
                # Full transcript still recoverable across the roll.
                loaded = SessionStore.load_session("offload_roll", base_dir=Path(tmp))
                assert len(loaded) == 2
                store.close()
        finally:
            ss._MAX_SESSION_BYTES = original
    _run_with_deadline(body, 100)


def test_flush_is_noop_from_executor_thread():
    """flush() called while the in_executor marker is set must return
    immediately (no wait) — this is the guard that prevents the self-join."""
    def body():
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(base_dir=Path(tmp), session_id="offload_reentry")
            store.append_message({"role": "user", "content": "queued"})
            # Simulate being on the executor thread: the guard must short-circuit
            # flush() so it does NOT wait on the (still-pending) future.
            _write_tls.in_executor = True
            try:
                store.flush()  # must return promptly, not block on the pending write
            finally:
                _write_tls.in_executor = False
            # Real flush from a normal thread still drains it.
            store.flush()
            assert store._session_file.read_text(encoding="utf-8").strip()
            store.close()
    _run_with_deadline(body)


def test_concurrent_appends_and_reads_stay_consistent():
    """Hammer the store from several threads while reading via the property;
    must neither deadlock nor lose lines."""
    def body():
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(base_dir=Path(tmp), session_id="offload_race")
            n_threads, per = 4, 20

            def worker(tid):
                for i in range(per):
                    store.append_message({"role": "user", "content": f"{tid}-{i}"})
                    if i % 5 == 0:
                        # property read = implicit flush; must not deadlock
                        _ = store.session_file.exists()

            threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            store.flush()
            loaded = SessionStore.load_session("offload_race", base_dir=Path(tmp))
            assert len(loaded) == n_threads * per
            store.close()
    _run_with_deadline(body)

"""Regression tests for persistence/lifecycle/logging plumbing
(cobweb findings 4.1–4.3, 5.1, 6.1–6.6)."""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import date, timedelta
from pathlib import Path

import pytest


# ── 4.2 fresh session ids avoid on-disk collisions ──

def test_fresh_session_id_regenerates_on_collision(tmp_path, monkeypatch):
    from orchestrator import session_store as ss

    # Force uuid4().hex to a fixed value so the first candidate always collides.
    seq = iter(["deadbeef" + "0" * 24, "deadbeef" + "0" * 24, "cafef00d" + "0" * 24])

    class _FakeUUID:
        def __init__(self, h):
            self.hex = h

    monkeypatch.setattr(ss, "uuid4", lambda: _FakeUUID(next(seq)))

    # Pre-create a session file that "deadbeef" (first 8 hex) would collide with.
    day = tmp_path / date.today().isoformat()
    day.mkdir(parents=True)
    (day / "deadbeef.jsonl").write_text("{}\n", encoding="utf-8")

    store = ss.SessionStore(base_dir=tmp_path)
    # Must NOT have adopted the pre-existing "deadbeef" file.
    assert store.session_id != "deadbeef"


def test_explicit_session_id_is_respected(tmp_path):
    from orchestrator.session_store import SessionStore
    store = SessionStore(base_dir=tmp_path, session_id="myresume")
    assert store.session_id == "myresume"


# ── 4.1 prune skips recently-touched (possibly-resumed) session files ──

def test_prune_skips_recent_files(tmp_path):
    from orchestrator.session_store import SessionStore

    old_day = (date.today() - timedelta(days=10)).isoformat()
    d = tmp_path / old_day
    d.mkdir(parents=True)
    f = d / "sess1.jsonl"
    msg = {"role": "user", "content": [{"type": "image", "data": "AAAA"}]}
    f.write_text(json.dumps(msg) + "\n", encoding="utf-8")
    # Fresh mtime (now) → the strip pass must skip it to avoid racing a resume.
    os.utime(f, (time.time(), time.time()))

    result = SessionStore.prune_old_sessions(
        max_age_days=365, screenshot_strip_age_days=1, base_dir=tmp_path
    )
    assert result["stripped"] == 0
    # The image payload is still intact.
    assert "image" in f.read_text(encoding="utf-8")


def test_prune_strips_old_untouched_files(tmp_path):
    from orchestrator.session_store import SessionStore

    old_day = (date.today() - timedelta(days=10)).isoformat()
    d = tmp_path / old_day
    d.mkdir(parents=True)
    f = d / "sess2.jsonl"
    msg = {"role": "user", "content": [{"type": "image", "data": "AAAA"}]}
    f.write_text(json.dumps(msg) + "\n", encoding="utf-8")
    old = time.time() - 7200  # 2h ago → outside the recency window
    os.utime(f, (old, old))

    result = SessionStore.prune_old_sessions(
        max_age_days=365, screenshot_strip_age_days=1, base_dir=tmp_path
    )
    assert result["stripped"] == 1
    assert "image" not in f.read_text(encoding="utf-8")


# ── 4.3 cron store fsyncs before the atomic rename ──

def test_cron_save_fsyncs(tmp_path, monkeypatch):
    from orchestrator import cron_store as cs

    calls = {"fsync": 0}
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.__setitem__("fsync", calls["fsync"] + 1), real_fsync(fd))[1])

    store = cs.CronStore(store_file=tmp_path / "sched.json")
    store.add("do a thing", "@hourly")
    assert calls["fsync"] >= 1
    # And the file round-trips.
    assert store.list_tasks()[0].task == "do a thing"


# ── 5.1 process-tree kill helper ──

def test_kill_process_tree_missing_pid_is_safe():
    from orchestrator.scheduled_runner import _kill_process_tree
    # A PID that does not exist must not raise.
    _kill_process_tree(2_000_000_000)


def test_scheduled_spawn_uses_process_group(monkeypatch, tmp_path):
    # The Popen call must request a new session/process group so the reaper can
    # kill the whole tree (5.1). Verify via the kwargs passed to Popen.
    import orchestrator.scheduled_runner as sr

    captured = {}

    class _FakeProc:
        pid = 4321

    def _fake_popen(args, **kwargs):
        captured.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(sr.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(sr, "_preflight", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_register_agent_pid", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_reap_stale_agents", lambda *a, **k: None)

    store = sr.CronStore(store_file=tmp_path / "s.json")
    store.add("run something", "@hourly")
    # Make it due.
    monkeypatch.setattr(sr, "find_interpreter", lambda root: "/usr/bin/python3")
    from datetime import datetime, timezone
    future = datetime.now(timezone.utc) + timedelta(days=400)
    sr.run_next_due_task(store_file=tmp_path / "s.json", kim_root=tmp_path, as_of=future)

    if os.name == "posix":
        assert captured.get("start_new_session") is True


# ── 6.3 truncated web_observe JSON does not induce a hard-block loop ──

def test_truncated_web_observe_marks_failed():
    from orchestrator.interaction_policy import InteractionPolicy
    p = InteractionPolicy()
    p.after_tool("web_observe", {}, "WEB_OBSERVATION_JSON: {this is truncated")
    assert p.web_observe_failed is True
    assert p.web_generation == 0
    assert p.known_web_element_ids == set()


def test_valid_web_observe_json_registers_ids():
    from orchestrator.interaction_policy import InteractionPolicy
    p = InteractionPolicy()
    payload = json.dumps({
        "visible_interactive_elements": [{"element_id": "w1"}, {"element_id": "w2"}],
        "observation_generation": 3,
    })
    p.after_tool("web_observe", {}, "WEB_OBSERVATION_JSON: " + payload)
    assert p.web_observe_failed is False
    assert p.known_web_element_ids == {"w1", "w2"}
    assert p.web_generation == 3


# ── 6.4 denial/timeout prefixes count as non-state-changing ──

@pytest.mark.parametrize("prefix", ["HITL_DENIED", "POLICY_DENIED", "TIMEOUT", "ERROR", "PERMISSION_ERROR"])
def test_failure_prefixes_do_not_dirty_state(prefix):
    from orchestrator.interaction_policy import InteractionPolicy
    p = InteractionPolicy()
    p.web_state_dirty = False
    p.after_tool("web_click", {"element_id": "w1"}, f"{prefix}: nope")
    assert p.web_state_dirty is False


def test_successful_web_click_dirties_state():
    from orchestrator.interaction_policy import InteractionPolicy
    p = InteractionPolicy()
    p.after_tool("web_click", {"element_id": "w1"}, "clicked ok")
    assert p.web_state_dirty is True


# ── 6.5 bounded log queue drops oldest ──

def test_log_queue_bounded_drops_oldest():
    from orchestrator.ui_bridge import UIBridge, LOG_QUEUE_MAXSIZE
    b = UIBridge()
    for i in range(LOG_QUEUE_MAXSIZE + 50):
        b.log("INFO", f"msg{i}")
    assert b.log_queue.qsize() <= LOG_QUEUE_MAXSIZE
    # Oldest entries were evicted; the newest survives.
    items = []
    while not b.log_queue.empty():
        items.append(b.log_queue.get_nowait())
    assert ("INFO", f"msg{LOG_QUEUE_MAXSIZE + 49}") in items
    assert ("INFO", "msg0") not in items


# ── 6.1 StdinPump rebinds to a new event loop ──

def test_pump_rebinds_to_new_loop():
    from orchestrator.ui_bridge import StdinPump
    pump = StdinPump()
    stale = asyncio.new_event_loop()
    pump._started = True
    pump._loop = stale

    async def go():
        pump.start()  # should rebind to the running loop
        return asyncio.get_running_loop()

    running = asyncio.run(go())
    assert pump._loop is running
    stale.close()


# ── 6.2 inject_decline wakes a pending approval wait ──

def test_inject_decline_wakes_pending_approval():
    from orchestrator.ui_bridge import StdinPump

    async def go():
        pump = StdinPump()
        pump._started = True
        pump._loop = asyncio.get_running_loop()
        # Fire the injection AFTER next_approval has drained + started waiting.
        pump._loop.call_later(0.05, pump.inject_decline)
        return await pump.next_approval(timeout=2.0)

    data = asyncio.run(go())
    assert str(data.get("decision")) == "decline"


# ── 6.6 logger: Google key redaction + UTC-aligned retention ──

def test_google_api_key_redacted():
    from mcp_server.logger import _redact
    secret = "AIza" + "B" * 35
    assert secret not in _redact(f"key={secret}")
    assert "REDACTED" in _redact(f"key={secret}")


def test_retention_uses_utc_date(tmp_path):
    from datetime import datetime, timezone
    from mcp_server.logger import apply_log_retention

    # A file dated well in the past must be deleted; a fresh one kept.
    old = datetime.now(timezone.utc).date() - timedelta(days=30)
    (tmp_path / f"kim_{old.isoformat()}.jsonl").write_text("{}\n", encoding="utf-8")
    today = datetime.now(timezone.utc).date()
    keep = tmp_path / f"kim_{today.isoformat()}.jsonl"
    keep.write_text("{}\n", encoding="utf-8")

    deleted = apply_log_retention(log_dir=str(tmp_path), keep_days=7)
    assert deleted == 1
    assert keep.exists()

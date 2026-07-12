"""F-J-6: a detached scheduled agent must self-enforce its wall-clock cap so a
wedged run is bounded even when the desktop app (and its external reaper) is
closed.

The runner passes the cap via KIM_AGENT_MAX_WALL_SECONDS; the agent entrypoint
starts a daemon watchdog that terminates the process (and its process-group
subtree) when the deadline elapses, and cancels it on normal completion.
"""

from __future__ import annotations

import threading
import time

from orchestrator.cli import (
    _wall_clock_deadline_from_env,
    maybe_start_wall_clock_watchdog,
    start_wall_clock_watchdog,
)
from orchestrator.scheduled_runner import _AGENT_MAX_WALL_SECONDS, _build_subprocess_env


def test_deadline_parsing(monkeypatch):
    monkeypatch.delenv("KIM_AGENT_MAX_WALL_SECONDS", raising=False)
    assert _wall_clock_deadline_from_env() is None

    monkeypatch.setenv("KIM_AGENT_MAX_WALL_SECONDS", "1800")
    assert _wall_clock_deadline_from_env() == 1800.0

    monkeypatch.setenv("KIM_AGENT_MAX_WALL_SECONDS", "0")
    assert _wall_clock_deadline_from_env() is None

    monkeypatch.setenv("KIM_AGENT_MAX_WALL_SECONDS", "-5")
    assert _wall_clock_deadline_from_env() is None

    monkeypatch.setenv("KIM_AGENT_MAX_WALL_SECONDS", "not-a-number")
    assert _wall_clock_deadline_from_env() is None


def test_watchdog_fires_after_deadline():
    """The watchdog must invoke its kill function once the deadline elapses."""
    fired = threading.Event()
    timer = start_wall_clock_watchdog(0.05, kill_fn=fired.set)
    try:
        assert fired.wait(timeout=3.0), "watchdog did not fire after its deadline"
    finally:
        timer.cancel()


def test_watchdog_cancel_prevents_fire():
    """A run that completes in time cancels the watchdog; it must not fire."""
    fired = threading.Event()
    timer = start_wall_clock_watchdog(0.5, kill_fn=fired.set)
    timer.cancel()
    assert not fired.wait(timeout=1.0), "cancelled watchdog fired anyway"


def test_maybe_start_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("KIM_AGENT_MAX_WALL_SECONDS", raising=False)
    assert maybe_start_wall_clock_watchdog() is None


def test_maybe_start_returns_timer_when_set(monkeypatch):
    # Long enough that it never actually fires during the test.
    monkeypatch.setenv("KIM_AGENT_MAX_WALL_SECONDS", "3600")
    timer = maybe_start_wall_clock_watchdog()
    assert isinstance(timer, threading.Timer)
    timer.cancel()


def test_runner_exports_wall_clock_cap_to_agent_env(tmp_path):
    """The scheduled runner must hand the cap to the detached agent so the
    self-watchdog can arm — this is the wiring that makes F-J-6 work end-to-end."""
    env = _build_subprocess_env(tmp_path)
    assert env["KIM_AGENT_MAX_WALL_SECONDS"] == str(int(_AGENT_MAX_WALL_SECONDS))
    # And the value round-trips through the agent-side parser to a live deadline.
    import os

    prev = os.environ.get("KIM_AGENT_MAX_WALL_SECONDS")
    os.environ["KIM_AGENT_MAX_WALL_SECONDS"] = env["KIM_AGENT_MAX_WALL_SECONDS"]
    try:
        assert _wall_clock_deadline_from_env() == float(_AGENT_MAX_WALL_SECONDS)
    finally:
        if prev is None:
            os.environ.pop("KIM_AGENT_MAX_WALL_SECONDS", None)
        else:
            os.environ["KIM_AGENT_MAX_WALL_SECONDS"] = prev

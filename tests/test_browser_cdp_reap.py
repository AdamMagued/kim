"""F-J-3: the auto-launched detached CDP Chrome is tracked so it can be reaped.
F-I-4: its debug port is bound to loopback only (unauthenticated endpoint)."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.providers.browser import provider as bp
from orchestrator.providers.browser.provider import (
    BrowserProvider,
    reap_launched_cdp_chrome,
)


def _provider(project_root):
    return BrowserProvider({"project_root": str(project_root), "browser_provider": {}})


def _registry_path(project_root):
    return project_root / "sessions" / ".kim_cdp_chrome.json"


def test_reap_kills_recorded_live_process(tmp_path, monkeypatch):
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    provider = _provider(tmp_path)

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        provider._record_launched_chrome(proc.pid, 9222)
        reg = _registry_path(provider._project_root)
        assert reg.exists()
        assert json.loads(reg.read_text())["pid"] == proc.pid

        killed = reap_launched_cdp_chrome(provider._project_root)
        assert killed is True
        proc.wait(timeout=5)  # SIGTERM terminated it
        assert not reg.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_reap_with_no_registry_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    assert reap_launched_cdp_chrome(tmp_path) is False


def test_reap_clears_stale_dead_pid(tmp_path, monkeypatch):
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    provider = _provider(tmp_path)
    # A PID that has already exited.
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=5)
    provider._record_launched_chrome(dead.pid, 9222)
    reg = _registry_path(provider._project_root)
    assert reg.exists()

    killed = reap_launched_cdp_chrome(provider._project_root)
    assert killed is False  # nothing live to signal
    assert not reg.exists()  # but the stale entry is cleared


def test_launch_binds_loopback_and_records_pid(tmp_path, monkeypatch):
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    provider = _provider(tmp_path)

    popen_mock = MagicMock(return_value=SimpleNamespace(pid=4242, poll=lambda: None))
    pw = SimpleNamespace(
        chromium=SimpleNamespace(connect_over_cdp=AsyncMock(side_effect=Exception("no cdp")))
    )

    with patch.object(provider, "_chrome_executable", return_value="/fake/chrome"), \
         patch.object(bp.subprocess, "Popen", popen_mock), \
         patch.object(bp.asyncio, "sleep", AsyncMock()):
        result = asyncio.run(provider._launch_headed_chrome(pw, 9222))

    assert result is None  # connect never succeeded (fake)
    popen_mock.assert_called_once()
    args = popen_mock.call_args.args[0]
    # F-I-4: the debug port is explicitly bound to loopback.
    assert "--remote-debugging-address=127.0.0.1" in args
    assert "--remote-debugging-port=9222" in args
    # F-J-3: the PID was recorded for later reaping.
    reg = _registry_path(provider._project_root)
    assert reg.exists()
    assert json.loads(reg.read_text())["pid"] == 4242


def test_launch_does_not_stack_a_second_chrome_when_prior_alive(tmp_path, monkeypatch):
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    provider = _provider(tmp_path)
    # A previously-launched Chrome that is still alive.
    provider._chrome_proc = SimpleNamespace(pid=111, poll=lambda: None)

    popen_mock = MagicMock()
    pw = SimpleNamespace(
        chromium=SimpleNamespace(connect_over_cdp=AsyncMock(side_effect=Exception("no cdp")))
    )
    with patch.object(provider, "_chrome_executable", return_value="/fake/chrome"), \
         patch.object(bp.subprocess, "Popen", popen_mock), \
         patch.object(bp.asyncio, "sleep", AsyncMock()):
        asyncio.run(provider._launch_headed_chrome(pw, 9222))

    popen_mock.assert_not_called()  # reused the live handle, no orphan stack

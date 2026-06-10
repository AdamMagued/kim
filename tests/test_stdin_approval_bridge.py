"""
Tests for StdinApprovalBridge — the Tauri-mode HITL approval mechanism.

StdinApprovalBridge.confirm_action() waits for a JSON line on stdin:
    {"type": "hitl_approve", "approved": true|false}
and returns the approved boolean. On timeout or parse error it returns False.
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _stub_heavy_modules():
    """Ensure heavy runtime deps don't block import of orchestrator modules."""
    import types
    import sys as _sys

    def _stub(name):
        m = types.ModuleType(name)
        return m

    for mod in ("mss", "pynput", "pynput.mouse", "pynput.keyboard",
                "pyautogui", "PIL", "PIL.Image", "sounddevice", "soundfile",
                "kokoro", "pygetwindow"):
        if mod not in _sys.modules:
            _sys.modules[mod] = _stub(mod)

    try:
        import mcp  # noqa: F401
    except ImportError:
        from unittest.mock import MagicMock
        for mod in ("mcp", "mcp.client", "mcp.client.stdio"):
            if mod not in _sys.modules:
                _sys.modules[mod] = _stub(mod)
        _sys.modules["mcp"].ClientSession = MagicMock
        _sys.modules["mcp"].StdioServerParameters = MagicMock
        _sys.modules["mcp.client.stdio"].stdio_client = MagicMock()


def _make_bridge():
    from orchestrator.ui_bridge import StdinApprovalBridge
    return StdinApprovalBridge()


def _run(coro):
    return asyncio.run(coro)


# ── confirm_action returns True when stdin sends approved=true ─────────────


def test_approve_true():
    bridge = _make_bridge()
    fake_stdin = io.StringIO(json.dumps({"type": "hitl_approve", "approved": True}) + "\n")
    with patch.object(sys, "stdin", fake_stdin):
        result = _run(bridge.confirm_action("run_command", {}))
    assert result is True


def test_approve_false():
    bridge = _make_bridge()
    fake_stdin = io.StringIO(json.dumps({"type": "hitl_approve", "approved": False}) + "\n")
    with patch.object(sys, "stdin", fake_stdin):
        result = _run(bridge.confirm_action("delete_file", {}))
    assert result is False


# ── cancelled bridge denies immediately ───────────────────────────────────


def test_cancelled_denies_immediately():
    bridge = _make_bridge()
    bridge.cancel()
    # Even if stdin would block, cancel short-circuits to False
    result = _run(bridge.confirm_action("any_tool", {}))
    assert result is False


# ── malformed JSON line returns False ────────────────────────────────────────


def test_malformed_json_returns_false():
    bridge = _make_bridge()
    fake_stdin = io.StringIO("not-json\n")
    with patch.object(sys, "stdin", fake_stdin):
        result = _run(bridge.confirm_action("run_command", {}))
    assert result is False


# ── empty line returns False ────────────────────────────────────────────────


def test_empty_line_returns_false():
    bridge = _make_bridge()
    fake_stdin = io.StringIO("\n")
    with patch.object(sys, "stdin", fake_stdin):
        result = _run(bridge.confirm_action("run_command", {}))
    assert result is False


# ── EOF returns False ────────────────────────────────────────────────────────


def test_eof_returns_false():
    bridge = _make_bridge()
    fake_stdin = io.StringIO("")  # immediate EOF
    with patch.object(sys, "stdin", fake_stdin):
        result = _run(bridge.confirm_action("run_command", {}))
    assert result is False


# ── KimAgent auto-wires StdinApprovalBridge when KIM_TAURI_MODE=1 ─────────
# These tests call KimAgent.__init__ directly (make_test_agent bypasses __init__).

def _make_agent_via_init(monkeypatch, tauri_mode=None, hitl_threshold=None):
    """Call KimAgent.__init__ with minimal mocks to test auto-wire logic."""
    from unittest.mock import MagicMock
    from orchestrator.agent import KimAgent
    from orchestrator.providers.fake import FakeProvider

    if tauri_mode is not None:
        monkeypatch.setenv("KIM_TAURI_MODE", tauri_mode)
    else:
        monkeypatch.delenv("KIM_TAURI_MODE", raising=False)

    if hitl_threshold is not None:
        monkeypatch.setenv("KIM_HITL_RISK_THRESHOLD", hitl_threshold)
    else:
        monkeypatch.delenv("KIM_HITL_RISK_THRESHOLD", raising=False)

    config = {
        "max_iterations": 5,
        "screenshot_scale": 0.75,
        "memory_max_messages": 40,
        "memory_keep_screenshots": 4,
        "context_budget_tokens": 100_000,
    }
    return KimAgent(config=config, session=MagicMock(), provider=FakeProvider())


def test_auto_wire_stdin_bridge(monkeypatch):
    """KimAgent.__init__ should auto-assign StdinApprovalBridge when
    KIM_TAURI_MODE=1 and KIM_HITL_RISK_THRESHOLD is set."""
    from orchestrator.ui_bridge import StdinApprovalBridge
    agent = _make_agent_via_init(monkeypatch, tauri_mode="1", hitl_threshold="high")
    assert isinstance(agent._ui_bridge, StdinApprovalBridge)


def test_no_auto_wire_without_threshold(monkeypatch):
    """Without KIM_HITL_RISK_THRESHOLD, _ui_bridge stays None."""
    agent = _make_agent_via_init(monkeypatch, tauri_mode="1", hitl_threshold=None)
    assert agent._ui_bridge is None


def test_no_auto_wire_without_tauri_mode(monkeypatch):
    """Without KIM_TAURI_MODE, _ui_bridge stays None even if threshold is set."""
    agent = _make_agent_via_init(monkeypatch, tauri_mode=None, hitl_threshold="high")
    assert agent._ui_bridge is None

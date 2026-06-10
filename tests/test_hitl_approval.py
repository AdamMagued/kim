"""
Tests for the interactive HITL approval gate wired into KimAgent._execute_tool().

Covers:
- _resolve_hitl_threshold() config/env resolution
- allow path  (confirm_action returns True  → tool executes)
- deny path   (confirm_action returns False → HITL_DENIED returned, tool not called)
- threshold filtering (low-risk tool with "high" threshold → not asked)
- guard conditions (no bridge, preview mode → gate does not fire)
- block_high_risk coexistence (interactive approval overrides hard-block)

No real MCP subprocess, no network, no screen/keyboard access.
Heavy optional imports are stubbed before agent.py is loaded.
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# Stub heavy runtime modules
# ---------------------------------------------------------------------------

def _stub(name: str, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


for _mod in (
    "mcp", "mcp.client", "mcp.client.stdio",
    "mcp.types", "mss", "pynput", "pynput.mouse", "pynput.keyboard",
    "pyautogui", "PIL", "PIL.Image", "sounddevice", "soundfile",
    "kokoro", "pygetwindow",
):
    if _mod not in sys.modules:
        sys.modules[_mod] = _stub(_mod)

_mcp = sys.modules["mcp"]
_mcp.ClientSession = MagicMock
_mcp.StdioServerParameters = MagicMock
sys.modules["mcp.client.stdio"].stdio_client = MagicMock()

try:
    import yaml  # noqa: F401
except ImportError:
    sys.modules["yaml"] = _stub("yaml", safe_load=lambda *a, **k: {})

try:
    from dotenv import load_dotenv  # noqa: F401
except ImportError:
    sys.modules["dotenv"] = _stub("dotenv", load_dotenv=lambda *a, **k: None)


# ---------------------------------------------------------------------------
# Agent + policy imports (after stubs)
# ---------------------------------------------------------------------------

from orchestrator.agent import _resolve_hitl_threshold  # noqa: E402
from orchestrator.interaction_policy import InteractionPolicy  # noqa: E402
from orchestrator.ui_bridge import UIBridge  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool_result(text: str = "tool_output"):
    """Return a minimal mock MCP call_tool result."""
    content_item = MagicMock()
    content_item.text = text
    result = MagicMock()
    result.content = [content_item]
    return result


def _make_agent(
    hitl_threshold: str | None = None,
    block_high_risk: bool = False,
    has_bridge: bool = True,
    preview_mode: bool = False,
):
    """Create a minimal KimAgent with controlled HITL settings."""
    from orchestrator.agent import KimAgent

    agent = KimAgent.__new__(KimAgent)
    agent._hitl_risk_threshold = hitl_threshold
    agent._interaction_policy = InteractionPolicy(block_high_risk=block_high_risk)
    agent.config = {}

    if has_bridge:
        bridge = UIBridge()
        bridge.preview_mode = preview_mode
        agent._ui_bridge = bridge
    else:
        agent._ui_bridge = None

    agent._log = lambda level, msg: None
    agent._session_store = MagicMock()
    agent._session_store.append_tool_event = MagicMock()

    agent.session = MagicMock()
    agent.session.call_tool = AsyncMock(return_value=_make_tool_result())

    return agent


def _run(coro):
    """Run a coroutine in a fresh event loop, safe regardless of pytest order."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _resolve_hitl_threshold — config / env resolution
# ---------------------------------------------------------------------------

class ResolveHitlThresholdTests(unittest.TestCase):

    def test_default_returns_none(self):
        self.assertIsNone(_resolve_hitl_threshold({}))

    def test_high_from_config(self):
        self.assertEqual(_resolve_hitl_threshold({"hitl_risk_threshold": "high"}), "high")

    def test_medium_from_config(self):
        self.assertEqual(_resolve_hitl_threshold({"hitl_risk_threshold": "medium"}), "medium")

    def test_low_from_config(self):
        self.assertEqual(_resolve_hitl_threshold({"hitl_risk_threshold": "low"}), "low")

    def test_high_from_env(self):
        self.assertEqual(_resolve_hitl_threshold({}, env_val="high"), "high")

    def test_medium_from_env(self):
        self.assertEqual(_resolve_hitl_threshold({}, env_val="MEDIUM"), "medium")

    def test_config_wins_over_env(self):
        result = _resolve_hitl_threshold({"hitl_risk_threshold": "medium"}, env_val="high")
        self.assertEqual(result, "medium")

    def test_invalid_value_returns_none(self):
        self.assertIsNone(_resolve_hitl_threshold({"hitl_risk_threshold": "extreme"}))

    def test_invalid_env_returns_none(self):
        self.assertIsNone(_resolve_hitl_threshold({}, env_val="critical"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(_resolve_hitl_threshold({"hitl_risk_threshold": ""}))

    def test_whitespace_normalized(self):
        self.assertEqual(_resolve_hitl_threshold({"hitl_risk_threshold": "  HIGH  "}), "high")


# ---------------------------------------------------------------------------
# Allow / deny paths
# ---------------------------------------------------------------------------

class HitlApprovalExecuteToolTests(unittest.TestCase):

    def test_allow_path_executes_tool(self):
        """confirm_action → True: tool runs and output is returned."""
        agent = _make_agent(hitl_threshold="high")
        agent._ui_bridge.confirm_action = AsyncMock(return_value=True)

        result = _run(agent._execute_tool("run_command", {"cmd": "echo hi"}))

        agent._ui_bridge.confirm_action.assert_called_once_with(
            "run_command", {"cmd": "echo hi"}
        )
        agent.session.call_tool.assert_called_once()
        self.assertEqual(result, "tool_output")

    def test_deny_path_returns_hitl_denied(self):
        """confirm_action → False: HITL_DENIED is returned, tool is NOT called."""
        agent = _make_agent(hitl_threshold="high")
        agent._ui_bridge.confirm_action = AsyncMock(return_value=False)

        result = _run(agent._execute_tool("run_command", {"cmd": "rm -rf /"}))

        agent._ui_bridge.confirm_action.assert_called_once()
        agent.session.call_tool.assert_not_called()
        self.assertIn("HITL_DENIED", result)
        self.assertIn("run_command", result)

    def test_deny_message_includes_risk_reason(self):
        agent = _make_agent(hitl_threshold="high")
        agent._ui_bridge.confirm_action = AsyncMock(return_value=False)

        result = _run(agent._execute_tool("delete_file", {"path": "foo.txt"}))

        self.assertIn("file_deletion", result)

    def test_low_risk_not_asked_when_threshold_is_high(self):
        """read_file is low-risk — confirm_action must not be called."""
        agent = _make_agent(hitl_threshold="high")
        agent._ui_bridge.confirm_action = AsyncMock(return_value=True)

        result = _run(agent._execute_tool("read_file", {"path": "foo.txt"}))

        agent._ui_bridge.confirm_action.assert_not_called()
        self.assertEqual(result, "tool_output")

    def test_medium_risk_not_asked_when_threshold_is_high(self):
        """write_file is medium-risk — not asked when threshold is 'high'."""
        agent = _make_agent(hitl_threshold="high")
        agent._ui_bridge.confirm_action = AsyncMock(return_value=True)

        result = _run(agent._execute_tool("write_file", {"path": "f.txt", "content": "x"}))

        agent._ui_bridge.confirm_action.assert_not_called()
        self.assertEqual(result, "tool_output")

    def test_medium_risk_asked_when_threshold_is_medium(self):
        """write_file is medium-risk — asked when threshold is 'medium'."""
        agent = _make_agent(hitl_threshold="medium")
        agent._ui_bridge.confirm_action = AsyncMock(return_value=True)

        _run(agent._execute_tool("write_file", {"path": "f.txt", "content": "x"}))

        agent._ui_bridge.confirm_action.assert_called_once()


# ---------------------------------------------------------------------------
# Guard conditions
# ---------------------------------------------------------------------------

class HitlApprovalGuardTests(unittest.TestCase):

    def test_gate_off_by_default(self):
        """No hitl_risk_threshold → confirm_action never called."""
        agent = _make_agent(hitl_threshold=None)
        agent._ui_bridge.confirm_action = AsyncMock(return_value=True)

        result = _run(agent._execute_tool("run_command", {"cmd": "ls"}))

        agent._ui_bridge.confirm_action.assert_not_called()
        self.assertEqual(result, "tool_output")

    def test_no_bridge_skips_gate(self):
        """Without a UIBridge the approval gate is silently skipped."""
        agent = _make_agent(hitl_threshold="high", has_bridge=False)

        result = _run(agent._execute_tool("run_command", {"cmd": "ls"}))

        self.assertEqual(result, "tool_output")

    def test_preview_mode_skips_gate(self):
        """In preview mode run() already confirms every action — no double-prompt."""
        agent = _make_agent(hitl_threshold="high", preview_mode=True)
        agent._ui_bridge.confirm_action = AsyncMock(return_value=True)

        _run(agent._execute_tool("run_command", {"cmd": "ls"}))

        agent._ui_bridge.confirm_action.assert_not_called()


# ---------------------------------------------------------------------------
# Coexistence with block_high_risk
# ---------------------------------------------------------------------------

class HitlApprovalBlockCoexistenceTests(unittest.TestCase):

    def test_interactive_approval_overrides_hard_block(self):
        """block_high_risk=True + hitl_risk_threshold + user approves → tool runs."""
        agent = _make_agent(hitl_threshold="high", block_high_risk=True)
        agent._ui_bridge.confirm_action = AsyncMock(return_value=True)

        result = _run(agent._execute_tool("run_command", {"cmd": "ls"}))

        agent._ui_bridge.confirm_action.assert_called_once()
        agent.session.call_tool.assert_called_once()
        self.assertEqual(result, "tool_output")

    def test_interactive_deny_still_blocks_when_hard_block_also_set(self):
        """block_high_risk=True + hitl_risk_threshold + user denies → HITL_DENIED."""
        agent = _make_agent(hitl_threshold="high", block_high_risk=True)
        agent._ui_bridge.confirm_action = AsyncMock(return_value=False)

        result = _run(agent._execute_tool("run_command", {"cmd": "ls"}))

        agent.session.call_tool.assert_not_called()
        self.assertIn("HITL_DENIED", result)

    def test_block_high_risk_without_bridge_still_hard_blocks(self):
        """block_high_risk=True with no bridge + no hitl_threshold → hard-block."""
        agent = _make_agent(hitl_threshold=None, block_high_risk=True, has_bridge=False)

        result = _run(agent._execute_tool("run_command", {"cmd": "ls"}))

        agent.session.call_tool.assert_not_called()
        self.assertIn("HITL_REQUIRED", result)


# ---------------------------------------------------------------------------
# JSON event emission
# ---------------------------------------------------------------------------

class HitlApprovalJsonEventTests(unittest.TestCase):

    def test_approval_request_and_result_events_emitted(self):
        """Verify structured JSON events are printed to stdout."""
        agent = _make_agent(hitl_threshold="high")
        agent._ui_bridge.confirm_action = AsyncMock(return_value=True)

        captured = io.StringIO()
        original_stdout = sys.stdout
        sys.stdout = captured
        try:
            _run(agent._execute_tool("run_command", {"cmd": "echo hi"}))
        finally:
            sys.stdout = original_stdout

        lines = [l for l in captured.getvalue().strip().split("\n") if l.strip()]
        events = []
        for line in lines:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass

        event_types = [e.get("type") for e in events]
        self.assertIn("hitl_approval_request", event_types)
        self.assertIn("hitl_approval_result", event_types)

    def test_approval_request_event_includes_tool_and_risk(self):
        agent = _make_agent(hitl_threshold="high")
        agent._ui_bridge.confirm_action = AsyncMock(return_value=False)

        captured = io.StringIO()
        original_stdout = sys.stdout
        sys.stdout = captured
        try:
            _run(agent._execute_tool("delete_file", {"path": "x.txt"}))
        finally:
            sys.stdout = original_stdout

        lines = [l for l in captured.getvalue().strip().split("\n") if l.strip()]
        request_events = []
        for line in lines:
            try:
                ev = json.loads(line)
                if ev.get("type") == "hitl_approval_request":
                    request_events.append(ev)
            except json.JSONDecodeError:
                pass

        self.assertEqual(len(request_events), 1)
        ev = request_events[0]
        self.assertEqual(ev["tool"], "delete_file")
        self.assertEqual(ev["risk"], "high")
        self.assertEqual(ev["reason"], "file_deletion")

    def test_no_events_emitted_when_gate_off(self):
        agent = _make_agent(hitl_threshold=None)

        captured = io.StringIO()
        original_stdout = sys.stdout
        sys.stdout = captured
        try:
            _run(agent._execute_tool("run_command", {"cmd": "echo hi"}))
        finally:
            sys.stdout = original_stdout

        lines = [l for l in captured.getvalue().strip().split("\n") if l.strip()]
        hitl_events = []
        for line in lines:
            try:
                ev = json.loads(line)
                if ev.get("type", "").startswith("hitl_"):
                    hitl_events.append(ev)
            except json.JSONDecodeError:
                pass

        self.assertEqual(hitl_events, [])


if __name__ == "__main__":
    unittest.main()

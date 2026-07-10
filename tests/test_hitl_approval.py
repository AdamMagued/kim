"""
Tests for the HITL approval threshold resolution and the agent-side approval
resolver.

NOTE (Phase 1 / K1): the interactive approval gate MOVED out of
KimAgent._execute_tool and into the MCP server (mcp_server/policy.py +
server.py:call_tool), brokered back to the agent's UIBridge. The
allow/deny/approve behavior is now covered end-to-end in
tests/test_policy_enforce.py and tests/test_policy_approval_flow.py. What
remains here:
  - _resolve_hitl_threshold() config/env resolution (unchanged)
  - the agent-side resolver forwards the server's request to the UIBridge and
    emits the hitl_approval_request/result events

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
    "mss", "pynput", "pynput.mouse", "pynput.keyboard",
    "pyautogui", "PIL", "PIL.Image", "sounddevice", "soundfile",
    "kokoro", "pygetwindow",
):
    if _mod not in sys.modules:
        sys.modules[_mod] = _stub(_mod)

# mcp: prefer the REAL package (it's installed in CI + dev). Only stub the
# client-facing symbols when the package is absent — stubbing it
# unconditionally would replace mcp.server for every later test in the
# session (e.g. the policy chokepoint tests that import mcp_server.server).
try:
    import mcp  # noqa: F401
    import mcp.client.stdio  # noqa: F401
except Exception:
    for _mod in ("mcp", "mcp.client", "mcp.client.stdio", "mcp.types"):
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
# Agent-side approval resolver (the K1 seam that answers the server's request)
# ---------------------------------------------------------------------------

class ApprovalResolverTests(unittest.TestCase):

    def _resolver(self, agent):
        return agent._make_approval_resolver()

    def test_forwards_decision_from_bridge(self):
        agent = _make_agent(hitl_threshold="high")
        agent._ui_bridge.decide_action = AsyncMock(return_value="accept")
        resolver = self._resolver(agent)
        decision = _run(resolver({
            "tool": "run_command", "risk": "high", "reason": "x",
            "preview": "echo hi", "args": {"cmd": "echo hi"},
        }))
        self.assertEqual(decision, "accept")
        args, kwargs = agent._ui_bridge.decide_action.call_args
        self.assertEqual(args, ("run_command", {"cmd": "echo hi"}))
        # T1: the resolver forwards the unique decision id it emitted.
        self.assertTrue(kwargs.get("request_id"))

    def test_decline_is_propagated(self):
        agent = _make_agent(hitl_threshold="high")
        agent._ui_bridge.decide_action = AsyncMock(return_value="decline")
        decision = _run(self._resolver(agent)({
            "tool": "delete_file", "risk": "high", "reason": "file_deletion",
            "preview": "", "args": {"path": "x"},
        }))
        self.assertEqual(decision, "decline")

    def test_accept_for_session_is_propagated(self):
        agent = _make_agent(hitl_threshold="high")
        agent._ui_bridge.decide_action = AsyncMock(return_value="acceptForSession")
        decision = _run(self._resolver(agent)({
            "tool": "run_command", "risk": "high", "reason": "x",
            "preview": "", "args": {},
        }))
        self.assertEqual(decision, "acceptForSession")

    def test_preview_mode_auto_accepts_without_asking(self):
        agent = _make_agent(hitl_threshold="high", preview_mode=True)
        agent._ui_bridge.decide_action = AsyncMock(return_value="decline")
        decision = _run(self._resolver(agent)({
            "tool": "run_command", "risk": "high", "reason": "x",
            "preview": "", "args": {},
        }))
        self.assertEqual(decision, "accept")
        agent._ui_bridge.decide_action.assert_not_called()

    def test_emits_request_and_result_events(self):
        agent = _make_agent(hitl_threshold="high")
        agent._ui_bridge.decide_action = AsyncMock(return_value="accept")

        captured = io.StringIO()
        original_stdout = sys.stdout
        sys.stdout = captured
        try:
            _run(self._resolver(agent)({
                "tool": "run_command", "risk": "high", "reason": "x",
                "preview": "echo hi", "args": {"cmd": "echo hi"},
            }))
        finally:
            sys.stdout = original_stdout

        events = []
        for line in captured.getvalue().strip().split("\n"):
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        types = [e.get("type") for e in events]
        self.assertIn("hitl_approval_request", types)
        self.assertIn("hitl_approval_result", types)
        result = [e for e in events if e["type"] == "hitl_approval_result"][0]
        self.assertTrue(result["approved"])


# ---------------------------------------------------------------------------
# _execute_tool's hard-block enforcement (finding 2)
# ---------------------------------------------------------------------------
#
# Previously, when the client-side InteractionPolicy hard-blocked a tool
# (hitl_block_high_risk / KIM_HITL_BLOCK_HIGH_RISK), _execute_tool fell
# through un-executed WITHOUT ever calling the UI bridge, on the assumption
# that the MCP server's own, separately-configured gate (a different config
# key: mcp_server hitl_risk_threshold) would independently also classify
# this tool+args as needing approval and pause on the ApprovalBroker. That
# assumption doesn't hold in general, so a "hard-blocked" tool could execute
# with a human never actually asked. These tests pin the fixed behavior:
# _execute_tool now always resolves the hard-block itself through the same
# resolver _make_approval_resolver() builds for server-originated requests.


class ExecuteToolHardBlockTests(unittest.TestCase):

    def test_hard_block_asks_the_ui_bridge_and_executes_on_accept(self):
        agent = _make_agent(block_high_risk=True)
        agent._ui_bridge.decide_action = AsyncMock(return_value="accept")
        result = _run(agent._execute_tool("run_command", {"cmd": "echo hi"}))
        self.assertEqual(result, "tool_output")
        agent._ui_bridge.decide_action.assert_called_once()
        call_args, _ = agent._ui_bridge.decide_action.call_args
        self.assertEqual(call_args[0], "run_command")
        agent.session.call_tool.assert_called_once()

    def test_hard_block_denies_execution_on_decline(self):
        agent = _make_agent(block_high_risk=True)
        agent._ui_bridge.decide_action = AsyncMock(return_value="decline")
        result = _run(agent._execute_tool("run_command", {"cmd": "echo hi"}))
        self.assertIn("HITL_REQUIRED", result)
        agent._ui_bridge.decide_action.assert_called_once()
        agent.session.call_tool.assert_not_called()

    def test_hard_block_accept_for_session_also_executes(self):
        agent = _make_agent(block_high_risk=True)
        agent._ui_bridge.decide_action = AsyncMock(return_value="acceptForSession")
        result = _run(agent._execute_tool("run_command", {"cmd": "echo hi"}))
        self.assertEqual(result, "tool_output")
        agent.session.call_tool.assert_called_once()

    def test_hard_block_without_ui_bridge_fails_closed(self):
        # No UI bridge means nothing can ask a human — the resolver declines
        # (bridge is None) and the tool must NOT execute.
        agent = _make_agent(block_high_risk=True, has_bridge=False)
        result = _run(agent._execute_tool("run_command", {"cmd": "echo hi"}))
        self.assertIn("HITL_REQUIRED", result)
        agent.session.call_tool.assert_not_called()

    def test_hard_block_fires_regardless_of_hitl_risk_threshold(self):
        # THE regression this finding is about: the old fallthrough also
        # required self._hitl_risk_threshold to be truthy — an unrelated,
        # separately-configured value (the MCP server's OWN gate) that has
        # nothing to do with whether this client-side hard-block should ask
        # for approval. With it unset (None), the tool must still go
        # through the approval gate — and be blocked on decline — instead
        # of silently executing.
        agent = _make_agent(block_high_risk=True, hitl_threshold=None)
        agent._ui_bridge.decide_action = AsyncMock(return_value="decline")
        result = _run(agent._execute_tool("run_command", {"cmd": "echo hi"}))
        self.assertIn("HITL_REQUIRED", result)
        agent._ui_bridge.decide_action.assert_called_once()
        agent.session.call_tool.assert_not_called()

    def test_hard_block_preview_mode_auto_accepts_without_double_prompt(self):
        # Preview mode's blanket confirm_action already asked about this
        # exact call before _execute_tool was reached; the shared resolver's
        # own preview auto-accept (same as the server-originated path) must
        # let it through without asking a second time.
        agent = _make_agent(block_high_risk=True, preview_mode=True)
        agent._ui_bridge.decide_action = AsyncMock(return_value="decline")
        result = _run(agent._execute_tool("run_command", {"cmd": "echo hi"}))
        self.assertEqual(result, "tool_output")
        agent._ui_bridge.decide_action.assert_not_called()
        agent.session.call_tool.assert_called_once()

    def test_non_hard_block_policy_denial_still_returns_message_unconditionally(self):
        # Non-HITL hard blocks (e.g. InteractionPolicy's stale/unknown
        # element_id POLICY_BLOCK for web_click) are NOT approval requests
        # and must keep being enforced unconditionally, with no detour
        # through the UI bridge — the "HITL_REQUIRED" in decision.message
        # check keeps this path (hard_block=True, but a different message)
        # from being routed through the new approval-resolver detour.
        agent = _make_agent(block_high_risk=False)
        agent._ui_bridge.decide_action = AsyncMock(return_value="accept")
        agent._interaction_policy.web_generation = 1  # simulate a prior web_observe
        result = _run(agent._execute_tool("web_click", {"element_id": "w99"}))
        self.assertIn("POLICY_BLOCK", result)
        agent._ui_bridge.decide_action.assert_not_called()
        agent.session.call_tool.assert_not_called()


if __name__ == "__main__":
    unittest.main()

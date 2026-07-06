"""
Behavioral tests for the K1 server-side approval round-trip.

Real components on a real loopback socket:

  mcp_server.server.call_tool
    → mcp_server.approvals.request_approval  (unix-socket client)
    → orchestrator.approval_broker.ApprovalBroker  (listener)
    → KimAgent._make_approval_resolver  (emits hitl events on stdout)
    → StdinApprovalBridge / StdinPump  (the SAME stdin line the Tauri GUI's
      hitl_respond_approval and the kim CLI's terminal prompt write)

Both caller vocabularies are exercised:
  GUI/CLI legacy:  {"type":"hitl_approve","approved":true|false}
  K1 generalized:  {"type":"hitl_approve","decision":"accept"|"acceptForSession"|"decline"}
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
from unittest.mock import MagicMock

import pytest

from mcp_server import approvals, policy
from mcp_server import server as srv
import orchestrator.ui_bridge as ui_bridge_mod
from orchestrator.approval_broker import ApprovalBroker
from orchestrator.ui_bridge import StdinApprovalBridge, StdinPump, normalize_decision


@pytest.fixture(autouse=True)
def _fresh_session_cache():
    approvals.reset_session_approvals()
    yield
    approvals.reset_session_approvals()


@pytest.fixture
def gated_env(monkeypatch):
    """Threshold high so run_command 'mkdir …' requires approval."""
    monkeypatch.setenv("KIM_HITL_RISK_THRESHOLD", "high")
    monkeypatch.delenv("KIM_APPROVAL_SOCK", raising=False)
    monkeypatch.delenv("KIM_APPROVAL_TCP", raising=False)


def _recording_handler(record: list):
    async def _h(args):
        record.append(args)
        return "executed"
    return _h


def _make_agent_with_stdin_bridge(monkeypatch):
    """Minimal KimAgent carrying a real StdinApprovalBridge + fake pump."""
    from orchestrator.agent import KimAgent

    agent = KimAgent.__new__(KimAgent)
    agent._ui_bridge = StdinApprovalBridge()
    agent.config = {}
    agent._log = lambda level, msg: None

    pump = StdinPump()
    pump._started = True
    pump._loop = None  # dispatch synchronously into the queue
    monkeypatch.setattr(ui_bridge_mod, "_STDIN_PUMP", pump)
    return agent, pump


async def _drive_call(monkeypatch, stdin_lines: list[dict], cmd="mkdir approved_dir"):
    """Full-stack call: broker + resolver + server.call_tool, answering each
    approval request by injecting `stdin_lines` through the REAL stdin pump
    routing (StdinPump._dispatch), exactly as Rust writes them."""
    agent, pump = _make_agent_with_stdin_bridge(monkeypatch)
    pump._loop = asyncio.get_running_loop()

    broker = ApprovalBroker()
    env = await broker.start()
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    broker.set_resolver(agent._make_approval_resolver())

    executed: list = []
    monkeypatch.setitem(srv._DISPATCH, "run_command", _recording_handler(executed))

    async def _answer():
        # Give the round-trip a beat to reach the pump wait, then answer.
        for line in stdin_lines:
            await asyncio.sleep(0.05)
            pump._dispatch({"type": "hitl_approve", **line})

    answer_task = asyncio.create_task(_answer())
    captured = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = captured
    try:
        result = await srv.call_tool("run_command", {"cmd": cmd})
    finally:
        sys.stdout = real_stdout
        await answer_task
        await broker.stop()
    return result[0].text, executed, captured.getvalue()


# ---------------------------------------------------------------------------
# GUI vocabulary: {"approved": bool}  (what subprocess.rs writes)
# ---------------------------------------------------------------------------

class TestGuiCallerRoundTrip:
    def test_gui_approve_executes_tool(self, monkeypatch, gated_env):
        text, executed, stdout = asyncio.run(
            _drive_call(monkeypatch, [{"approved": True}])
        )
        assert text == "executed"
        assert executed == [{"cmd": "mkdir approved_dir"}]
        # the approval request/result events reached stdout for the GUI card
        events = [json.loads(l) for l in stdout.splitlines() if l.strip()]
        types = [e["type"] for e in events]
        assert "hitl_approval_request" in types
        assert "hitl_approval_result" in types

    def test_gui_deny_blocks_tool(self, monkeypatch, gated_env):
        text, executed, _ = asyncio.run(
            _drive_call(monkeypatch, [{"approved": False}])
        )
        assert "HITL_DENIED" in text
        assert executed == []


# ---------------------------------------------------------------------------
# CLI / generalized vocabulary: {"decision": …}  (K1 stdin line)
# ---------------------------------------------------------------------------

class TestCliCallerRoundTrip:
    def test_decision_accept_executes(self, monkeypatch, gated_env):
        text, executed, _ = asyncio.run(
            _drive_call(monkeypatch, [{"approved": True, "decision": "accept"}])
        )
        assert text == "executed"
        assert len(executed) == 1

    def test_decision_decline_blocks(self, monkeypatch, gated_env):
        text, executed, _ = asyncio.run(
            _drive_call(monkeypatch, [{"approved": True, "decision": "decline"}])
        )
        # decision wins over the legacy bool
        assert "HITL_DENIED" in text
        assert executed == []

    def test_accept_for_session_caches_the_signature(self, monkeypatch, gated_env):
        async def _two_calls():
            agent, pump = _make_agent_with_stdin_bridge(monkeypatch)
            pump._loop = asyncio.get_running_loop()
            broker = ApprovalBroker()
            env = await broker.start()
            for k, v in env.items():
                monkeypatch.setenv(k, v)

            asked = []

            async def resolver(request):
                asked.append(request["tool"])
                return "acceptForSession"

            broker.set_resolver(resolver)
            executed: list = []
            monkeypatch.setitem(
                srv._DISPATCH, "run_command", _recording_handler(executed)
            )
            r1 = await srv.call_tool("run_command", {"cmd": "mkdir cached_dir"})
            r2 = await srv.call_tool("run_command", {"cmd": "mkdir cached_dir"})
            await broker.stop()
            return asked, executed, r1[0].text, r2[0].text

        asked, executed, t1, t2 = asyncio.run(_two_calls())
        assert asked == ["run_command"], "second identical call must not re-prompt"
        assert len(executed) == 2
        assert t1 == t2 == "executed"


# ---------------------------------------------------------------------------
# Fail-closed behavior
# ---------------------------------------------------------------------------

class TestFailClosed:
    def test_timeout_default_denies(self, monkeypatch, gated_env):
        async def _hang():
            broker = ApprovalBroker()
            env = await broker.start()
            for k, v in env.items():
                monkeypatch.setenv(k, v)

            async def never_resolves(request):
                await asyncio.sleep(30)
                return "accept"

            broker.set_resolver(never_resolves)
            monkeypatch.setattr(approvals, "_APPROVAL_TIMEOUT_S", 0.3)
            executed: list = []
            monkeypatch.setitem(
                srv._DISPATCH, "run_command", _recording_handler(executed)
            )
            result = await srv.call_tool("run_command", {"cmd": "mkdir slow_dir"})
            await broker.stop()
            return result[0].text, executed

        text, executed = asyncio.run(_hang())
        assert "HITL_DENIED" in text
        assert executed == []

    def test_no_channel_configured_denies(self, monkeypatch, gated_env):
        async def _call():
            executed: list = []
            monkeypatch.setitem(
                srv._DISPATCH, "run_command", _recording_handler(executed)
            )
            result = await srv.call_tool("run_command", {"cmd": "mkdir nochan_dir"})
            return result[0].text, executed

        text, executed = asyncio.run(_call())
        assert "HITL_DENIED" in text
        assert executed == []

    def test_no_resolver_registered_denies(self, monkeypatch, gated_env):
        async def _call():
            broker = ApprovalBroker()
            env = await broker.start()
            for k, v in env.items():
                monkeypatch.setenv(k, v)
            broker.set_resolver(None)
            executed: list = []
            monkeypatch.setitem(
                srv._DISPATCH, "run_command", _recording_handler(executed)
            )
            result = await srv.call_tool("run_command", {"cmd": "mkdir nores_dir"})
            await broker.stop()
            return result[0].text, executed

        text, executed = asyncio.run(_call())
        assert "HITL_DENIED" in text
        assert executed == []


# ---------------------------------------------------------------------------
# Vocabulary normalization (unit level, both directions)
# ---------------------------------------------------------------------------

class TestDecisionNormalization:
    @pytest.mark.parametrize("payload,expected", [
        ({"approved": True}, "accept"),
        ({"approved": False}, "decline"),
        ({"decision": "accept"}, "accept"),
        ({"decision": "acceptForSession"}, "acceptForSession"),
        ({"decision": "accept_for_session"}, "acceptForSession"),
        ({"decision": "decline", "approved": True}, "decline"),
        ({"decision": "garbage", "approved": True}, "accept"),
        ({}, "decline"),
    ])
    def test_stdin_line_normalization(self, payload, expected):
        assert normalize_decision(payload) == expected

    def test_server_side_normalization_matches(self):
        for payload, expected in [
            ({"approved": True}, "accept"),
            ({"decision": "acceptForSession"}, "acceptForSession"),
            ({}, "decline"),
        ]:
            assert approvals._normalize_decision(payload) == expected


# ---------------------------------------------------------------------------
# Resolver behavior (agent side)
# ---------------------------------------------------------------------------

class TestResolver:
    def _agent(self, bridge):
        from orchestrator.agent import KimAgent
        agent = KimAgent.__new__(KimAgent)
        agent._ui_bridge = bridge
        agent.config = {}
        return agent

    def test_preview_mode_auto_accepts(self):
        """Preview mode already confirms per action in run() — no double-prompt."""
        bridge = StdinApprovalBridge()
        bridge.preview_mode = True
        agent = self._agent(bridge)
        resolver = agent._make_approval_resolver()
        decision = asyncio.run(resolver({"tool": "run_command", "args": {}}))
        assert decision == "accept"

    def test_no_bridge_declines(self):
        agent = self._agent(None)
        resolver = agent._make_approval_resolver()
        assert asyncio.run(resolver({"tool": "x", "args": {}})) == "decline"

    def test_bridge_decision_is_forwarded_and_events_emitted(self):
        bridge = MagicMock()
        bridge.preview_mode = False

        async def decide(tool, args):
            return "acceptForSession"

        bridge.decide_action = decide
        agent = self._agent(bridge)
        resolver = agent._make_approval_resolver()

        captured = io.StringIO()
        real = sys.stdout
        sys.stdout = captured
        try:
            decision = asyncio.run(resolver({
                "tool": "run_command", "risk": "high",
                "reason": "r", "preview": "p", "args": {"cmd": "x"},
            }))
        finally:
            sys.stdout = real
        assert decision == "acceptForSession"
        events = [json.loads(l) for l in captured.getvalue().splitlines() if l.strip()]
        result_events = [e for e in events if e["type"] == "hitl_approval_result"]
        assert result_events and result_events[0]["approved"] is True

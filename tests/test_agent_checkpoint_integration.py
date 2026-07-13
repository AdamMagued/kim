"""
Tests for the run_checkpoint integration in KimAgent.run.

These tests verify that:
1. append_checkpoint is called once per iteration with the expected fields.
2. last_tool_name advances correctly after a tool completes.
3. A checkpoint write failure does NOT abort the agent run.

No real providers, MCP connections, or network access are used.  The agent is
driven via a fake provider that emits canned responses, and SessionStore is
replaced with a lightweight spy.

Heavy optional modules (mcp, yaml, dotenv, mss, pynput, …) are stubbed into
sys.modules before the import so this test file works without the runtime
environment.
"""
from __future__ import annotations

import asyncio
import sys
import types
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Stub out heavy runtime modules that aren't installed in the test venv
# ---------------------------------------------------------------------------

def _stub(name: str, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


for _mod in ("mss", "pynput", "pynput.mouse", "pynput.keyboard",
             "pyautogui", "PIL", "PIL.Image", "sounddevice", "soundfile",
             "kokoro", "pygetwindow"):
    if _mod not in sys.modules:
        sys.modules[_mod] = _stub(_mod)

# mcp: prefer the REAL package (installed in CI + dev). Only stub the
# client symbols when it is genuinely absent — stubbing unconditionally would
# replace mcp.server for later tests that import mcp_server.server.
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

# yaml must be real (it is installed), but stub in case it isn't
try:
    import yaml  # noqa: F401
except ImportError:
    sys.modules["yaml"] = _stub("yaml", safe_load=lambda *a, **k: {})

# dotenv
try:
    from dotenv import load_dotenv  # noqa: F401
except ImportError:
    sys.modules["dotenv"] = _stub("dotenv", load_dotenv=lambda *a, **k: None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _SpySessionStore:
    """Minimal SessionStore stand-in that records append_checkpoint calls."""

    def __init__(self, base_dir: Path, session_id: str = "spy_session") -> None:
        self.session_id = session_id
        self.base_dir = base_dir
        self.session_file = base_dir / f"{session_id}.jsonl"
        self.checkpoints: List[Dict[str, Any]] = []
        self.messages: List[dict] = []
        self.context_state: Dict[str, Any] = {}
        self._checkpoint_raise = False

    def append_checkpoint(
        self,
        iteration: int,
        phase: str,
        last_tool_name: Optional[str] = None,
        result_type: Optional[str] = None,
        consecutive_continues: Optional[int] = None,
    ) -> None:
        if self._checkpoint_raise:
            raise OSError("simulated disk full")
        record: dict = {"iteration": iteration, "phase": phase}
        if last_tool_name is not None:
            record["last_tool_name"] = last_tool_name
        if result_type is not None:
            record["result_type"] = result_type
        if consecutive_continues is not None:
            record["consecutive_continues"] = consecutive_continues
        self.checkpoints.append(record)

    def append_message(self, msg: dict) -> None:
        self.messages.append(msg)

    def append_run_started(self, task: str, cwd: str = None) -> None:
        pass

    def append_run_result(self, result: dict, cwd: str = None) -> None:
        pass

    def append_tool_event(self, *args, **kwargs) -> None:
        pass

    def append_llm_event(self, *args, **kwargs) -> None:
        pass

    def load_context_state(self) -> Optional[dict]:
        return dict(self.context_state)

    def save_context_state(self, state: dict) -> None:
        self.context_state = dict(state)


def _make_task_complete_response(summary: str = "done") -> dict:
    return {"type": "tool_call", "tool": "task_complete", "args": {"summary": summary}}


def _make_tool_response(tool: str, args: dict) -> dict:
    return {"type": "tool_call", "tool": tool, "args": args}


def _run(coro):
    # asyncio.run, not get_event_loop().run_until_complete — the latter depends
    # on a leftover loop and breaks when any earlier test used asyncio.run.
    return asyncio.run(coro)


def _build_agent(spy_store: _SpySessionStore, provider_responses: list):
    """Build a KimAgent wired to a fake provider sequence and the spy store."""
    from orchestrator.agent import KimAgent
    from orchestrator.memory import ConversationMemory

    call_iter = iter(provider_responses)

    async def fake_complete(messages, tools, system, **kwargs):
        return next(call_iter)

    agent = KimAgent.__new__(KimAgent)

    config = {
        "max_iterations": 10,
        "screenshot_scale": 0.75,
        "memory_max_messages": 40,
        "memory_keep_screenshots": 4,
        "context_budget_tokens": 100_000,
    }
    # Public attrs used by run()
    agent.config = config
    agent.provider = MagicMock(spec=[])  # no reset_session
    agent.session = MagicMock()
    agent.max_iterations = int(config["max_iterations"])

    # Private attrs
    agent._session_store = spy_store
    agent._ui_bridge = None
    agent._tools = [{"name": "run_command"}, {"name": "task_complete"}]
    agent._screenshot_hashes = []
    agent._total_tokens = {"input": 0, "output": 0}
    agent._context_meter = MagicMock()
    agent._context_meter.snapshot = MagicMock(return_value=MagicMock(
        pct=0.1, used=1000, budget=100_000, tokens_left=99_000, source="test",
    ))
    agent._context_meter.observe_usage = MagicMock(return_value=MagicMock(pct=0.1))
    agent._interaction_policy = MagicMock()
    agent._interaction_policy.before_tool = MagicMock(return_value=MagicMock(
        blocked=False, reason=None, risk_level="low"
    ))
    agent._interaction_policy.after_tool = MagicMock()
    agent._resume_session_id = None
    agent._clear_chat_on_next_call = False
    agent._last_plan_signature = ""
    agent._last_step_signature = ""
    agent._last_done_signature = ""
    agent._current_plan_steps = []
    agent._current_step_index = 0

    agent._log = lambda level, msg: None
    agent._is_cancelled = lambda: False
    agent._is_stuck = lambda _: False
    agent._emit_plan_markers = lambda _: None
    agent._complete_run = lambda result: result
    agent._emit_context_snapshot = lambda: None
    agent._build_system_prompt = lambda task: "system"
    agent._persist_context_state_extra = lambda *a, **kw: None

    agent.memory = ConversationMemory(
        max_messages=config["memory_max_messages"],
        keep_screenshots=config["memory_keep_screenshots"],
    )

    async def _refresh_tools():
        pass  # tools already set above

    agent._refresh_tools = _refresh_tools

    async def _call_with_retry(messages, tools, system, clear_chat=False, **kwargs):
        return await fake_complete(messages, tools, system)

    agent._call_with_retry = _call_with_retry

    async def _execute_tool(name, args):
        return f"OK:{name}"

    agent._execute_tool = _execute_tool
    agent._check_context_pressure = AsyncMock(return_value=None)
    agent._track_context_usage = lambda *a, **k: None

    return agent


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_checkpoint_called_once_per_iteration_on_immediate_complete():
    """One iteration → one checkpoint at iteration=1, phase=iteration_start."""
    with tempfile.TemporaryDirectory() as tmp:
        spy = _SpySessionStore(Path(tmp))
        agent = _build_agent(spy, [_make_task_complete_response()])

        with patch("orchestrator.agent.estimate_request_tokens", return_value=100), \
             patch("orchestrator.agent.SessionStore.session_exists", return_value=False):
            _run(agent.run("do the thing"))

    assert len(spy.checkpoints) == 1
    cp = spy.checkpoints[0]
    assert cp["iteration"] == 1
    assert cp["phase"] == "iteration_start"
    assert "last_tool_name" not in cp  # no prior tool on first iteration


def test_checkpoint_last_tool_name_advances_after_tool_call():
    """After a tool executes in iteration N, last_tool_name appears in iteration N+1."""
    with tempfile.TemporaryDirectory() as tmp:
        spy = _SpySessionStore(Path(tmp))
        agent = _build_agent(spy, [
            _make_tool_response("run_command", {"cmd": "ls"}),
            _make_task_complete_response(),
        ])

        with patch("orchestrator.agent.estimate_request_tokens", return_value=100), \
             patch("orchestrator.agent.SessionStore.session_exists", return_value=False):
            _run(agent.run("do the thing"))

    assert len(spy.checkpoints) == 2
    assert "last_tool_name" not in spy.checkpoints[0]
    assert spy.checkpoints[1]["last_tool_name"] == "run_command"


def test_checkpoint_write_failure_does_not_abort_run():
    """A failing append_checkpoint must not propagate — the run must still complete."""
    with tempfile.TemporaryDirectory() as tmp:
        spy = _SpySessionStore(Path(tmp))
        spy._checkpoint_raise = True
        agent = _build_agent(spy, [_make_task_complete_response()])

        with patch("orchestrator.agent.estimate_request_tokens", return_value=100), \
             patch("orchestrator.agent.SessionStore.session_exists", return_value=False):
            result = _run(agent.run("do the thing"))

    # Run must complete (task_complete), not raise.
    assert result is not None


def test_checkpoint_consecutive_continues_included():
    """consecutive_continues is passed to append_checkpoint on every call."""
    with tempfile.TemporaryDirectory() as tmp:
        spy = _SpySessionStore(Path(tmp))
        agent = _build_agent(spy, [_make_task_complete_response()])

        with patch("orchestrator.agent.estimate_request_tokens", return_value=100), \
             patch("orchestrator.agent.SessionStore.session_exists", return_value=False):
            _run(agent.run("do the thing"))

    # consecutive_continues starts at 0; checkpoint must record it.
    assert spy.checkpoints[0]["consecutive_continues"] == 0


def test_track_context_usage_persists_real_snapshot_metadata():
    with tempfile.TemporaryDirectory() as tmp:
        spy = _SpySessionStore(Path(tmp))
        agent = _build_agent(spy, [])

        from orchestrator.agent import KimAgent
        from orchestrator.context_meter import ContextMeter

        agent._context_meter = ContextMeter(budget=1_000)
        agent._persist_context_state_extra = KimAgent._persist_context_state_extra.__get__(agent, KimAgent)
        agent._track_context_usage = KimAgent._track_context_usage.__get__(agent, KimAgent)
        agent._print_context_json = lambda snapshot: None

        agent._track_context_usage(
            {"input": 120, "output": 33, "source": "browser_prompt", "estimated": True}
        )

        assert spy.context_state["last_input"] == 120
        assert spy.context_state["last_output"] == 33
        assert spy.context_state["source"] == "browser_prompt"
        assert spy.context_state["estimated"] is True

        agent._persist_context_state_extra({"needs_fresh_chat": False})

        assert spy.context_state["last_input"] == 120
        assert spy.context_state["last_output"] == 33
        assert spy.context_state["source"] == "browser_prompt"
        assert spy.context_state["needs_fresh_chat"] is False


def test_resume_log_counts_only_real_conversation_messages():
    with tempfile.TemporaryDirectory() as tmp:
        spy = _SpySessionStore(Path(tmp))
        agent = _build_agent(spy, [_make_task_complete_response()])
        logs: list[str] = []
        agent._log = lambda _level, msg: logs.append(msg)
        agent._resume_session_id = "resume-1"

        saved = [
            {"type": "run_started", "task": "hello"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"type": "run_result", "termination": "task_complete"},
        ]

        with patch("orchestrator.agent.estimate_request_tokens", return_value=100), \
             patch("orchestrator.agent.SessionStore.session_exists", return_value=True), \
             patch("orchestrator.agent.SessionStore.load_session", return_value=saved):
            _run(agent.run("continue"))

    assert any("Resuming session resume-1 (2 messages)" in msg for msg in logs)

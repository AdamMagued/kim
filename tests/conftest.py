"""
Shared pytest fixtures and test factories.

make_test_agent(**overrides)
    Build a KimAgent with all required attributes pre-wired so tests don't
    need to reverse-engineer __init__'s private internals. Accepts keyword
    overrides for any attribute.

    Example:
        agent = make_test_agent(max_iterations=3)
        agent = make_test_agent(provider=my_fake_provider)
        agent = make_test_agent(config={"hitl_risk_threshold": "high"})
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Path fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def project_root():
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def mock_config():
    return {
        "max_iterations": 25,
        "screenshot_scale": 0.75,
        "memory_max_messages": 40,
        "memory_keep_screenshots": 4,
        "context_budget_tokens": 100_000,
    }


# ---------------------------------------------------------------------------
# make_test_agent — shared factory
# ---------------------------------------------------------------------------

def _ensure_stubs():
    """Ensure heavy runtime modules are stubbed if not already available."""
    def _stub(name: str, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        return m

    for mod in (
        "mss", "pynput", "pynput.mouse", "pynput.keyboard",
        "pyautogui", "PIL", "PIL.Image", "sounddevice", "soundfile",
        "kokoro", "pygetwindow",
    ):
        if mod not in sys.modules:
            sys.modules[mod] = _stub(mod)

    # mcp: use real package if installed, otherwise stub minimally
    try:
        import mcp  # noqa: F401
    except ImportError:
        for mod in ("mcp", "mcp.client", "mcp.client.stdio"):
            if mod not in sys.modules:
                sys.modules[mod] = _stub(mod)
        mcp_mod = sys.modules["mcp"]
        mcp_mod.ClientSession = MagicMock
        mcp_mod.StdioServerParameters = MagicMock
        sys.modules["mcp.client.stdio"].stdio_client = MagicMock()


def make_test_agent(**overrides):
    """
    Build a KimAgent with all required attributes pre-wired.

    Accepts keyword overrides for any attribute or group:
      - provider:       BaseProvider instance (default: FakeProvider)
      - config:         dict (default: standard test config)
      - session_store:  SessionStore or mock (default: MagicMock)
      - ui_bridge:      UIBridge or None (default: None)
      - max_iterations: int (default: 10)

    Any other kwarg is set directly on the agent object.
    """
    _ensure_stubs()

    from orchestrator.providers.fake import FakeProvider
    from orchestrator.memory import ConversationMemory
    from orchestrator.interaction_policy import InteractionPolicy
    from orchestrator.agent import KimAgent

    config = overrides.pop("config", {
        "max_iterations": 10,
        "screenshot_scale": 0.75,
        "memory_max_messages": 40,
        "memory_keep_screenshots": 4,
        "context_budget_tokens": 100_000,
    })
    provider = overrides.pop("provider", FakeProvider())
    session_store = overrides.pop("session_store", MagicMock())
    ui_bridge = overrides.pop("ui_bridge", None)

    agent = object.__new__(KimAgent)

    # Required public attrs
    agent.config = config
    agent.provider = provider
    agent.session = MagicMock()
    agent.max_iterations = int(config.get("max_iterations", 10))
    agent.screenshot_scale = float(config.get("screenshot_scale", 0.75))
    agent.memory = ConversationMemory(
        max_messages=int(config.get("memory_max_messages", 40)),
        keep_screenshots=int(config.get("memory_keep_screenshots", 4)),
    )

    # Required private attrs
    agent._ui_bridge = ui_bridge
    agent._session_store = session_store
    agent._resume_session_id = None
    agent._screenshot_hashes = []
    agent._recent_action_sigs = []
    agent._tools = [
        {"name": "take_screenshot", "description": "Take screenshot", "parameters": {}},
        {"name": "run_command", "description": "Run shell command", "parameters": {}},
    ]
    agent._total_tokens = {"input": 0, "output": 0}
    agent._last_plan_signature = ""
    agent._last_step_signature = ""
    agent._last_done_signature = ""
    agent._current_plan_steps = []
    agent._current_step_index = 0
    agent._clear_chat_on_next_call = False
    agent._max_retries = int(config.get("max_retries", 5))
    agent._retry_base_delay = float(config.get("retry_base_delay", 1.0))
    agent._retry_max_delay = float(config.get("retry_max_delay", 60.0))
    agent._hitl_risk_threshold = None
    agent._interaction_policy = InteractionPolicy(block_high_risk=False)

    # Context meter mock (override with a real ContextMeter if needed)
    agent._context_meter = MagicMock()
    agent._context_meter.snapshot = MagicMock(return_value=MagicMock(
        pct=0.1, used=1000, budget=100_000, tokens_left=99_000, source="test",
    ))
    agent._context_meter.observe_usage = MagicMock(return_value=MagicMock(pct=0.1))

    # Apply any remaining overrides as direct attribute sets
    for key, value in overrides.items():
        setattr(agent, key, value)

    return agent


@pytest.fixture
def test_agent():
    """Pytest fixture form of make_test_agent with default settings."""
    return make_test_agent()

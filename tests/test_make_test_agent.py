"""
Tests for the make_test_agent factory in conftest.py.

Validates that the factory:
- Builds a valid KimAgent with all required attributes
- Accepts per-attribute overrides
- Produces agents that can actually be used in test scenarios
"""
from __future__ import annotations

import asyncio
import pytest
from conftest import make_test_agent


def test_make_test_agent_returns_kim_agent():
    from orchestrator.agent import KimAgent
    agent = make_test_agent()
    assert isinstance(agent, KimAgent)


def test_make_test_agent_default_attributes():
    agent = make_test_agent()
    assert agent.max_iterations == 10
    assert agent.config["context_budget_tokens"] == 100_000
    assert agent._tools is not None
    assert len(agent._tools) >= 1
    assert agent._ui_bridge is None
    assert agent._voice is None


def test_make_test_agent_custom_max_iterations():
    agent = make_test_agent(max_iterations=3)
    assert agent.max_iterations == 3


def test_make_test_agent_custom_provider():
    from orchestrator.providers.fake import FakeProvider
    my_provider = FakeProvider(responses=[
        {"type": "text", "content": "TASK_COMPLETE: custom"},
    ])
    agent = make_test_agent(provider=my_provider)
    result = asyncio.run(agent.provider.complete([], [], ""))
    assert "custom" in result["content"]


def test_make_test_agent_custom_config():
    agent = make_test_agent(config={
        "max_iterations": 5,
        "context_budget_tokens": 50_000,
    })
    assert agent.max_iterations == 5
    assert agent.config["context_budget_tokens"] == 50_000


def test_make_test_agent_arbitrary_override():
    agent = make_test_agent(_hitl_risk_threshold="high")
    assert agent._hitl_risk_threshold == "high"


def test_test_agent_fixture(test_agent):
    from orchestrator.agent import KimAgent
    assert isinstance(test_agent, KimAgent)
    assert test_agent.max_iterations == 10


def test_make_test_agent_has_all_attributes_used_by_run():
    """Verify every attribute accessed in KimAgent.run() is present."""
    agent = make_test_agent()
    # These attributes are accessed by the run() loop:
    assert hasattr(agent, "config")
    assert hasattr(agent, "provider")
    assert hasattr(agent, "session")
    assert hasattr(agent, "max_iterations")
    assert hasattr(agent, "memory")
    assert hasattr(agent, "_tools")
    assert hasattr(agent, "_ui_bridge")
    assert hasattr(agent, "_session_store")
    assert hasattr(agent, "_resume_session_id")
    assert hasattr(agent, "_screenshot_hashes")
    assert hasattr(agent, "_context_meter")
    assert hasattr(agent, "_interaction_policy")
    assert hasattr(agent, "_total_tokens")

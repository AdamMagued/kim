"""K3: mid-run steering — a steer line lands in the next request payload."""

import asyncio

from orchestrator.ui_bridge import StdinPump


def _make_agent():
    from unittest.mock import MagicMock
    from orchestrator.agent import KimAgent
    from orchestrator.providers.fake import FakeProvider

    config = {
        "max_iterations": 5,
        "screenshot_scale": 0.75,
        "memory_max_messages": 40,
        "memory_keep_screenshots": 4,
        "context_budget_tokens": 100_000,
    }
    return KimAgent(config=config, session=MagicMock(), provider=FakeProvider())


def test_steer_lands_in_next_request_payload():
    agent = _make_agent()
    agent.add_steer("actually, focus on the login page")
    agent._drain_steers()
    messages = agent.memory.get_messages()
    blob = str(messages)
    assert "actually, focus on the login page" in blob
    assert any("steering" in str(m).lower() for m in messages)


def test_drain_is_idempotent_and_clears_inbox():
    agent = _make_agent()
    agent.add_steer("one")
    agent._drain_steers()
    n_after_first = len(agent.memory.get_messages())
    agent._drain_steers()  # nothing queued — no new messages
    assert len(agent.memory.get_messages()) == n_after_first


def test_pump_dispatch_routes_steer_and_approval():
    pump = StdinPump()
    seen = []
    pump.set_steer_callback(seen.append)
    # No running loop set → dispatch routes synchronously.
    pump._dispatch({"type": "user_steer", "text": "go left"})
    assert seen == ["go left"]
    pump._dispatch({"type": "hitl_approve", "approved": True})
    got = asyncio.run(pump.next_approval(timeout=1.0))
    assert got["approved"] is True

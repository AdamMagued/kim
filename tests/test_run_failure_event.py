"""Tests for run_failure_event() in orchestrator/agent_states.py (P1-2)."""
from __future__ import annotations

import pytest

from orchestrator.agent_states import AgentTermination, run_failure_event


class TestRunFailureEvent:

    def test_returns_none_for_task_complete(self):
        assert run_failure_event(AgentTermination.TASK_COMPLETE, "done") is None

    def test_returns_none_for_cancelled(self):
        assert run_failure_event(AgentTermination.CANCELLED, "cancelled") is None

    def test_returns_dict_for_provider_failed(self):
        event = run_failure_event(AgentTermination.PROVIDER_FAILED, "LLM failed")
        assert event is not None
        assert event["type"] == "run_failed"
        assert event["reason"] == "provider_failed"
        assert isinstance(event["recoverable"], bool)
        assert isinstance(event["suggestion"], str)
        assert len(event["suggestion"]) > 0

    def test_auth_error_code_marks_not_recoverable(self):
        event = run_failure_event(
            AgentTermination.PROVIDER_FAILED, "API key invalid", provider_error_code="auth"
        )
        assert event is not None
        assert event["recoverable"] is False
        assert "Settings" in event["suggestion"] or "Re-authenticate" in event["suggestion"]

    def test_rate_limit_code_marks_recoverable(self):
        event = run_failure_event(
            AgentTermination.PROVIDER_FAILED, "Too many requests", provider_error_code="rate_limit"
        )
        assert event is not None
        assert event["recoverable"] is True

    def test_cdp_browser_error_in_summary(self):
        event = run_failure_event(
            AgentTermination.PROVIDER_FAILED,
            "CDP connection refused",
        )
        assert event is not None
        assert event["recoverable"] is True
        assert "--remote-debugging-port" in event["suggestion"].lower() or "Chrome" in event["suggestion"]

    def test_stuck_is_recoverable(self):
        event = run_failure_event(AgentTermination.STUCK, "screen unchanged")
        assert event is not None
        assert event["reason"] == "stuck"
        assert event["recoverable"] is True

    def test_max_iterations_is_recoverable(self):
        event = run_failure_event(AgentTermination.MAX_ITERATIONS, "limit reached")
        assert event is not None
        assert event["reason"] == "max_iterations"
        assert event["recoverable"] is True

    def test_need_help_event(self):
        event = run_failure_event(AgentTermination.NEED_HELP, "clarification needed")
        assert event is not None
        assert event["reason"] == "need_help"

    def test_conversational_loop_event(self):
        event = run_failure_event(AgentTermination.CONVERSATIONAL_LOOP, "stuck in loop")
        assert event is not None
        assert event["reason"] == "conversational_loop"

    def test_event_keys_present(self):
        event = run_failure_event(AgentTermination.STUCK, "stuck")
        assert event is not None
        assert set(event.keys()) == {"type", "reason", "recoverable", "suggestion"}

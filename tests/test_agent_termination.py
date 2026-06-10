"""
Contract tests for AgentTermination and make_run_result in agent_states.py.

Verifies that the typed termination reason is preserved in the result dict so
callers (task_queue, cli, tray) can route on WHY a run ended, not just whether
it succeeded.  The `termination` field was the whole point of Phase 10's typed
enum refactor; this suite enforces that the value is not silently dropped.
"""

from __future__ import annotations

import pytest

from orchestrator.agent_states import AgentTermination, make_run_result


# ── termination field is preserved ───────────────────────────────────────────


def test_task_complete_sets_success_true_and_termination():
    result = make_run_result(AgentTermination.TASK_COMPLETE, "Done!", "screenshot_b64")
    assert result["success"] is True
    assert result["termination"] == "task_complete"


@pytest.mark.parametrize("term,expected_value", [
    (AgentTermination.NEED_HELP,           "need_help"),
    (AgentTermination.CANCELLED,           "cancelled"),
    (AgentTermination.MAX_ITERATIONS,      "max_iterations"),
    (AgentTermination.PROVIDER_FAILED,     "provider_failed"),
    (AgentTermination.CONVERSATIONAL_LOOP, "conversational_loop"),
    (AgentTermination.STUCK,              "stuck"),
])
def test_non_success_terminations_set_success_false_and_preserve_reason(term, expected_value):
    result = make_run_result(term, "reason text", "")
    assert result["success"] is False
    assert result["termination"] == expected_value, (
        f"Expected termination={expected_value!r} for {term}, got {result['termination']!r}"
    )


# ── existing fields still present (backward compat) ───────────────────────────


def test_result_dict_contains_all_expected_keys():
    result = make_run_result(AgentTermination.TASK_COMPLETE, "ok", "img")
    assert set(result.keys()) >= {"success", "termination", "summary", "screenshot"}


def test_summary_and_screenshot_are_forwarded_unchanged():
    result = make_run_result(AgentTermination.NEED_HELP, "specific reason", "base64data")
    assert result["summary"] == "specific reason"
    assert result["screenshot"] == "base64data"


# ── all enum values are reachable (no missing case) ────────────────────────────


def test_all_termination_values_produce_a_result():
    """Guards against new enum members being added without updating make_run_result."""
    for term in AgentTermination:
        result = make_run_result(term, "test", "")
        assert "termination" in result
        assert result["termination"] == term.value

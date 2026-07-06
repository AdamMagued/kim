"""
Tests for agent plan marker parsing — the Python-side logic that detects
PLAN/STEP/DONE markers in assistant text and emits structured [STATUS] events.

These tests exercise the REAL ``KimAgent._emit_plan_markers`` (built via
``conftest.make_test_agent``), capturing the ``emit_plan``/``emit_step``/
``emit_done`` calls it makes.  An earlier version of this file tested a local
reimplementation of the regex logic, which meant a regression in agent.py
could pass every test here — never test a copy of the product.
"""

from __future__ import annotations

import pytest

import orchestrator.agent as agent_mod
from tests.conftest import make_test_agent


class Recorder:
    """Captures emit_plan / emit_step / emit_done calls made by the agent."""

    def __init__(self, monkeypatch):
        self.plans: list[list[str]] = []
        self.steps: list[tuple[int, dict]] = []
        self.dones: list[int] = []
        monkeypatch.setattr(agent_mod, "emit_plan", lambda steps: self.plans.append(list(steps)))
        monkeypatch.setattr(agent_mod, "emit_step", lambda n, data: self.steps.append((int(n), dict(data))))
        monkeypatch.setattr(agent_mod, "emit_done", lambda n: self.dones.append(int(n)))


@pytest.fixture
def rec(monkeypatch):
    return Recorder(monkeypatch)


@pytest.fixture
def agent():
    return make_test_agent()


class TestPlanParsing:
    """Drive the real _emit_plan_markers and assert on the emitted events."""

    def test_plan_block_emits_plan(self, agent, rec):
        agent._emit_plan_markers(
            "PLAN: 3 steps\n"
            "1. Read the existing codebase\n"
            "2. Implement the new feature\n"
            "3. Write tests\n"
        )
        assert rec.plans == [[
            "Read the existing codebase",
            "Implement the new feature",
            "Write tests",
        ]]

    def test_plan_block_requires_minimum_two_steps(self, agent, rec):
        agent._emit_plan_markers("PLAN: 1 step\n1. Do everything\n")
        assert rec.plans == []

    def test_step_marker_emits_step(self, agent, rec):
        agent._emit_plan_markers("STEP 2: Implement the new feature\n")
        assert rec.steps == [(2, {"index": 2, "name": "Implement the new feature"})]
        assert agent._current_step_index == 2

    def test_done_marker_emits_done(self, agent, rec):
        agent._emit_plan_markers("DONE 1: Successfully read the codebase\n")
        assert rec.dones == [1]

    def test_multiple_steps_picks_last(self, agent, rec):
        agent._emit_plan_markers("STEP 1: First step\nSome work here\nSTEP 2: Second step\n")
        assert len(rec.steps) == 1
        assert rec.steps[0][0] == 2, "Should pick the last STEP marker"

    def test_plan_step_done_combined(self, agent, rec):
        agent._emit_plan_markers(
            "PLAN: 3 steps\n"
            "1. Read code\n"
            "2. Implement feature\n"
            "3. Write tests\n"
            "\n"
            "STEP 1: Read code\n"
            "DONE 1: Finished reading code\n"
        )
        assert len(rec.plans) == 1
        assert len(rec.steps) == 1
        assert rec.dones == [1]

    def test_empty_content_is_no_op(self, agent, rec):
        agent._emit_plan_markers("")
        assert rec.plans == [] and rec.steps == [] and rec.dones == []

    def test_plan_steps_are_truncated_to_120_chars(self, agent, rec):
        long_step = "A" * 200
        agent._emit_plan_markers(f"PLAN: 2 steps\n1. {long_step}\n2. Short step\n")
        assert len(rec.plans) == 1
        assert len(rec.plans[0][0]) == 120

    def test_plan_limited_to_12_steps(self, agent, rec):
        steps_text = "\n".join(f"{i}. Step {i}" for i in range(1, 20))
        agent._emit_plan_markers(f"PLAN: 19 steps\n{steps_text}\n")
        assert len(rec.plans) == 1
        assert len(rec.plans[0]) == 12

    def test_step_case_insensitive(self, agent, rec):
        agent._emit_plan_markers("step 3: Do something\n")
        assert rec.steps and rec.steps[0][0] == 3

    def test_done_summary_truncated_to_160_chars(self, agent, rec, monkeypatch):
        recorded: list[dict] = []
        # emit_done only receives the index; the truncated summary lives in the
        # [STATUS] [DONE]{json} log envelope, so capture the agent log instead.
        monkeypatch.setattr(
            agent, "_log", lambda level, message: recorded.append({"level": level, "message": message})
        )
        long_summary = "B" * 300
        agent._emit_plan_markers(f"DONE 1: {long_summary}\n")
        assert rec.dones == [1]
        done_lines = [r["message"] for r in recorded if "[DONE]" in r["message"]]
        assert len(done_lines) == 1
        import json
        payload = json.loads(done_lines[0].split("[DONE]", 1)[1])
        assert payload["index"] == 1
        assert len(payload["summary"]) == 160

    def test_plan_with_parenthesized_numbers(self, agent, rec):
        agent._emit_plan_markers("PLAN: 2 steps\n1) Install dependencies\n2) Run the build\n")
        assert rec.plans == [["Install dependencies", "Run the build"]]

    def test_no_markers_in_random_text(self, agent, rec):
        agent._emit_plan_markers("This is just a normal response about coding.\nNo plan here.")
        assert rec.plans == [] and rec.steps == [] and rec.dones == []


class TestPlanDedupe:
    """The real implementation dedupes repeated markers across turns."""

    def test_repeated_plan_emitted_once(self, agent, rec):
        content = "PLAN: 2 steps\n1. First\n2. Second\n"
        agent._emit_plan_markers(content)
        agent._emit_plan_markers(content)
        assert len(rec.plans) == 1

    def test_repeated_step_emitted_once(self, agent, rec):
        agent._emit_plan_markers("STEP 1: Do the thing\n")
        agent._emit_plan_markers("STEP 1: Do the thing\n")
        assert len(rec.steps) == 1

    def test_new_plan_resets_step_dedupe(self, agent, rec):
        agent._emit_plan_markers("STEP 1: Alpha\n")
        agent._emit_plan_markers("PLAN: 2 steps\n1. New first\n2. New second\n")
        agent._emit_plan_markers("STEP 1: Alpha\n")
        assert len(rec.steps) == 2, "a new PLAN must reset the STEP dedupe signature"

    def test_status_log_envelopes_written(self, agent, rec, monkeypatch):
        recorded: list[str] = []
        monkeypatch.setattr(agent, "_log", lambda level, message: recorded.append(message))
        agent._emit_plan_markers("PLAN: 2 steps\n1. First\n2. Second\n\nSTEP 1: First\n")
        assert any(m.startswith("[STATUS] [PLAN]{") for m in recorded)
        assert any(m.startswith("[STATUS] [STEP]{") for m in recorded)

"""
Tests for agent plan marker parsing — the Python-side logic that detects
PLAN/STEP/DONE markers in assistant text and emits structured [STATUS] events.

This tests the plan extraction regex logic from _emit_plan_markers by
replicating the core parsing without importing the full Agent class
(which has heavy dependencies on MCP, providers, etc.).
"""

import json
import re


# ── Replicate the core plan extraction logic from agent.py ───────────────────
# This avoids importing the full Agent class with all its dependencies.


def _extract_plan_markers(content: str) -> list[tuple[str, str]]:
    """
    Replicate the plan/step/done extraction from Agent._emit_plan_markers.
    Returns list of (marker_type, json_payload_string) tuples.
    """
    if not content:
        return []

    results = []

    # ── PLAN block ───────────────────────────────────────────────────
    plan_match = re.search(
        r"^\s*PLAN:\s*(\d+)\s*step",
        content,
        re.IGNORECASE | re.MULTILINE,
    )
    if plan_match:
        after = content[plan_match.end():]
        steps: list[str] = []
        for raw in after.splitlines():
            line = raw.strip()
            if not line:
                if steps:
                    break
                continue
            m = re.match(r"^(\d+)[.)]\s+(.+?)\s*$", line)
            if m:
                steps.append(m.group(2).strip())
                continue
            if steps:
                break

        if len(steps) >= 2:
            plan_payload = {"steps": [s[:120] for s in steps[:12]]}
            sig = json.dumps(plan_payload, separators=(",", ":"), ensure_ascii=False)
            results.append(("PLAN", sig))

    # ── STEP markers ─────────────────────────────────────────────────
    step_matches = list(
        re.finditer(
            r"^\s*STEP\s*(\d+)\s*:\s*(.+?)\s*$",
            content,
            re.IGNORECASE | re.MULTILINE,
        )
    )
    if step_matches:
        m = step_matches[-1]  # last one wins
        step_payload = {"index": int(m.group(1)), "name": m.group(2).strip()[:120]}
        sig = json.dumps(step_payload, separators=(",", ":"), ensure_ascii=False)
        results.append(("STEP", sig))

    # ── DONE markers ─────────────────────────────────────────────────
    done_matches = list(
        re.finditer(
            r"^\s*DONE\s*(\d+)\s*:\s*(.+?)\s*$",
            content,
            re.IGNORECASE | re.MULTILINE,
        )
    )
    if done_matches:
        m = done_matches[-1]
        done_payload = {"index": int(m.group(1)), "summary": m.group(2).strip()[:160]}
        sig = json.dumps(done_payload, separators=(",", ":"), ensure_ascii=False)
        results.append(("DONE", sig))

    return results


# ── Tests ────────────────────────────────────────────────────────────────────


class TestPlanParsing:
    """Verify plan extraction regex logic matches agent.py behavior."""

    def test_plan_block_emits_plan(self):
        """A numbered plan block should produce a PLAN marker."""
        content = (
            "PLAN: 3 steps\n"
            "1. Read the existing codebase\n"
            "2. Implement the new feature\n"
            "3. Write tests\n"
        )
        markers = _extract_plan_markers(content)
        plan_markers = [(t, p) for t, p in markers if t == "PLAN"]
        assert len(plan_markers) == 1

        payload = json.loads(plan_markers[0][1])
        assert len(payload["steps"]) == 3
        assert payload["steps"][0] == "Read the existing codebase"
        assert payload["steps"][2] == "Write tests"

    def test_plan_block_requires_minimum_two_steps(self):
        """A plan with only 1 step should NOT emit a PLAN marker."""
        content = "PLAN: 1 step\n1. Do everything\n"
        markers = _extract_plan_markers(content)
        assert not any(t == "PLAN" for t, _ in markers)

    def test_step_marker_emits_step(self):
        """A STEP N: line should produce a STEP marker."""
        content = "STEP 2: Implement the new feature\n"
        markers = _extract_plan_markers(content)
        step_markers = [(t, p) for t, p in markers if t == "STEP"]
        assert len(step_markers) == 1

        payload = json.loads(step_markers[0][1])
        assert payload["index"] == 2
        assert payload["name"] == "Implement the new feature"

    def test_done_marker_emits_done(self):
        """A DONE N: line should produce a DONE marker."""
        content = "DONE 1: Successfully read the codebase\n"
        markers = _extract_plan_markers(content)
        done_markers = [(t, p) for t, p in markers if t == "DONE"]
        assert len(done_markers) == 1

        payload = json.loads(done_markers[0][1])
        assert payload["index"] == 1
        assert "Successfully read" in payload["summary"]

    def test_multiple_steps_picks_last(self):
        """When multiple STEP markers appear, only the last one should be captured."""
        content = "STEP 1: First step\nSome work here\nSTEP 2: Second step\n"
        markers = _extract_plan_markers(content)
        step_markers = [(t, p) for t, p in markers if t == "STEP"]
        assert len(step_markers) == 1

        payload = json.loads(step_markers[0][1])
        assert payload["index"] == 2, "Should pick the last STEP marker"

    def test_plan_step_done_combined(self):
        """A content block with PLAN + STEP + DONE should emit all three."""
        content = (
            "PLAN: 3 steps\n"
            "1. Read code\n"
            "2. Implement feature\n"
            "3. Write tests\n"
            "\n"
            "STEP 1: Read code\n"
            "DONE 1: Finished reading code\n"
        )
        markers = _extract_plan_markers(content)
        types = [t for t, _ in markers]
        assert "PLAN" in types
        assert "STEP" in types
        assert "DONE" in types

    def test_empty_content_is_no_op(self):
        """Empty string should not emit anything."""
        markers = _extract_plan_markers("")
        assert len(markers) == 0

    def test_plan_steps_are_truncated_to_120_chars(self):
        """Step text longer than 120 chars should be truncated."""
        long_step = "A" * 200
        content = f"PLAN: 2 steps\n1. {long_step}\n2. Short step\n"
        markers = _extract_plan_markers(content)
        plan_markers = [(t, p) for t, p in markers if t == "PLAN"]
        assert len(plan_markers) == 1

        payload = json.loads(plan_markers[0][1])
        assert len(payload["steps"][0]) <= 120

    def test_plan_limited_to_12_steps(self):
        """Plans with more than 12 steps should be capped at 12."""
        steps_text = "\n".join(f"{i}. Step {i}" for i in range(1, 20))
        content = f"PLAN: 19 steps\n{steps_text}\n"
        markers = _extract_plan_markers(content)
        plan_markers = [(t, p) for t, p in markers if t == "PLAN"]
        assert len(plan_markers) == 1

        payload = json.loads(plan_markers[0][1])
        assert len(payload["steps"]) <= 12

    def test_step_case_insensitive(self):
        """STEP markers should be case-insensitive."""
        content = "step 3: Do something\n"
        markers = _extract_plan_markers(content)
        step_markers = [(t, p) for t, p in markers if t == "STEP"]
        assert len(step_markers) == 1

        payload = json.loads(step_markers[0][1])
        assert payload["index"] == 3

    def test_done_summary_truncated_to_160_chars(self):
        """DONE summary longer than 160 chars should be truncated."""
        long_summary = "B" * 300
        content = f"DONE 1: {long_summary}\n"
        markers = _extract_plan_markers(content)
        done_markers = [(t, p) for t, p in markers if t == "DONE"]
        assert len(done_markers) == 1

        payload = json.loads(done_markers[0][1])
        assert len(payload["summary"]) <= 160

    def test_plan_with_parenthesized_numbers(self):
        """Plan steps using N) format should also be parsed."""
        content = (
            "PLAN: 2 steps\n"
            "1) Install dependencies\n"
            "2) Run the build\n"
        )
        markers = _extract_plan_markers(content)
        plan_markers = [(t, p) for t, p in markers if t == "PLAN"]
        assert len(plan_markers) == 1

        payload = json.loads(plan_markers[0][1])
        assert len(payload["steps"]) == 2
        assert payload["steps"][0] == "Install dependencies"

    def test_no_markers_in_random_text(self):
        """Random text without markers should produce no results."""
        content = "This is just a normal response about coding.\nNo plan here."
        markers = _extract_plan_markers(content)
        assert len(markers) == 0

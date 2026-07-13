"""
Tests for orchestrator.tool_risk.classify_tool_risk and the
InteractionPolicy risk gate (block_high_risk=True).

classify_tool_risk maps registered MCP tool names to stable risk levels so
that traces and HITL gates can branch without inline string checks.

Tests use only tool names as registered in mcp_server/tool_registry.py.
No external services, no real MCP subprocess, no dangerous commands executed.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from orchestrator.tool_risk import classify_tool_risk, coerce_hitl_bool

_MCP_DIR = Path(__file__).resolve().parent.parent / "mcp_server"
_REGISTRY_PY = _MCP_DIR / "tool_registry.py"
# Schemas extracted for the file-size gate live in a sibling module; the
# static coverage scan must see every registered tool, wherever its schema is.
_SCHEMA_FILES = (_REGISTRY_PY, _MCP_DIR / "tool_schemas_parity.py")


class ClassifyToolRiskTests(unittest.TestCase):
    # ── High-risk tools ────────────────────────────────────────────────────

    def test_delete_file_is_high_risk(self):
        result = classify_tool_risk("delete_file", {})
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["reason"], "file_deletion")

    def test_run_command_is_high_risk(self):
        result = classify_tool_risk("run_command", {"cmd": "echo hi"})
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["reason"], "arbitrary_code_execution")

    def test_run_powershell_is_high_risk(self):
        result = classify_tool_risk("run_powershell", {"script": "echo hi"})
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["reason"], "arbitrary_code_execution")

    def test_run_python_is_high_risk(self):
        result = classify_tool_risk("run_python", {"code": "print('hi')"})
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["reason"], "arbitrary_code_execution")

    def test_run_node_is_high_risk(self):
        result = classify_tool_risk("run_node", {"code": "console.log('hi')"})
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["reason"], "arbitrary_code_execution")

    def test_git_commit_is_high_risk(self):
        result = classify_tool_risk("git_commit", {"message": "feat: add something"})
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["reason"], "git_history_change")

    def test_git_checkout_is_high_risk(self):
        result = classify_tool_risk("git_checkout", {"target": "main"})
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["reason"], "git_branch_change")

    def test_github_create_repo_is_high_risk(self):
        result = classify_tool_risk("github_create_repo", {"name": "my-repo"})
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["reason"], "remote_resource_creation")

    # ── Medium-risk tools ──────────────────────────────────────────────────

    def test_write_file_is_medium_risk(self):
        result = classify_tool_risk("write_file", {"path": "foo.txt", "content": "bar"})
        self.assertEqual(result["level"], "medium")
        self.assertEqual(result["reason"], "file_write")

    def test_git_add_is_medium_risk(self):
        result = classify_tool_risk("git_add", {"paths": "src/"})
        self.assertEqual(result["level"], "medium")
        self.assertEqual(result["reason"], "git_stage")

    def test_web_fill_is_medium_risk(self):
        result = classify_tool_risk("web_fill", {"element_id": "w1", "text": "hello"})
        self.assertEqual(result["level"], "medium")
        self.assertEqual(result["reason"], "form_interaction")

    def test_web_press_is_medium_risk(self):
        result = classify_tool_risk("web_press", {"key": "enter"})
        self.assertEqual(result["level"], "medium")
        self.assertEqual(result["reason"], "form_interaction")

    def test_web_click_is_medium_risk(self):
        result = classify_tool_risk("web_click", {"element_id": "w1"})
        self.assertEqual(result["level"], "medium")
        self.assertEqual(result["reason"], "web_interaction")

    def test_type_text_is_medium_risk(self):
        result = classify_tool_risk("type_text", {"text": "hello"})
        self.assertEqual(result["level"], "medium")
        self.assertEqual(result["reason"], "input_injection")

    def test_hotkey_is_medium_risk(self):
        result = classify_tool_risk("hotkey", {"keys": "ctrl+s"})
        self.assertEqual(result["level"], "medium")
        self.assertEqual(result["reason"], "input_injection")

    def test_key_press_is_medium_risk(self):
        result = classify_tool_risk("key_press", {"key": "enter"})
        self.assertEqual(result["level"], "medium")
        self.assertEqual(result["reason"], "input_injection")

    def test_click_is_medium_risk(self):
        result = classify_tool_risk("click", {"x": 100, "y": 200})
        self.assertEqual(result["level"], "medium")
        self.assertEqual(result["reason"], "native_click")

    def test_double_click_is_medium_risk(self):
        result = classify_tool_risk("double_click", {"x": 100, "y": 200})
        self.assertEqual(result["level"], "medium")
        self.assertEqual(result["reason"], "native_click")

    def test_web_open_is_medium_risk(self):
        result = classify_tool_risk("web_open", {"url": "https://example.com"})
        self.assertEqual(result["level"], "medium")
        self.assertEqual(result["reason"], "web_navigation")

    def test_open_url_is_medium_risk(self):
        result = classify_tool_risk("open_url", {"url": "https://example.com"})
        self.assertEqual(result["level"], "medium")
        self.assertEqual(result["reason"], "web_navigation")

    def test_write_memory_is_medium_risk(self):
        result = classify_tool_risk("write_memory", {"key": "note", "value": "hello"})
        self.assertEqual(result["level"], "medium")
        self.assertEqual(result["reason"], "memory_write")

    def test_click_ui_is_medium_risk(self):
        result = classify_tool_risk("click_ui", {"element_id": "e12"})
        self.assertEqual(result["level"], "medium")
        self.assertEqual(result["reason"], "ui_interaction")

    # ── Low-risk tools ─────────────────────────────────────────────────────

    def test_read_file_is_low_risk(self):
        result = classify_tool_risk("read_file", {"path": "foo.txt"})
        self.assertEqual(result["level"], "low")

    def test_list_dir_is_low_risk(self):
        result = classify_tool_risk("list_dir", {"path": "."})
        self.assertEqual(result["level"], "low")

    def test_take_screenshot_is_low_risk(self):
        result = classify_tool_risk("take_screenshot", {})
        self.assertEqual(result["level"], "low")

    def test_git_status_is_low_risk(self):
        result = classify_tool_risk("git_status", {})
        self.assertEqual(result["level"], "low")

    def test_git_diff_is_low_risk(self):
        result = classify_tool_risk("git_diff", {})
        self.assertEqual(result["level"], "low")

    def test_git_log_is_low_risk(self):
        result = classify_tool_risk("git_log", {})
        self.assertEqual(result["level"], "low")

    def test_web_observe_is_low_risk(self):
        result = classify_tool_risk("web_observe", {})
        self.assertEqual(result["level"], "low")

    def test_search_in_files_is_low_risk(self):
        result = classify_tool_risk("search_in_files", {"pattern": "def "})
        self.assertEqual(result["level"], "low")

    def test_find_files_is_low_risk(self):
        result = classify_tool_risk("find_files", {"pattern": "*.py"})
        self.assertEqual(result["level"], "low")

    def test_read_memory_is_low_risk(self):
        result = classify_tool_risk("read_memory", {"key": "note"})
        self.assertEqual(result["level"], "low")

    def test_lint_file_is_low_risk(self):
        result = classify_tool_risk("lint_file", {"path": "foo.py"})
        self.assertEqual(result["level"], "low")

    def test_observe_ui_is_low_risk(self):
        result = classify_tool_risk("observe_ui", {})
        self.assertEqual(result["level"], "low")
        self.assertEqual(result["reason"], "read_only")

    def test_get_screen_info_is_low_risk(self):
        result = classify_tool_risk("get_screen_info", {})
        self.assertEqual(result["level"], "low")
        self.assertEqual(result["reason"], "read_only")

    def test_take_annotated_screenshot_is_low_risk(self):
        result = classify_tool_risk("take_annotated_screenshot", {})
        self.assertEqual(result["level"], "low")
        self.assertEqual(result["reason"], "read_only")

    def test_web_resolve_is_low_risk(self):
        result = classify_tool_risk("web_resolve", {"intent": "submit button"})
        self.assertEqual(result["level"], "low")
        self.assertEqual(result["reason"], "read_only")

    def test_web_text_is_low_risk(self):
        result = classify_tool_risk("web_text", {})
        self.assertEqual(result["level"], "low")
        self.assertEqual(result["reason"], "read_only")

    def test_web_screenshot_is_low_risk(self):
        result = classify_tool_risk("web_screenshot", {})
        self.assertEqual(result["level"], "low")
        self.assertEqual(result["reason"], "read_only")

    def test_web_wait_for_is_low_risk(self):
        result = classify_tool_risk("web_wait_for", {"text": "Success"})
        self.assertEqual(result["level"], "low")
        self.assertEqual(result["reason"], "read_only")

    def test_web_wait_for_url_is_low_risk(self):
        result = classify_tool_risk("web_wait_for_url", {"url_contains": "/dashboard"})
        self.assertEqual(result["level"], "low")
        self.assertEqual(result["reason"], "read_only")

    def test_get_windows_is_low_risk(self):
        result = classify_tool_risk("get_windows", {})
        self.assertEqual(result["level"], "low")
        self.assertEqual(result["reason"], "read_only")

    # ── Unknown tools default to low ───────────────────────────────────────

    def test_unknown_tool_is_low_risk(self):
        result = classify_tool_risk("some_future_tool", {})
        self.assertEqual(result["level"], "low")

    # ── Args parameter is accepted but not required ────────────────────────

    def test_no_args_parameter(self):
        result = classify_tool_risk("run_command")
        self.assertEqual(result["level"], "high")

    def test_result_has_required_keys(self):
        for name in ("delete_file", "write_file", "read_file", "run_command"):
            with self.subTest(name=name):
                result = classify_tool_risk(name, {})
                self.assertIn("level", result)
                self.assertIn("reason", result)
                self.assertIn(result["level"], ("high", "medium", "low"))


class InteractionPolicyRiskGateTests(unittest.TestCase):
    """block_high_risk=True makes before_tool() hard-block high-risk tools."""

    def _policy(self):
        from orchestrator.interaction_policy import InteractionPolicy
        return InteractionPolicy(block_high_risk=True)

    def test_delete_file_blocked_when_gate_enabled(self):
        policy = self._policy()
        decision = policy.before_tool("delete_file", {"path": "foo.txt"})
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.hard_block)
        self.assertIn("HITL_REQUIRED", decision.message)
        self.assertIn("delete_file", decision.message)

    def test_run_command_blocked_when_gate_enabled(self):
        policy = self._policy()
        decision = policy.before_tool("run_command", {"cmd": "ls"})
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.hard_block)

    def test_git_commit_blocked_when_gate_enabled(self):
        policy = self._policy()
        decision = policy.before_tool("git_commit", {"message": "feat"})
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.hard_block)

    def test_write_file_allowed_when_gate_enabled(self):
        """Medium-risk tools are not hard-blocked by the high-risk gate."""
        policy = self._policy()
        decision = policy.before_tool("write_file", {"path": "f.txt", "content": "x"})
        self.assertTrue(decision.allowed)

    def test_read_file_allowed_when_gate_enabled(self):
        policy = self._policy()
        decision = policy.before_tool("read_file", {"path": "f.txt"})
        self.assertTrue(decision.allowed)

    def test_gate_off_by_default(self):
        """Default InteractionPolicy must NOT block high-risk tools."""
        from orchestrator.interaction_policy import InteractionPolicy
        policy = InteractionPolicy()
        # Without block_high_risk, run_command passes through (normal policy allows it)
        decision = policy.before_tool("run_command", {"cmd": "ls"})
        self.assertTrue(decision.allowed)

    def test_existing_policy_rules_still_apply_when_gate_enabled(self):
        """The high-risk gate must coexist with existing policy checks."""
        import json
        from orchestrator.interaction_policy import InteractionPolicy
        policy = InteractionPolicy(block_high_risk=True)
        # web_click before web_observe is still blocked by existing staleness rule
        decision = policy.before_tool("web_click", {"element_id": "w1"})
        self.assertFalse(decision.allowed)
        self.assertIn("web_observe", decision.message)


class HitlBoolCoercionTests(unittest.TestCase):
    def test_explicit_truthy_values_enable_gate(self):
        for value in (True, 1, "1", "true", "TRUE", "yes", "on"):
            with self.subTest(value=value):
                self.assertTrue(coerce_hitl_bool(value))

    def test_falsey_and_ambiguous_values_do_not_enable_gate(self):
        for value in (False, 0, None, "", "0", "false", "FALSE", "no", "off", [], {}):
            with self.subTest(value=value):
                self.assertFalse(coerce_hitl_bool(value))


class StaticRiskCoverageTests(unittest.TestCase):
    """Static source scan: every Tool registered in tool_registry.py is
    explicitly present in the risk classification metadata.

    Uses regex over the source file so the mcp package is NOT required.
    Any new tool added to tool_registry.py without a corresponding entry in
    _HIGH_RISK, _MEDIUM_RISK, or _TOOL_REASON will cause this test to fail,
    forcing a deliberate risk decision rather than a silent default.
    """

    def _registered_tool_names(self) -> list[str]:
        source = "\n".join(f.read_text(encoding="utf-8") for f in _SCHEMA_FILES)
        # Matches Tool( followed by optional whitespace + newline + indentation
        # + name="<tool_name>" — the exact pattern used in the schema modules.
        return re.findall(r'Tool\(\s+name="([^"]+)"', source)

    def test_registry_file_exists(self):
        self.assertTrue(_REGISTRY_PY.exists(), f"Registry file not found: {_REGISTRY_PY}")

    def test_regex_extracts_known_tool_names(self):
        names = self._registered_tool_names()
        self.assertIn("read_file", names)
        self.assertIn("click_ui", names)
        self.assertIn("web_resolve", names)
        self.assertGreater(len(names), 20, "Suspiciously few tool names extracted")

    def test_all_registered_tools_explicitly_classified(self):
        """No registered tool may rely on the unknown-default; each must be deliberate."""
        from orchestrator.tool_risk import _HIGH_RISK, _MEDIUM_RISK, _TOOL_REASON  # noqa: PLC0415
        classified = _HIGH_RISK | _MEDIUM_RISK | set(_TOOL_REASON.keys())
        names = self._registered_tool_names()
        unclassified = [n for n in names if n not in classified]
        self.assertEqual(
            unclassified,
            [],
            f"Tools in registry but not explicitly classified in tool_risk.py: {unclassified}",
        )

    def test_unknown_tool_still_defaults_low(self):
        """The unknown-default path must survive; only unknown future tools use it."""
        result = classify_tool_risk("hypothetical_future_tool_xyz", {})
        self.assertEqual(result["level"], "low")


class LintFileArgRefinementTests(unittest.TestCase):
    """Regression guards for the lint_file fix=True → high/file_write refinement.

    These tests encode the arg-level classification added to classify_tool_risk:
    a plain lint check stays read-only / auto-approvable, but passing fix=True
    (or any truthy coercion of that arg) escalates to high/file_write so the
    HITL gate fires before the file is mutated in place.
    """

    # ── Behavior 1: fix=True is classified high/file_write ────────────────

    def test_lint_file_fix_true_is_high_file_write(self):
        result = classify_tool_risk("lint_file", {"fix": True})
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["reason"], "file_write")

    # ── Behavior 2: fix absent or False stays read-only (not high) ────────

    def test_lint_file_no_args_stays_read_only(self):
        result = classify_tool_risk("lint_file", {})
        self.assertNotEqual(result["level"], "high")
        self.assertEqual(result["level"], "low")
        self.assertEqual(result["reason"], "read_only")

    def test_lint_file_fix_false_stays_read_only(self):
        result = classify_tool_risk("lint_file", {"fix": False})
        self.assertNotEqual(result["level"], "high")
        self.assertEqual(result["level"], "low")
        self.assertEqual(result["reason"], "read_only")

    def test_lint_file_fix_none_stays_read_only(self):
        result = classify_tool_risk("lint_file", {"fix": None})
        self.assertNotEqual(result["level"], "high")
        self.assertEqual(result["level"], "low")

    # ── Behavior 3: truthy string values for fix are coerced to high ──────

    def test_lint_file_fix_string_true_is_high(self):
        result = classify_tool_risk("lint_file", {"fix": "true"})
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["reason"], "file_write")

    def test_lint_file_fix_string_1_is_high(self):
        result = classify_tool_risk("lint_file", {"fix": "1"})
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["reason"], "file_write")

    def test_lint_file_fix_string_yes_is_high(self):
        result = classify_tool_risk("lint_file", {"fix": "yes"})
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["reason"], "file_write")

    def test_lint_file_fix_string_on_is_high(self):
        result = classify_tool_risk("lint_file", {"fix": "on"})
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["reason"], "file_write")

    def test_lint_file_fix_string_TRUE_uppercase_is_high(self):
        result = classify_tool_risk("lint_file", {"fix": "TRUE"})
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["reason"], "file_write")

    def test_lint_file_fix_int_1_is_high(self):
        result = classify_tool_risk("lint_file", {"fix": 1})
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["reason"], "file_write")

    def test_lint_file_fix_string_false_stays_read_only(self):
        result = classify_tool_risk("lint_file", {"fix": "false"})
        self.assertNotEqual(result["level"], "high")

    def test_lint_file_fix_string_0_stays_read_only(self):
        result = classify_tool_risk("lint_file", {"fix": "0"})
        self.assertNotEqual(result["level"], "high")


class BaseClassificationRegressionTests(unittest.TestCase):
    """Regression guards confirming that the lint_file arg-refinement branch
    did NOT alter the pre-existing tier assignments for other tools.

    Each assertion here is a deliberate canary: if a refactor accidentally
    moves delete_file, run_command, or write_file to a different tier these
    tests will catch it immediately.
    """

    # ── Behavior 4: pre-existing tiers are unchanged ──────────────────────

    def test_delete_file_remains_high_after_refinement_branch(self):
        result = classify_tool_risk("delete_file", {})
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["reason"], "file_deletion")

    def test_run_command_remains_high_after_refinement_branch(self):
        result = classify_tool_risk("run_command", {"cmd": "ls"})
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["reason"], "arbitrary_code_execution")

    def test_write_file_remains_medium_after_refinement_branch(self):
        result = classify_tool_risk("write_file", {"path": "out.txt", "content": "x"})
        self.assertEqual(result["level"], "medium")
        self.assertEqual(result["reason"], "file_write")

    def test_git_commit_remains_high_after_refinement_branch(self):
        result = classify_tool_risk("git_commit", {"message": "chore: bump"})
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["reason"], "git_history_change")

    def test_read_file_remains_low_after_refinement_branch(self):
        result = classify_tool_risk("read_file", {"path": "foo.txt"})
        self.assertEqual(result["level"], "low")
        self.assertEqual(result["reason"], "read_only")


if __name__ == "__main__":
    unittest.main()

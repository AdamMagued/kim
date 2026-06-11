"""
Tests for orchestrator.tool_errors.classify_tool_output.

The classifier maps well-known tool output prefixes to stable error codes so
that traces and HITL logic can branch on error type without fragile inline
string checks.  Only the leading bytes of output are inspected; the classifier
does not keyword-scan output bodies (false-positive risk).
"""
from __future__ import annotations

import unittest

from orchestrator.tool_errors import classify_tool_output


class ClassifyToolOutputTests(unittest.TestCase):
    # ── Error prefixes ─────────────────────────────────────────────────────

    def test_permission_error_prefix(self):
        self.assertEqual(
            classify_tool_output("PERMISSION_ERROR: refusing git path '/etc' (outside root)"),
            "permission_denied",
        )

    def test_permission_error_cwd_variant(self):
        self.assertEqual(
            classify_tool_output("PERMISSION_ERROR: cwd /tmp not allowed"),
            "permission_denied",
        )

    def test_blocked_prefix(self):
        self.assertEqual(
            classify_tool_output("BLOCKED: 'rm' is a blocked command"),
            "blocked",
        )

    def test_blocked_pattern_variant(self):
        self.assertEqual(
            classify_tool_output("BLOCKED: Command matches dangerous pattern"),
            "blocked",
        )

    def test_blocked_metachar_variant(self):
        self.assertEqual(
            classify_tool_output(
                "BLOCKED: Command contains chaining metacharacters (;, &&, ||, |, `, $(...)). "
                "Use separate run_command calls."
            ),
            "blocked",
        )

    def test_timeout_prefix(self):
        self.assertEqual(
            classify_tool_output("TIMEOUT: command exceeded 30s"),
            "timeout",
        )

    def test_timeout_powershell_variant(self):
        self.assertEqual(
            classify_tool_output("TIMEOUT: PowerShell exceeded 60s"),
            "timeout",
        )

    def test_os_limitation_prefix(self):
        self.assertEqual(
            classify_tool_output(
                "OS_LIMITATION: PowerShell is not available on this macOS system. "
                "Please use the 'run_command' tool with bash/zsh syntax instead."
            ),
            "os_limitation",
        )

    def test_not_found_prefix(self):
        self.assertEqual(
            classify_tool_output("NOT_FOUND: 'my_key' - available keys: ['other']"),
            "not_found",
        )

    def test_error_colon_prefix(self):
        self.assertEqual(
            classify_tool_output("ERROR: File not found: /path/to/file"),
            "execution_error",
        )

    def test_error_colon_not_file_variant(self):
        self.assertEqual(
            classify_tool_output("ERROR: Not a file: /path"),
            "execution_error",
        )

    def test_error_colon_git_not_installed(self):
        self.assertEqual(
            classify_tool_output("ERROR: 'git' is not installed or not found on PATH."),
            "execution_error",
        )

    def test_error_calling_session_failure(self):
        """MCP session/transport error emitted by agent.py — distinct from tool logic."""
        self.assertEqual(
            classify_tool_output("ERROR calling read_file: connection refused"),
            "internal_error",
        )

    def test_error_calling_another_tool(self):
        self.assertEqual(
            classify_tool_output("ERROR calling run_command: subprocess failed"),
            "internal_error",
        )

    # ── Success / non-error outputs ────────────────────────────────────────

    def test_success_write_file(self):
        self.assertIsNone(classify_tool_output("Written 42 chars to /path/file.py"))

    def test_success_exit_code_zero(self):
        self.assertIsNone(classify_tool_output("exit_code: 0\nstdout:\nhello"))

    def test_success_json_output(self):
        self.assertIsNone(classify_tool_output('{"type": "result", "value": 42}'))

    def test_success_memory_ok_prefix(self):
        self.assertIsNone(classify_tool_output("OK: stored 'key' (5 chars) in memory.json"))

    def test_empty_string(self):
        self.assertIsNone(classify_tool_output(""))

    def test_whitespace_only(self):
        # Whitespace-only output does not start with any error prefix.
        self.assertIsNone(classify_tool_output("   \n"))

    def test_body_contains_error_word_not_classified(self):
        """'error' appearing in the body (not as a prefix) must not be classified."""
        self.assertIsNone(
            classify_tool_output("exit_code: 1\nstderr:\nbash: error: command not found")
        )

    def test_body_contains_not_found_not_classified(self):
        """File content that says 'not found' inside must not be classified."""
        self.assertIsNone(
            classify_tool_output("line 1\nline 2: symbol not found\nline 3")
        )

    # ── Edge cases ─────────────────────────────────────────────────────────

    def test_error_without_colon_not_classified(self):
        """'ERROR ' without a colon does not match the 'ERROR:' prefix."""
        self.assertIsNone(classify_tool_output("ERROR without colon"))

    def test_case_sensitive(self):
        """Prefixes are case-sensitive — lowercase 'error:' is not a tool error."""
        self.assertIsNone(classify_tool_output("error: something went wrong"))


if __name__ == "__main__":
    unittest.main()

"""
Contract tests for provider error normalization.

The agent retry boundary used to retry every OSError.  PermissionError is an
OSError subclass, so actionable auth failures such as "Sign in to Ollama" were
retried even though the user must fix credentials.  The central classifier keeps
retry and status-code behavior consistent while preserving existing exception
messages for old callers.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from orchestrator.providers.base import ProviderError, classify_provider_error

_AGENT_PY = Path(__file__).resolve().parent.parent / "orchestrator" / "agent.py"


class ProviderErrorClassificationTests(unittest.TestCase):
    def test_permission_error_is_auth_and_not_retryable(self):
        result = classify_provider_error(PermissionError("Sign in to Ollama to use cloud models."))
        self.assertEqual(result.code, "auth")
        self.assertFalse(result.retryable)

    def test_auth_markers_beat_oserror_retry(self):
        result = classify_provider_error(OSError("unauthorized: missing API key"))
        self.assertEqual(result.code, "auth")
        self.assertFalse(result.retryable)

    def test_network_oserror_is_retryable(self):
        result = classify_provider_error(OSError("connection reset by peer"))
        self.assertEqual(result.code, "network")
        self.assertTrue(result.retryable)

    def test_timeout_is_retryable(self):
        result = classify_provider_error(TimeoutError("provider timeout"))
        self.assertEqual(result.code, "timeout")
        self.assertTrue(result.retryable)

    def test_rate_limit_is_retryable(self):
        result = classify_provider_error(RuntimeError("HTTP 429 rate limit exceeded"))
        self.assertEqual(result.code, "rate_limit")
        self.assertTrue(result.retryable)

    def test_server_error_is_retryable(self):
        result = classify_provider_error(RuntimeError("provider returned 503"))
        self.assertEqual(result.code, "server_error")
        self.assertTrue(result.retryable)

    def test_invalid_request_is_not_retryable(self):
        result = classify_provider_error(ValueError("invalid request: bad schema"))
        self.assertEqual(result.code, "invalid_request")
        self.assertFalse(result.retryable)

    def test_unknown_error_is_not_retryable(self):
        result = classify_provider_error(RuntimeError("model rejected the prompt"))
        self.assertEqual(result.code, "unknown")
        self.assertFalse(result.retryable)

    def test_existing_provider_error_is_preserved(self):
        original = ProviderError("quota_project", "quota project missing", retryable=False)
        self.assertIs(classify_provider_error(original), original)

    def test_message_is_preserved_for_old_callers(self):
        result = classify_provider_error(RuntimeError("HTTP 500 from provider"))
        self.assertEqual(str(result), "HTTP 500 from provider")
        self.assertEqual(result.message, "HTTP 500 from provider")


class AgentProviderErrorBoundaryTests(unittest.TestCase):
    """Static contracts for agent.py without importing the MCP-dependent module."""

    def setUp(self):
        self.source = _AGENT_PY.read_text(encoding="utf-8")

    def test_agent_imports_classifier(self):
        self.assertIn("classify_provider_error", self.source)

    def test_retryable_delegates_to_classifier(self):
        tree = ast.parse(self.source)
        found_retry = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_is_retryable":
                found_retry = True
                body_src = ast.get_source_segment(self.source, node) or ""
                self.assertIn("classify_provider_error(error).retryable", body_src)
        self.assertTrue(found_retry, "_is_retryable not found in agent.py")

    def test_provider_error_status_code_is_emitted(self):
        self.assertIn("[STATUS] provider error:", self.source)
        self.assertIn("provider_error.code", self.source)

    def test_retry_warning_includes_code(self):
        self.assertIn("({provider_error.code})", self.source)


if __name__ == "__main__":
    unittest.main()

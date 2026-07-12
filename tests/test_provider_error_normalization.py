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

    def test_httpx_read_timeout_is_retryable_timeout(self):
        # F-B-5: httpx.ReadTimeout is not a builtin TimeoutError and its str()
        # is "timed out" (no "timeout" substring) — the classifier must still
        # mark it a retryable timeout via the exception class name.
        import httpx
        result = classify_provider_error(httpx.ReadTimeout("timed out"))
        self.assertEqual(result.code, "timeout")
        self.assertTrue(result.retryable)

    def test_httpx_connect_timeout_empty_message_is_retryable(self):
        # F-B-5: httpx.ConnectTimeout often has an empty str(); rely on the
        # class name ("connecttimeout") so it is not misfiled as "unknown".
        import httpx
        result = classify_provider_error(httpx.ConnectTimeout(""))
        self.assertEqual(result.code, "timeout")
        self.assertTrue(result.retryable)

    def test_timed_out_wording_is_retryable_timeout(self):
        result = classify_provider_error(RuntimeError("the operation timed out"))
        self.assertEqual(result.code, "timeout")
        self.assertTrue(result.retryable)

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


# ---------------------------------------------------------------------------
# Pytest-style regression guards added for the four targeted behaviours
# ---------------------------------------------------------------------------


def test_auth_word_boundary_no_false_positive():
    """'author', 'authority', and 'authenticated' must NOT trigger the auth branch."""
    result_author = classify_provider_error(Exception("author of the book"))
    assert result_author.code != "auth", (
        "'author of the book' should not classify as auth (false positive via word boundary)"
    )

    result_authority = classify_provider_error(Exception("authority error"))
    assert result_authority.code != "auth", (
        "'authority error' should not classify as auth"
    )

    # 'authenticated' describes a success state; it must not be treated as an auth failure.
    result_authenticated = classify_provider_error(Exception("user is authenticated"))
    assert result_authenticated.code != "auth", (
        "'authenticated' is a success state and must not classify as auth"
    )


def test_auth_and_oauth_word_match():
    """Standalone 'auth' and 'oauth' tokens (and explicit markers) → auth, retryable=False."""
    # bare 'auth' token with word boundaries on both sides
    r = classify_provider_error(Exception("please auth again"))
    assert r.code == "auth"
    assert r.retryable is False

    # 'oauth' as a standalone token
    r_oauth = classify_provider_error(Exception("oauth token expired"))
    assert r_oauth.code == "auth"
    assert r_oauth.retryable is False

    # explicit markers that are always auth
    for msg in (
        "sign in to continue",
        "invalid api key provided",
        "forbidden: access denied",
        "unauthorized request",
        "missing credential",
    ):
        r = classify_provider_error(Exception(msg))
        assert r.code == "auth", f"Expected 'auth' for message: {msg!r}, got {r.code!r}"
        assert r.retryable is False, f"Auth error must not be retryable for: {msg!r}"


def test_http_400_digit_bounded():
    """'400 bad request' → invalid_request; 'error 4001' must NOT match that branch."""
    r_400 = classify_provider_error(Exception("400 bad request"))
    assert r_400.code == "invalid_request", (
        f"'400 bad request' should be invalid_request, got {r_400.code!r}"
    )

    r_4001 = classify_provider_error(Exception("error 4001"))
    assert r_4001.code != "invalid_request", (
        "'error 4001' must not match the 400 digit-bounded pattern and become invalid_request"
    )


def test_server_and_rate_codes():
    """429 / 'rate limit' → rate_limit retryable; digit-bounded 500/502/503/529 → server_error retryable."""
    # rate-limit via numeric code
    r_429 = classify_provider_error(Exception("HTTP 429 Too Many Requests"))
    assert r_429.code == "rate_limit"
    assert r_429.retryable is True

    # rate-limit via text phrase
    r_rl = classify_provider_error(Exception("rate limit exceeded"))
    assert r_rl.code == "rate_limit"
    assert r_rl.retryable is True

    # server error codes — each must be digit-bounded and retryable
    for code in ("500", "502", "503", "529"):
        r = classify_provider_error(Exception(f"provider returned {code} error"))
        assert r.code == "server_error", (
            f"HTTP {code} should be server_error, got {r.code!r}"
        )
        assert r.retryable is True, f"server_error for {code} must be retryable"

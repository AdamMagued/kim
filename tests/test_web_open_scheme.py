"""
Regression tests for handle_web_open URL-scheme enforcement and SSRF guards.

Covers:
  - Local/privileged scheme refusal (file:, chrome:, about:, view-source:,
    filesystem:) — must return an error before any browser call.
  - _is_ssrf_target() correctly identifies loopback / private / link-local IPs
    as True and public domain names as False.
  - handle_web_open rejects loopback IPs without touching the browser.

All assertions are against HEAD behaviour; these are regression guards.
"""

from __future__ import annotations

import asyncio
import unittest

from mcp_server.tools.web import _is_ssrf_target, handle_web_open


# ---------------------------------------------------------------------------
# 1. local_privileged_schemes_refused
# ---------------------------------------------------------------------------

class TestPrivilegedSchemesRefused(unittest.TestCase):
    """handle_web_open must refuse privileged / local URL schemes immediately."""

    PRIVILEGED_URLS = [
        "file:///etc/passwd",
        "FILE://x",
        "chrome://settings",
        "about:blank",
        "view-source:http://x",
        "filesystem:http://x",
    ]

    def _run(self, url: str) -> str:
        return asyncio.run(handle_web_open({"url": url}))

    def test_local_privileged_schemes_refused(self):
        """Each privileged/local URL scheme must return a string starting with 'ERROR: refusing'."""
        for url in self.PRIVILEGED_URLS:
            with self.subTest(url=url):
                result = self._run(url)
                self.assertIsInstance(result, str, f"Expected str for {url!r}, got {type(result)}")
                self.assertTrue(
                    result.startswith("ERROR: refusing"),
                    f"Expected result to start with 'ERROR: refusing' for {url!r}, got {result!r}",
                )


# ---------------------------------------------------------------------------
# 2. ssrf_target_loopback_private_true
# ---------------------------------------------------------------------------

class TestSsrfTargetDetection(unittest.TestCase):
    """_is_ssrf_target() must detect loopback/private/link-local IPs."""

    SSRF_URLS = [
        "http://127.0.0.1",
        "http://127.0.0.1:8080",
        "http://10.0.0.1",
        "http://10.10.10.10",
        "http://192.168.1.1",
        "http://169.254.1.1",
        "http://[::1]",
    ]

    def test_ssrf_target_loopback_private_true(self):
        """Loopback, private, and link-local addresses must be detected as SSRF targets."""
        for url in self.SSRF_URLS:
            with self.subTest(url=url):
                result = _is_ssrf_target(url)
                self.assertTrue(
                    result,
                    f"_is_ssrf_target({url!r}) should be True but returned {result!r}",
                )


# ---------------------------------------------------------------------------
# 3. ssrf_target_public_domain_false
# ---------------------------------------------------------------------------

    def test_ssrf_target_public_domain_false(self):
        """Public domain names must NOT be flagged as SSRF targets."""
        result = _is_ssrf_target("https://example.com")
        self.assertFalse(
            result,
            f"_is_ssrf_target('https://example.com') should be False but returned {result!r}",
        )


# ---------------------------------------------------------------------------
# 4. ssrf_loopback_refused_by_handler
# ---------------------------------------------------------------------------

class TestSsrfLoopbackRefusedByHandler(unittest.TestCase):
    """handle_web_open must reject loopback addresses before calling _page()."""

    def test_ssrf_loopback_refused_by_handler(self):
        """http://127.0.0.1:8080 must be refused with the internal-address error message."""
        result = asyncio.run(handle_web_open({"url": "http://127.0.0.1:8080"}))
        self.assertIsInstance(result, str)
        self.assertIn(
            "ERROR: refusing to open an internal/loopback address",
            result,
            f"Expected SSRF refusal message but got: {result!r}",
        )


if __name__ == "__main__":
    unittest.main()

"""Tests for auto-launching Chrome when it isn't reachable over CDP.

In visible mode the BrowserProvider now opens a real Chrome (remote-debugging
enabled) so the user can sign in, instead of erroring out with manual launch
instructions. These tests cover the pure/mocked seams — no real browser.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from orchestrator.providers.browser import provider as bp
from orchestrator.providers.browser.provider import BrowserProvider


def _provider(**bp_cfg):
    return BrowserProvider({"project_root": ".", "browser_provider": bp_cfg})


class TestAutoLaunchConfig(unittest.TestCase):
    def test_defaults_on(self):
        self.assertTrue(_provider()._auto_launch_chrome)

    def test_opt_out(self):
        self.assertFalse(_provider(browser_auto_launch=False)._auto_launch_chrome)


class TestSiteLaunchUrl(unittest.TestCase):
    def test_uses_preferred_site_pattern(self):
        p = _provider(preferred_site="gemini")
        self.assertEqual(p._site_launch_url(), "https://gemini.google.com/")

    def test_falls_back_to_a_configured_site(self):
        p = _provider()  # no preferred site
        url = p._site_launch_url()
        # Some configured AI-chat site, rendered as a full https URL.
        assert url is not None
        self.assertTrue(url.startswith("https://") and url.endswith("/"))


class TestChromeExecutable(unittest.TestCase):
    def test_returns_existing_absolute_candidate_on_macos(self):
        p = _provider()
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        with patch.object(bp.platform, "system", return_value="Darwin"), \
             patch.object(bp.os.path, "exists", side_effect=lambda c: c == chrome):
            self.assertEqual(p._chrome_executable(), chrome)

    def test_resolves_via_which_on_linux(self):
        p = _provider()
        with patch.object(bp.platform, "system", return_value="Linux"), \
             patch.object(bp.os.path, "exists", return_value=False), \
             patch.object(bp.shutil, "which",
                          side_effect=lambda c: "/usr/bin/google-chrome"
                          if c == "google-chrome" else None):
            self.assertEqual(p._chrome_executable(), "/usr/bin/google-chrome")

    def test_returns_none_when_nothing_found(self):
        p = _provider()
        with patch.object(bp.platform, "system", return_value="Linux"), \
             patch.object(bp.os.path, "exists", return_value=False), \
             patch.object(bp.shutil, "which", return_value=None):
            self.assertIsNone(p._chrome_executable())


class TestLaunchHeadedChrome(unittest.IsolatedAsyncioTestCase):
    async def test_no_chrome_binary_returns_none_without_launching(self):
        p = _provider()
        with patch.object(p, "_chrome_executable", return_value=None), \
             patch.object(bp.subprocess, "Popen") as popen:
            result = await p._launch_headed_chrome(MagicMock(), 9222)
        self.assertIsNone(result)
        popen.assert_not_called()

    async def test_launches_with_debugging_flags_then_attaches(self):
        p = _provider(preferred_site="gemini")
        sentinel_browser = object()
        fake_pw = MagicMock()
        fake_pw.chromium.connect_over_cdp = AsyncMock(return_value=sentinel_browser)

        with patch.object(p, "_chrome_executable", return_value="/bin/chrome"), \
             patch.object(bp.subprocess, "Popen") as popen, \
             patch.object(bp.asyncio, "sleep", new=AsyncMock()):
            result = await p._launch_headed_chrome(fake_pw, 9222)

        self.assertIs(result, sentinel_browser)
        popen.assert_called_once()
        args = popen.call_args.args[0]
        self.assertEqual(args[0], "/bin/chrome")
        self.assertIn("--remote-debugging-port=9222", args)
        self.assertTrue(any(a.startswith("--user-data-dir=") for a in args))
        self.assertIn("https://gemini.google.com/", args)

    async def test_returns_none_when_cdp_never_comes_up(self):
        p = _provider()
        fake_pw = MagicMock()
        fake_pw.chromium.connect_over_cdp = AsyncMock(side_effect=Exception("refused"))

        with patch.object(p, "_chrome_executable", return_value="/bin/chrome"), \
             patch.object(bp.subprocess, "Popen"), \
             patch.object(bp.asyncio, "sleep", new=AsyncMock()):
            result = await p._launch_headed_chrome(fake_pw, 9222)

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()

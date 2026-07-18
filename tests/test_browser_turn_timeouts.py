"""F-B-15: a browser turn must never hang silently.

Covers turn_timeouts.py directly (phase resolution, run_phase success/
timeout paths, NEED_HELP formatting) and end-to-end through
BrowserProvider.complete() with an injected driver/page that never actually
completes a phase — the exact QA repro (composer never driven, turn sat on
"Working…" forever with no error).
"""
from __future__ import annotations

import asyncio
import os
import time
import unittest
from typing import Optional, cast
from unittest.mock import patch

from orchestrator.providers.browser.page_driver import PageLike
from orchestrator.providers.browser.provider import BrowserProvider
from orchestrator.providers.browser.turn_timeouts import (
    PhaseTimeout,
    phase_timeout_response,
    phase_timeout_s,
    run_phase,
)

MESSAGES = [{"role": "user", "content": "hello"}]


# ---------------------------------------------------------------------------
# turn_timeouts.py unit tests
# ---------------------------------------------------------------------------

class TestPhaseTimeoutS(unittest.TestCase):
    def test_default_when_unset(self):
        self.assertEqual(phase_timeout_s({}, "connect"), 45.0)
        self.assertEqual(phase_timeout_s({}, "submit"), 90.0)

    def test_config_override(self):
        cfg = {"browser_provider": {"timeouts": {"connect": 12}}}
        self.assertEqual(phase_timeout_s(cfg, "connect"), 12.0)

    def test_env_wins_over_config(self):
        cfg = {"browser_provider": {"timeouts": {"connect": 12}}}
        os.environ["KIM_BROWSER_TIMEOUT_CONNECT"] = "7"
        try:
            self.assertEqual(phase_timeout_s(cfg, "connect"), 7.0)
        finally:
            del os.environ["KIM_BROWSER_TIMEOUT_CONNECT"]

    def test_invalid_env_falls_back(self):
        os.environ["KIM_BROWSER_TIMEOUT_CONNECT"] = "not-a-number"
        try:
            self.assertEqual(phase_timeout_s({}, "connect"), 45.0)
        finally:
            del os.environ["KIM_BROWSER_TIMEOUT_CONNECT"]


class TestRunPhase(unittest.IsolatedAsyncioTestCase):
    async def test_returns_value_on_success(self):
        async def _ok():
            return 42

        result = await run_phase(_ok(), phase="connect", timeout_s=5.0)
        self.assertEqual(result, 42)

    async def test_raises_phase_timeout_when_exceeded(self):
        async def _never():
            await asyncio.sleep(10)

        start = time.monotonic()
        with self.assertRaises(PhaseTimeout) as ctx:
            await run_phase(_never(), phase="submit", timeout_s=0.05, site="claude")
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 2.0)  # fails fast, not after the full sleep
        self.assertEqual(ctx.exception.phase, "submit")
        self.assertEqual(ctx.exception.site, "claude")

    async def test_underlying_exception_propagates_unchanged(self):
        async def _boom():
            raise RuntimeError("composer not found")

        with self.assertRaises(RuntimeError):
            await run_phase(_boom(), phase="submit", timeout_s=5.0)


class TestPhaseTimeoutResponse(unittest.TestCase):
    def test_names_phase_and_site_in_need_help(self):
        exc = PhaseTimeout("submit", 90.0, site="chatgpt")
        resp = phase_timeout_response(exc)
        self.assertEqual(resp["type"], "text")
        self.assertIn("NEED_HELP", resp["content"])
        self.assertIn("chatgpt", resp["content"])
        self.assertIn("90", resp["content"])

    def test_submit_phase_warns_message_may_already_have_posted(self):
        # The submit-phase timeout can land just after Enter was pressed —
        # the message may already be sent, so the text must tell the user
        # to check before resending rather than just "resend".
        exc = PhaseTimeout("submit", 90.0, site="chatgpt")
        resp = phase_timeout_response(exc)
        self.assertIn("already posted", resp["content"])

    def test_connect_phase_keeps_generic_resend_wording(self):
        exc = PhaseTimeout("connect", 45.0, site="chatgpt")
        resp = phase_timeout_response(exc)
        self.assertNotIn("already posted", resp["content"])
        self.assertIn("resend", resp["content"])


# ---------------------------------------------------------------------------
# End-to-end: BrowserProvider.complete() must never hang past its budgets
# ---------------------------------------------------------------------------

class _HangingDriver:
    """A PageDriver whose acquire() never returns — simulates a dead CDP
    connection / a tab scan that never resolves (the "connect" phase)."""

    async def acquire(self) -> tuple[Optional[PageLike], Optional[str]]:
        await asyncio.sleep(30)
        return None, None  # pragma: no cover — never reached


class _StuckComposerPage:
    """A page whose composer selector IS found, but every interaction with
    it hangs — simulates the QA repro: tab open, but never actually driven
    (the "submit" phase)."""

    site = "claude"

    class _Keyboard:
        async def press(self, key: str) -> None:
            return None

    def __init__(self) -> None:
        self.url = "https://claude.ai/new"
        self.keyboard = self._Keyboard()

    def locator(self, selector: str) -> "_StuckComposerPage":
        return self

    async def count(self) -> int:
        return 1

    def nth(self, index: int) -> "_StuckComposerPage":
        return self

    @property
    def first(self) -> "_StuckComposerPage":
        return self

    async def all(self):
        return [self]

    async def is_visible(self) -> bool:
        return True

    async def inner_text(self) -> str:
        return ""

    async def text_content(self):
        return ""

    async def click(self, selector: str, **kwargs) -> None:
        await asyncio.sleep(30)  # never actually types anything

    async def evaluate(self, expression: str, arg=None):
        return None

    async def wait_for_timeout(self, timeout: float) -> None:
        return None

    async def goto(self, url: str, **kwargs) -> None:
        self.url = url

    async def title(self) -> str:
        return ""


class _StuckComposerDriver:
    def __init__(self) -> None:
        self.page = _StuckComposerPage()

    async def acquire(self) -> tuple[Optional[PageLike], Optional[str]]:
        # _StuckComposerPage is a minimal structural fake — cast rather than
        # chase pyright's invariant-field/self-referential-Protocol checks
        # for a test double never used as a real LocatorLike elsewhere.
        return cast(PageLike, self.page), "claude"


class TestBrowserTurnNeverHangsForever(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._env = patch.dict(os.environ)
        self._env.start()
        os.environ["KIM_BROWSER_TIMEOUT_CONNECT"] = "0.2"
        os.environ["KIM_BROWSER_TIMEOUT_SUBMIT"] = "0.2"
        for key in ("KIM_WEBVIEW_BRIDGE_URL", "KIM_WEBVIEW_BRIDGE_TOKEN", "KIM_PREFERRED_SITE"):
            os.environ.pop(key, None)

    def tearDown(self):
        self._env.stop()

    async def test_dead_connection_fails_fast_with_need_help(self):
        provider = BrowserProvider(
            {"project_root": ".", "browser_provider": {}}, page_driver=_HangingDriver()
        )
        start = time.monotonic()
        result = await provider.complete(messages=MESSAGES, tools=[], system="hi")
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 3.0, "connect-phase timeout should fail in ~0.2s, not hang")
        self.assertEqual(result["type"], "text")
        self.assertIn("NEED_HELP", result["content"])
        self.assertIn("connect", result["content"].lower())

    async def test_never_driven_composer_fails_fast_with_need_help(self):
        provider = BrowserProvider(
            {"project_root": ".", "browser_provider": {}},
            page_driver=_StuckComposerDriver(),
        )
        start = time.monotonic()
        result = await provider.complete(messages=MESSAGES, tools=[], system="hi")
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 3.0, "submit-phase timeout should fail in ~0.2s, not hang")
        self.assertEqual(result["type"], "text")
        self.assertIn("NEED_HELP", result["content"])
        self.assertIn("submit", result["content"].lower())


if __name__ == "__main__":
    unittest.main()

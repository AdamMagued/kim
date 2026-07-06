"""Tests for browser response detection and preferred-site tab selection.

Covers the fixes for "the browser responded but Kim didn't catch it":

  * _normalize_for_marker            — styled completion sentinels still match
  * _wait_for_generation_complete    — definitive hash exit; shrinking text
                                       (e.g. a collapsed thinking panel) must
                                       not freeze the idle counter
  * _wait_for_new_response           — responses streaming into an EXISTING
                                       element (reused thread) are detected
  * _find_chat_page                  — an explicitly selected site
                                       (browser:chatgpt) is authoritative: a
                                       focused tab of another site must not
                                       hijack the run, and a missing tab is
                                       opened rather than silently substituted
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from orchestrator.providers.browser import provider as bp
from orchestrator.providers.browser.provider import (
    BrowserProvider,
    _normalize_for_marker,
)


def _provider(**bp_cfg):
    return BrowserProvider({"project_root": ".", "browser_provider": bp_cfg})


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakePage:
    def __init__(self, url: str, focused: bool = False):
        self.url = url
        self._focused = focused

    async def evaluate(self, _script):
        return self._focused


def _browser_with(pages):
    ctx = SimpleNamespace(pages=list(pages))
    return SimpleNamespace(contexts=[ctx])


class _FakeElement:
    def __init__(self, texts):
        # texts: successive inner_text() results (loops on the last).
        self._texts = list(texts)
        self._i = 0

    async def inner_text(self):
        text = self._texts[min(self._i, len(self._texts) - 1)]
        self._i += 1
        return text


class _FakeLocator:
    def __init__(self, count: int, element: _FakeElement):
        self._count = count
        self._element = element

    async def count(self):
        return self._count

    def nth(self, _i):
        return self._element


# ---------------------------------------------------------------------------
# _normalize_for_marker
# ---------------------------------------------------------------------------

class TestNormalizeForMarker(unittest.TestCase):
    MARKER = "[END_OF_RESPONSE_a1b2c3]"

    def test_plain_marker_matches(self):
        self.assertIn(
            _normalize_for_marker(self.MARKER),
            _normalize_for_marker(f"answer text {self.MARKER}"),
        )

    def test_backticked_marker_matches(self):
        styled = "answer text `[END_OF_RESPONSE_a1b2c3]`"
        self.assertIn(
            _normalize_for_marker(self.MARKER), _normalize_for_marker(styled)
        )

    def test_italic_mangled_marker_matches(self):
        # Markdown renderers can eat the underscores entirely.
        styled = "answer text [END*OF*RESPONSE*a1b2c3]"
        self.assertIn(
            _normalize_for_marker(self.MARKER), _normalize_for_marker(styled)
        )

    def test_line_broken_marker_matches(self):
        styled = "answer text [END_OF_RESPONSE_\na1b2c3]"
        self.assertIn(
            _normalize_for_marker(self.MARKER), _normalize_for_marker(styled)
        )

    def test_different_id_does_not_match(self):
        self.assertNotIn(
            _normalize_for_marker(self.MARKER),
            _normalize_for_marker("answer [END_OF_RESPONSE_zzz]"),
        )


# ---------------------------------------------------------------------------
# _wait_for_generation_complete
# ---------------------------------------------------------------------------

class TestWaitForGenerationComplete(unittest.IsolatedAsyncioTestCase):
    async def _wait(self, provider, scrapes, completion_hash):
        scrape = AsyncMock(side_effect=scrapes)
        with patch.object(provider, "_scrape_last_response", scrape), \
             patch.object(bp, "GENERATION_WAIT_S", 3.0), \
             patch.object(bp.asyncio, "sleep", AsyncMock()):
            result = await provider._wait_for_generation_complete(
                MagicMock(),
                stop_selectors=[],
                response_selectors=["sel"],
                completion_hash=completion_hash,
                min_generation_s=0.0,
            )
        return result, scrape

    async def test_styled_hash_is_definitive(self):
        p = _provider()
        result, scrape = await self._wait(
            p,
            ["streaming…", "done `[END_OF_RESPONSE_abc]`"] + ["x"] * 50,
            "[END_OF_RESPONSE_abc]",
        )
        self.assertTrue(result)
        self.assertEqual(scrape.await_count, 2)

    async def test_shrinking_text_does_not_freeze_idle_counter(self):
        # A long "thinking" text collapses into a shorter final answer. The
        # idle counter must resync to the new (shorter) baseline and exit via
        # the settled-text heuristic — not spin until the global deadline.
        p = _provider()
        scrapes = ["thinking " * 40] * 2 + ["short final answer"] * 200
        result, scrape = await self._wait(p, scrapes, completion_hash=None)
        self.assertFalse(result)  # heuristic exit, not definitive
        # Fix bound: shrink-resync + 8 settled polls. Without the fix the loop
        # runs to the (patched) deadline in a busy loop — hundreds of scrapes.
        self.assertLess(scrape.await_count, 30)

    async def test_stable_text_without_hash_is_not_definitive(self):
        p = _provider()
        result, _ = await self._wait(
            p, ["the answer"] * 200, completion_hash=None
        )
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# _wait_for_new_response — reused-thread streaming
# ---------------------------------------------------------------------------

class TestWaitForNewResponse(unittest.IsolatedAsyncioTestCase):
    async def test_detects_streaming_into_existing_element(self):
        p = _provider()
        element = _FakeElement(["old reply", "old reply", "old reply + new tokens"])
        page = MagicMock()
        page.locator = MagicMock(return_value=_FakeLocator(1, element))
        with patch.object(bp, "RESPONSE_WAIT_S", 3.0), \
             patch.object(bp.asyncio, "sleep", AsyncMock()):
            started = await p._wait_for_new_response(page, "sel", initial_count=1)
        self.assertTrue(started)

    async def test_detects_new_element_by_count(self):
        p = _provider()
        element = _FakeElement(["irrelevant"])
        page = MagicMock()
        page.locator = MagicMock(return_value=_FakeLocator(2, element))
        with patch.object(bp, "RESPONSE_WAIT_S", 3.0), \
             patch.object(bp.asyncio, "sleep", AsyncMock()):
            started = await p._wait_for_new_response(page, "sel", initial_count=1)
        self.assertTrue(started)


# ---------------------------------------------------------------------------
# _find_chat_page — preferred site is authoritative
# ---------------------------------------------------------------------------

class TestFindChatPagePreferredSite(unittest.IsolatedAsyncioTestCase):
    async def test_focused_other_site_does_not_hijack_preferred(self):
        # User selected browser:chatgpt; only a FOCUSED Gemini tab is open.
        # Kim must open ChatGPT, not reuse the Gemini tab.
        p = _provider(preferred_site="chatgpt")
        gemini = _FakePage("https://gemini.google.com/app/abc", focused=True)
        new_tab = _FakePage("https://chatgpt.com/")
        with patch.object(
            p, "_open_preferred_site_tab", AsyncMock(return_value=new_tab)
        ) as opener:
            page, site = await p._find_chat_page(_browser_with([gemini]))
        self.assertEqual(site, "chatgpt")
        self.assertIs(page, new_tab)
        opener.assert_awaited_once()

    async def test_preferred_tab_beats_focused_other_site(self):
        p = _provider(preferred_site="chatgpt")
        gemini = _FakePage("https://gemini.google.com/app/abc", focused=True)
        chatgpt = _FakePage("https://chatgpt.com/c/123", focused=False)
        page, site = await p._find_chat_page(_browser_with([gemini, chatgpt]))
        self.assertEqual(site, "chatgpt")
        self.assertIs(page, chatgpt)

    async def test_mid_task_lost_tab_stays_need_help(self):
        # Mid-task (last chat site == preferred) with the tab gone: do NOT
        # silently open a fresh tab — that would drop the LLM context. The
        # caller's NEED_HELP path must win.
        p = _provider(preferred_site="chatgpt")
        p._last_chat_site = "chatgpt"
        p._last_chat_page_url = "https://chatgpt.com/c/123"
        gemini = _FakePage("https://gemini.google.com/app/abc", focused=True)
        with patch.object(
            p, "_open_preferred_site_tab", AsyncMock()
        ) as opener:
            page, site = await p._find_chat_page(_browser_with([gemini]))
        self.assertIsNone(page)
        self.assertIsNone(site)
        opener.assert_not_awaited()

    async def test_no_preferred_site_keeps_legacy_fallback(self):
        p = _provider()  # no preferred site
        gemini = _FakePage("https://gemini.google.com/app/abc", focused=False)
        page, site = await p._find_chat_page(_browser_with([gemini]))
        self.assertEqual(site, "gemini")
        self.assertIs(page, gemini)


class TestOpenPreferredSiteTab(unittest.IsolatedAsyncioTestCase):
    async def test_opens_launch_url_in_new_tab(self):
        p = _provider(preferred_site="chatgpt")
        goto = AsyncMock()
        new_page = SimpleNamespace(url="https://chatgpt.com/", goto=goto)
        ctx = SimpleNamespace(new_page=AsyncMock(return_value=new_page))
        browser = SimpleNamespace(contexts=[ctx])
        with patch.object(bp.asyncio, "sleep", AsyncMock()):
            page = await p._open_preferred_site_tab(browser)
        self.assertIs(page, new_page)
        goto.assert_awaited_once()
        assert goto.await_args is not None
        self.assertIn("chatgpt.com", goto.await_args.args[0])

    async def test_returns_none_when_tab_open_fails(self):
        p = _provider(preferred_site="chatgpt")
        ctx = SimpleNamespace(new_page=AsyncMock(side_effect=RuntimeError("boom")))
        browser = SimpleNamespace(contexts=[ctx])
        self.assertIsNone(await p._open_preferred_site_tab(browser))


if __name__ == "__main__":
    unittest.main()

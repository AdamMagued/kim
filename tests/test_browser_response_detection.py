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


class _FakeStopLocator:
    """Stop-button locator whose visibility follows a per-poll script."""

    def __init__(self, visibility):
        self._vis = list(visibility)
        self._i = 0

    async def count(self):
        return 1

    def nth(self, _i):
        return self

    async def is_visible(self):
        visible = self._vis[min(self._i, len(self._vis) - 1)]
        self._i += 1
        return visible


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
# _scrape_last_response markdown reconstruction (as_markdown=True)
# ---------------------------------------------------------------------------

class _MdElement:
    """Fake response element: evaluate() returns fenced markdown (or raises),
    inner_text() returns the flattened form the DOM would actually yield."""

    def __init__(self, *, markdown=None, flat="", raises=False):
        self._markdown = markdown
        self._flat = flat
        self._raises = raises

    async def evaluate(self, _script):
        if self._raises:
            raise RuntimeError("evaluate boom")
        return self._markdown

    async def inner_text(self):
        return self._flat

    async def text_content(self):
        return self._flat


class _MdScrapePage:
    def __init__(self, element):
        self._element = element

    def locator(self, _sel):
        el = self._element

        class _Loc:
            async def all(self):
                return [el]

        return _Loc()


class TestScrapeMarkdown(unittest.IsolatedAsyncioTestCase):
    async def test_markdown_path_restores_fences(self):
        p = _provider()
        # inner_text would drop the fence; evaluate reconstructs it.
        el = _MdElement(
            markdown="Run this:\n\n```bash\nopen pong.html\n```",
            flat="Run this:\nopen pong.html",
        )
        page = _MdScrapePage(el)
        text = await p._scrape_last_response(page, ["sel"], as_markdown=True)
        self.assertIn("```bash", text)
        self.assertIn("open pong.html", text)

    async def test_falls_back_to_inner_text_when_evaluate_raises(self):
        p = _provider()
        el = _MdElement(raises=True, flat="plain scraped text, no fences")
        page = _MdScrapePage(el)
        text = await p._scrape_last_response(page, ["sel"], as_markdown=True)
        self.assertEqual(text, "plain scraped text, no fences")

    async def test_empty_markdown_falls_back_to_inner_text(self):
        p = _provider()
        el = _MdElement(markdown="   ", flat="fallback body")
        page = _MdScrapePage(el)
        text = await p._scrape_last_response(page, ["sel"], as_markdown=True)
        self.assertEqual(text, "fallback body")

    async def test_default_path_never_calls_evaluate(self):
        p = _provider()
        el = _MdElement(raises=True, flat="plain body")
        page = _MdScrapePage(el)
        # as_markdown defaults False → must not touch evaluate (used by the
        # completion-polling path, which stays on plain inner_text).
        text = await p._scrape_last_response(page, ["sel"])
        self.assertEqual(text, "plain body")


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

    async def test_stop_transition_is_definitive_without_sentinel(self):
        # The stop control appeared during generation and then cleared — the
        # site itself signals completion. Must exit fast even though (a) the
        # sentinel never arrived (would otherwise cost the 20-idle penalty)
        # and (b) min_generation_s has not elapsed (mocked sleep keeps
        # loop.time() well under the 5s floor — only the transition path can
        # return this quickly).
        p = _provider()
        stop = _FakeStopLocator([True, True, False, False, False, False])
        page = MagicMock()
        page.locator = MagicMock(return_value=stop)
        scrape = AsyncMock(
            side_effect=["hel", "hello there!"] + ["hello there!"] * 50
        )
        with patch.object(p, "_scrape_last_response", scrape), \
             patch.object(bp, "GENERATION_WAIT_S", 3.0), \
             patch.object(bp.asyncio, "sleep", AsyncMock()):
            result = await p._wait_for_generation_complete(
                page,
                stop_selectors=["button.stop"],
                response_selectors=["sel"],
                completion_hash="[END_OF_RESPONSE_never_echoed]",
            )
        self.assertTrue(result)
        self.assertLess(scrape.await_count, 10)

    async def test_stop_never_seen_does_not_use_transition_exit(self):
        # If the stop control was never observed, the transition shortcut must
        # not fire — completion falls back to the sentinel/idle heuristics.
        p = _provider()
        stop = _FakeStopLocator([False])
        page = MagicMock()
        page.locator = MagicMock(return_value=stop)
        scrape = AsyncMock(side_effect=["the answer"] * 300)
        with patch.object(p, "_scrape_last_response", scrape), \
             patch.object(bp, "GENERATION_WAIT_S", 2.0), \
             patch.object(bp.asyncio, "sleep", AsyncMock()):
            result = await p._wait_for_generation_complete(
                page,
                stop_selectors=["button.stop"],
                response_selectors=["sel"],
                completion_hash=None,
                min_generation_s=0.0,
            )
        self.assertFalse(result)  # idle-heuristic exit, not the transition


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

"""Tests for browser runtime fixes: GUI effort wiring's gemini authuser URL
threading, per-selector scrape-index baselines, popup-label matching, and the
deepseek-R1 "still thinking" wait extension.

Split out of test_browser_response_detection.py (which stays anchored near
its historical size — see Q6 file-size gate in scripts/check_file_size_gate.py)
so this newer coverage gets its own home instead of pushing that file over
the gate's growth limit. No behavior change vs. where these tests used to
live; only the file they live in changed.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from orchestrator.providers.browser import provider as bp
from orchestrator.providers.browser.popup_matching import popup_label_matches
from orchestrator.providers.browser.provider import BrowserProvider


def _provider(**bp_cfg):
    # Isolate from any REAL ~/.../kim/account.json on the machine running the
    # tests (FIX 6 threads self._gemini_authuser, loaded from that file, into
    # gemini URLs — a dev machine with a signed-in Google account would
    # otherwise leak a real authuser index into these tests).
    with patch.dict("os.environ", {"KIM_ACCOUNT_PATH": "/nonexistent/kim-account-test-isolation.json"}):
        return BrowserProvider({"project_root": ".", "browser_provider": bp_cfg})


# ---------------------------------------------------------------------------
# Site-URL coverage for the deepseek-added / claude+grok-removed sites (Goal
# A of 219ffb2), kept alongside the authuser tests below since they all
# exercise _fresh_chat_url.
# ---------------------------------------------------------------------------

class TestFreshChatUrlSiteChanges(unittest.TestCase):
    def test_deepseek_fresh_url(self):
        p = _provider()
        self.assertEqual(p._fresh_chat_url("deepseek"), "https://chat.deepseek.com/")

    def test_claude_and_grok_are_no_longer_known_sites(self):
        # Goal A: browser:claude and browser:grok were removed entirely.
        p = _provider()
        self.assertIsNone(p._fresh_chat_url("claude"))
        self.assertIsNone(p._fresh_chat_url("grok"))


class TestGeminiAuthuserUrlBuilder(unittest.TestCase):
    """FIX 6: gemini authuser must be threaded into the CDP-path launch/
    fresh-chat URLs, not just the webview-bridge payload."""

    def test_fresh_chat_url_appends_authuser_when_set(self):
        p = _provider()
        p._gemini_authuser = 2
        self.assertEqual(p._fresh_chat_url("gemini"), "https://gemini.google.com/?authuser=2")

    def test_fresh_chat_url_unaffected_for_non_gemini_site(self):
        p = _provider(preferred_site="chatgpt")
        p._gemini_authuser = 2
        self.assertEqual(p._fresh_chat_url("chatgpt"), "https://chatgpt.com/")

    def test_no_authuser_leaves_url_unchanged(self):
        p = _provider()
        p._gemini_authuser = None
        self.assertEqual(p._fresh_chat_url("gemini"), "https://gemini.google.com/")

    def test_site_launch_url_appends_authuser_for_preferred_gemini(self):
        p = _provider(preferred_site="gemini")
        p._gemini_authuser = 1
        self.assertEqual(p._site_launch_url(), "https://gemini.google.com/?authuser=1")

    def test_apply_gemini_authuser_helper_handles_existing_query_string(self):
        p = _provider()
        p._gemini_authuser = 3
        # Defensive: if a caller ever passes a URL that already has a query
        # string, the helper must append with '&', not corrupt it with a
        # second '?'.
        out = p._apply_gemini_authuser("gemini", "https://gemini.google.com/app?x=1")
        self.assertEqual(out, "https://gemini.google.com/app?x=1&authuser=3")


# ---------------------------------------------------------------------------
# FIX 2 — _scrape_last_response min_index must be per-selector, not a single
# index computed from one selector applied to every other selector's array.
# ---------------------------------------------------------------------------

class _TextEl:
    def __init__(self, text: str):
        self._text = text

    async def inner_text(self):
        return self._text

    async def text_content(self):
        return self._text

    async def evaluate(self, _script):
        return None  # no markdown reconstruction in these fakes


class _MultiSelLocator:
    def __init__(self, elements):
        self._elements = list(elements)

    async def all(self):
        return list(self._elements)


class _MultiSelPage:
    def __init__(self, mapping: dict[str, list]):
        self._mapping = mapping

    def locator(self, sel: str):
        return _MultiSelLocator(self._mapping.get(sel, []))


class TestScrapeLastResponsePerSelectorBaseline(unittest.IsolatedAsyncioTestCase):
    async def test_selector_a_matches_at_send_time_selector_b_appears_later(self):
        # Selector A had ONE pre-existing element at send-time; after send a
        # SECOND element appears under A but is an empty placeholder (e.g. an
        # avatar-only wrapper an overly broad selector also matches) — never
        # the real answer. Selector B had ZERO elements at send-time; the
        # real new response actually rendered there.
        p = _provider()
        a_old = _TextEl("old A content (stale)")
        a_new_empty = _TextEl("")
        b_new = _TextEl("the real new response")
        page = _MultiSelPage({"A": [a_old, a_new_empty], "B": [b_new]})

        # FIX 2: per-selector baselines recorded BEFORE send (A had 1, B had 0).
        baselines = {"A": 1, "B": 0}
        text = await p._scrape_last_response(page, ["A", "B"], min_index=baselines)
        self.assertEqual(text, "the real new response")

    async def test_old_shared_int_baseline_would_pick_wrong_selector(self):
        # Regression guard: reproduces the pre-fix bug directly — a SINGLE
        # min_index computed from selector A's own count-delta (here: A grew
        # 1 -> 2, so the old code's shared index would be 1) applied to BOTH
        # selectors. Selector A's stale-but-nonempty tail element then wins
        # before selector B (whose real content sits at a DIFFERENT index) is
        # ever reached correctly.
        p = _provider()
        a_old = _TextEl("old A content (stale)")
        a_stale_tail = _TextEl("also stale A content")
        b_new = _TextEl("the real new response")
        page = _MultiSelPage({"A": [a_old, a_stale_tail], "B": [b_new]})

        shared_min_index = 1  # what the pre-fix code would have computed from A
        text = await p._scrape_last_response(page, ["A", "B"], min_index=shared_min_index)
        # This documents the OLD architecture bug's actual failure mode: with
        # a single shared index, selector A's (wrong, stale) tail element is
        # returned instead of B's real content. FIX 2 (per-selector dict, see
        # the test above) is what a caller must use to get the right answer.
        self.assertEqual(text, "also stale A content")
        self.assertNotEqual(text, "the real new response")

    async def test_backward_compatible_int_baseline_single_selector(self):
        # A plain int min_index (no dict) still works exactly as before when
        # only one selector is queried — existing callers/tests are unaffected.
        p = _provider()
        el = _TextEl("only reply")
        page = _MultiSelPage({"sel": [el]})
        text = await p._scrape_last_response(page, ["sel"], min_index=0)
        self.assertEqual(text, "only reply")


# ---------------------------------------------------------------------------
# FIX 5 — popup-label matcher (orchestrator.providers.browser.popup_matching):
# "Continue" must dismiss a standalone Continue button but never ChatGPT's
# "Continue generating".
# ---------------------------------------------------------------------------

class TestPopupLabelMatches(unittest.TestCase):
    def test_exact_continue_matches(self):
        self.assertTrue(popup_label_matches("Continue", "Continue"))

    def test_continue_generating_does_not_match(self):
        self.assertFalse(popup_label_matches("Continue generating", "Continue"))

    def test_case_and_whitespace_insensitive(self):
        self.assertTrue(popup_label_matches("  continue  ", "Continue"))
        self.assertTrue(popup_label_matches("CONTINUE", "Continue"))

    def test_unrelated_text_does_not_match(self):
        self.assertFalse(popup_label_matches("Cancel", "Continue"))

    def test_continues_is_not_a_whole_word_match(self):
        self.assertFalse(popup_label_matches("Continues", "Continue"))

    def test_empty_text_or_label_does_not_match(self):
        self.assertFalse(popup_label_matches("", "Continue"))
        self.assertFalse(popup_label_matches("Continue", ""))


class _FakePopupButton:
    def __init__(self, text: str):
        self._text = text
        self.clicked = False

    async def inner_text(self):
        return self._text

    async def wait_for(self, state="visible", timeout=0):
        return None

    async def click(self):
        self.clicked = True


class _FakePopupButtonLocator:
    def __init__(self, buttons: list):
        self._buttons = buttons

    async def count(self):
        return len(self._buttons)

    def nth(self, i):
        return self._buttons[i]


class _FakePopupPage:
    def __init__(self, buttons: list):
        self._buttons = buttons
        self.keyboard = SimpleNamespace(press=AsyncMock())

    def locator(self, sel: str):
        if sel == "button":
            return _FakePopupButtonLocator(self._buttons)
        return _FakePopupButtonLocator([])  # e.g. the generic dialog sweep


class TestDismissPopupsContinueMatcher(unittest.IsolatedAsyncioTestCase):
    async def test_continue_generating_not_clicked_bare_continue_is(self):
        p = _provider()
        continue_generating = _FakePopupButton("Continue generating")
        continue_btn = _FakePopupButton("Continue")
        page = _FakePopupPage([continue_generating, continue_btn])
        with patch.object(bp.asyncio, "sleep", AsyncMock()):
            await p._dismiss_popups(page)
        self.assertFalse(continue_generating.clicked)
        self.assertTrue(continue_btn.clicked)


# ---------------------------------------------------------------------------
# FIX 3 — deepseek R1 (or any slow reasoning model): a visible "still
# thinking" signal must extend the wait instead of an instant
# _DeliveredNoResponse once RESPONSE_WAIT_S elapses with no response element.
# ---------------------------------------------------------------------------

class _ThinkingProbeLocator:
    def __init__(self, visible: bool):
        self._visible = visible

    async def count(self):
        return 1

    def nth(self, _i):
        return self

    async def is_visible(self):
        return self._visible


class _EmptyProbeLocator:
    async def count(self):
        return 0


class TestModelStillThinking(unittest.IsolatedAsyncioTestCase):
    async def test_reasoning_panel_visible_is_detected(self):
        p = _provider()
        page = MagicMock()

        def locator(sel):
            if sel == "text=/Thought for/i":
                return _ThinkingProbeLocator(True)
            return _EmptyProbeLocator()

        page.locator = MagicMock(side_effect=locator)
        result = await p._model_still_thinking(page, {"stop_selectors": ["button.stop"]})
        self.assertTrue(result)

    async def test_stop_selector_visible_is_detected(self):
        p = _provider()
        page = MagicMock()

        def locator(sel):
            if sel == "button.stop":
                return _ThinkingProbeLocator(True)
            return _EmptyProbeLocator()

        page.locator = MagicMock(side_effect=locator)
        result = await p._model_still_thinking(page, {"stop_selectors": ["button.stop"]})
        self.assertTrue(result)

    async def test_nothing_visible_is_not_thinking(self):
        p = _provider()
        page = MagicMock()
        page.locator = MagicMock(return_value=_EmptyProbeLocator())
        result = await p._model_still_thinking(page, {"stop_selectors": ["button.stop"]})
        self.assertFalse(result)


class _ClickOnceLocator:
    """Minimal locator matching the `.first.click()` shape _submit() uses
    for the send-button selector."""

    @property
    def first(self):
        return self

    async def click(self):
        return None


class _DeepseekRespLocator:
    """Response-element locator whose count only goes from 0 -> 1 well past
    the (patched, short) RESPONSE_WAIT_S deadline — simulating DeepSeek R1's
    answer element mounting only after a long reasoning phase."""

    def __init__(self, mounts_after_polls: int):
        self._polls = 0
        self._mounts_after = mounts_after_polls
        self.count_value = 0

    async def count(self):
        self._polls += 1
        if self._polls > self._mounts_after:
            self.count_value = 1
        return self.count_value

    def nth(self, _i):
        return self

    async def inner_text(self):
        return "the answer"


class TestSendAndWaitExtendsForThinking(unittest.IsolatedAsyncioTestCase):
    async def test_reasoning_panel_visible_does_not_raise_delivered_no_response(self):
        p = _provider()
        cfg = {
            "input_selectors": ["#input"],
            "send_selectors": ["#send"],
            "stop_selectors": ["#stop"],
            "response_selectors": ["div.ds-markdown"],
        }
        resp_loc = _DeepseekRespLocator(mounts_after_polls=3)

        page = MagicMock()
        page.keyboard = SimpleNamespace(press=AsyncMock())

        def locator(sel):
            if sel == "div.ds-markdown":
                return resp_loc
            if sel == "#send":
                return _ClickOnceLocator()
            return _EmptyProbeLocator()

        page.locator = MagicMock(side_effect=locator)

        # _find_selector drives response_sel / input_sel / send_sel lookups
        # (call 1-3) and _model_still_thinking's probe (call 4+): keep it
        # simple and deterministic by mocking it directly — the selector-
        # matching semantics of _model_still_thinking itself are covered by
        # TestModelStillThinking above.
        p._find_selector = AsyncMock(
            side_effect=["div.ds-markdown", "#input", "#send", "text=/Thought for/i"]
        )
        p._inject_text = AsyncMock()
        p._verify_injection = AsyncMock(return_value=True)
        p._wait_for_generation_complete = AsyncMock(return_value=True)
        p._scrape_last_response = AsyncMock(return_value="the answer")

        with patch.object(bp, "RESPONSE_WAIT_S", 0.05), \
             patch.object(bp, "GENERATION_WAIT_S", 5.0), \
             patch.object(bp.asyncio, "sleep", AsyncMock()):
            text = await p._send_and_wait(page, cfg, "prompt", "deepseek", "[END_OF_RESPONSE_x]")

        self.assertEqual(text, "the answer")


if __name__ == "__main__":
    unittest.main()

"""
Regression tests for the browser/web fix wave (branch fix/browser-web).

Covers:
  4.2  prompt-trim must never truncate the transport-marker instruction
       (truncation => the model never emits [END_OF_RESPONSE_xxxx] => the
       generation wait polls for the full ~20-min timeout every turn).
  3.2  web_open HTTP-Basic auth route is scoped to the EXACT requested URL
       (no same-origin credential replay), and unroute failures are surfaced
       instead of swallowed.
  3.3  Chrome launches are single-flighted — no second Popen on the same
       --user-data-dir while a launch is plausibly in flight.
  3.6  web/ element state exposes reset_element_state() for run boundaries.
  7.2/7.3 screenshot honesty — the bridge's real attachments_uploaded signal
       gates the "image attached" claim; a failed CDP clipboard paste returns
       False so the caller can amend the prompt.
"""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

from mcp_server.tools.web import browser as web_browser
from mcp_server.tools.web import navigation as web_navigation
from mcp_server.tools.web import observation as web_observation
from orchestrator.providers.browser.bridge_client import (
    _IMAGE_NOT_DELIVERED_NOTE,
    _apply_attachment_signal,
)
from orchestrator.providers.browser.prompt_builder import (
    format_prompt,
    transport_marker_instruction,
)


# ---------------------------------------------------------------------------
# 4.2 — prompt trim must preserve the transport-marker instruction
# ---------------------------------------------------------------------------

class TestPromptTrimKeepsTransportMarker(unittest.TestCase):
    def _fmt(self, system: str, max_inject_chars: int, sent: bool = False):
        messages = [{"role": "user", "content": "do the thing " * 50}]
        return format_prompt(
            messages,
            [],
            system,
            sent_system_prompt=sent,
            max_inject_chars=max_inject_chars,
            use_webview_bridge=False,
        )

    def test_oversized_system_block_keeps_marker_instruction(self):
        # [SYSTEM] block alone far exceeds half the budget — the old trim
        # capped the head at max_inject_chars // 2 and cut the marker
        # instruction out of the prompt entirely.
        system = "RULE: " + ("x" * 20000)
        prompt, _atts, completion_hash, _sent = self._fmt(system, 4000)
        self.assertLessEqual(len(prompt), 4000 + 200)  # small slack only
        self.assertIn(
            transport_marker_instruction(completion_hash),
            prompt,
            "transport-marker instruction must survive trimming (4.2)",
        )
        self.assertIn(completion_hash, prompt)

    def test_trim_notice_present_when_trimmed(self):
        system = "RULE: " + ("x" * 20000)
        prompt, _atts, _h, _sent = self._fmt(system, 4000)
        self.assertIn("earlier context trimmed", prompt)

    def test_untrimmed_prompt_unchanged(self):
        prompt, _atts, completion_hash, _sent = self._fmt("be nice", 200000)
        self.assertIn(transport_marker_instruction(completion_hash), prompt)

    def test_subsequent_turn_trim_keeps_marker(self):
        # Non-first turn: marker is appended at the end; a trim must keep it.
        messages = [{"role": "user", "content": "y" * 30000}]
        prompt, _atts, completion_hash, _sent = format_prompt(
            messages, [], "sys",
            sent_system_prompt=True,
            max_inject_chars=3000,
            use_webview_bridge=False,
        )
        self.assertIn(transport_marker_instruction(completion_hash), prompt)


# ---------------------------------------------------------------------------
# 3.2 — auth route exact-URL scoping + surfaced unroute failure
# ---------------------------------------------------------------------------

class _FakePage:
    """Minimal Playwright-page stand-in for handle_web_open."""

    def __init__(self, unroute_raises=False, unroute_all_raises=False,
                 has_unroute_all=True):
        self.url = "https://example.com/secret"
        self.routes: list[tuple] = []
        self.unrouted: list[tuple] = []
        self.unroute_all_called = 0
        self._unroute_raises = unroute_raises
        self._unroute_all_raises = unroute_all_raises
        if not has_unroute_all:
            self.unroute_all = None  # attribute exists but not callable

    async def route(self, matcher, handler):
        self.routes.append((matcher, handler))

    async def unroute(self, matcher, handler):
        if self._unroute_raises:
            raise RuntimeError("unroute blew up")
        self.unrouted.append((matcher, handler))

    async def unroute_all(self):
        self.unroute_all_called += 1
        if self._unroute_all_raises:
            raise RuntimeError("unroute_all blew up")

    async def goto(self, url, wait_until=None, timeout=None):
        self.url = url

    async def title(self):
        return "Example"


class TestAuthRouteScoping(unittest.TestCase):
    URL = "https://example.com/secret"

    def _open(self, page):
        async def fake_page():
            return page
        with mock.patch.object(web_navigation.browser, "_page", fake_page):
            return asyncio.run(
                web_navigation.handle_web_open(
                    {"url": self.URL, "username": "u", "password": "p"}
                )
            )

    def test_route_matcher_is_exact_url_scoped(self):
        page = _FakePage()
        result = self._open(page)
        self.assertTrue(result.startswith("Opened:"), result)
        self.assertEqual(len(page.routes), 1)
        matcher = page.routes[0][0]
        self.assertTrue(callable(matcher), "route must use a callable matcher, not an origin glob")
        # Exact URL matches (incl. default-port normalization).
        self.assertTrue(matcher("https://example.com/secret"))
        self.assertTrue(matcher("https://example.com:443/secret"))
        # Same-origin but different path/query — must NOT get the credentials.
        self.assertFalse(matcher("https://example.com/other"))
        self.assertFalse(matcher("https://example.com/secret?x=1"))
        self.assertFalse(matcher("https://example.com/"))
        # Other origin / scheme — must NOT get the credentials.
        self.assertFalse(matcher("https://evil.com/secret"))
        self.assertFalse(matcher("http://example.com/secret"))

    def test_route_is_unrouted_after_navigation(self):
        page = _FakePage()
        self._open(page)
        self.assertEqual(len(page.unrouted), 1)

    def test_unroute_failure_is_surfaced_when_unrecoverable(self):
        page = _FakePage(unroute_raises=True, unroute_all_raises=True)
        result = self._open(page)
        self.assertIn("WARNING", result)
        self.assertIn("HTTP-auth", result)

    def test_unroute_failure_recovered_by_unroute_all(self):
        page = _FakePage(unroute_raises=True)
        result = self._open(page)
        self.assertEqual(page.unroute_all_called, 1)
        self.assertNotIn("WARNING", result)

    def test_no_credentials_installs_no_route(self):
        page = _FakePage()

        async def fake_page():
            return page
        with mock.patch.object(web_navigation.browser, "_page", fake_page):
            asyncio.run(web_navigation.handle_web_open({"url": self.URL}))
        self.assertEqual(page.routes, [])


# ---------------------------------------------------------------------------
# 3.3 — single-flight Chrome launch
# ---------------------------------------------------------------------------

class TestSingleFlightLaunch(unittest.TestCase):
    def setUp(self):
        web_browser._last_launch_at.clear()
        web_browser._dedicated_launch_proc = None

    tearDown = setUp

    def test_launch_blocked_when_port_listening(self):
        with mock.patch.object(web_browser, "_port_listening", return_value=True):
            self.assertFalse(web_browser._launch_allowed(9333))

    def test_launch_blocked_while_tracked_proc_alive(self):
        proc = mock.Mock()
        proc.poll.return_value = None  # still running
        web_browser._dedicated_launch_proc = proc
        with mock.patch.object(web_browser, "_port_listening", return_value=False):
            self.assertFalse(web_browser._launch_allowed(web_browser._DEDICATED_CDP_PORT))

    def test_launch_blocked_within_cooldown(self):
        web_browser._last_launch_at[9333] = time.monotonic()
        with mock.patch.object(web_browser, "_port_listening", return_value=False):
            self.assertFalse(web_browser._launch_allowed(9333))

    def test_launch_allowed_after_cooldown_and_dead_proc(self):
        proc = mock.Mock()
        proc.poll.return_value = 0  # exited
        web_browser._dedicated_launch_proc = proc
        web_browser._last_launch_at[web_browser._DEDICATED_CDP_PORT] = (
            time.monotonic() - web_browser._LAUNCH_COOLDOWN_S - 1
        )
        with mock.patch.object(web_browser, "_port_listening", return_value=False):
            self.assertTrue(web_browser._launch_allowed(web_browser._DEDICATED_CDP_PORT))

    def test_dedicated_launch_skips_popen_when_in_flight(self):
        with mock.patch.object(web_browser, "_browser_executable", return_value="/usr/bin/chrome"), \
             mock.patch.object(web_browser, "_launch_allowed", return_value=False), \
             mock.patch.object(web_browser.subprocess, "Popen") as popen:
            ok = asyncio.run(web_browser._launch_dedicated_browser())
        self.assertTrue(ok)
        popen.assert_not_called()

    def test_dedicated_launch_tracks_popen_handle(self):
        proc = mock.Mock()
        with mock.patch.object(web_browser, "_browser_executable", return_value="/usr/bin/chrome"), \
             mock.patch.object(web_browser, "_launch_allowed", return_value=True), \
             mock.patch.object(web_browser.subprocess, "Popen", return_value=proc) as popen:
            ok = asyncio.run(web_browser._launch_dedicated_browser())
        self.assertTrue(ok)
        popen.assert_called_once()
        self.assertIs(web_browser._dedicated_launch_proc, proc)
        # Cooldown timestamp recorded for this port.
        self.assertIn(web_browser._DEDICATED_CDP_PORT, web_browser._last_launch_at)

    def test_real_launch_skips_open_when_in_flight(self):
        with mock.patch.object(web_browser, "_launch_allowed", return_value=False), \
             mock.patch.object(web_browser.subprocess, "Popen") as popen:
            ok = asyncio.run(web_browser._launch_real_browser())
        self.assertTrue(ok)
        popen.assert_not_called()


# ---------------------------------------------------------------------------
# 3.6 — element-state reset hook
# ---------------------------------------------------------------------------

class TestResetElementState(unittest.TestCase):
    def test_reset_clears_all_observation_state(self):
        web_observation._element_map["e1"] = "#sel"
        web_observation._element_data_map["e1"] = {"id": "e1"}
        web_observation._last_observation = {"url": "https://x"}
        web_observation._last_form_diagnostics = {"fields": [1]}
        web_observation._observe_generation = 7

        web_observation.reset_element_state()

        self.assertEqual(web_observation._element_map, {})
        self.assertEqual(web_observation._element_data_map, {})
        self.assertIsNone(web_observation._last_observation)
        self.assertEqual(web_observation._last_form_diagnostics, {})
        self.assertEqual(web_observation._observe_generation, 0)


# ---------------------------------------------------------------------------
# 7.2/7.3 — screenshot honesty: attachments_uploaded gating (bridge path)
# ---------------------------------------------------------------------------

class TestAttachmentSignalGating(unittest.TestCase):
    IMG = [{"name": "s.png", "mime_type": "image/png", "data_base64": "aa=="}]

    def test_absent_signal_leaves_result_unchanged(self):
        result = {"type": "text", "content": "hello"}
        out = _apply_attachment_signal(dict(result), {}, self.IMG)
        self.assertEqual(out, result)

    def test_zero_uploaded_with_image_appends_disclaimer(self):
        result = {"type": "text", "content": "TASK_COMPLETE: I see your screen"}
        out = _apply_attachment_signal(result, {"attachments_uploaded": 0}, self.IMG)
        self.assertEqual(out["attachments_uploaded"], 0)
        self.assertTrue(out["content"].endswith(_IMAGE_NOT_DELIVERED_NOTE))
        # The disclaimer is APPENDED so the agent's TASK_COMPLETE regex
        # (\bTASK_COMPLETE:\s*(.+)\Z, DOTALL) carries it into the
        # user-visible summary.
        self.assertIn("TASK_COMPLETE:", out["content"])

    def test_uploaded_image_gets_no_disclaimer(self):
        result = {"type": "text", "content": "I see your screen"}
        out = _apply_attachment_signal(result, {"attachments_uploaded": 1}, self.IMG)
        self.assertEqual(out["attachments_uploaded"], 1)
        self.assertNotIn(_IMAGE_NOT_DELIVERED_NOTE, out["content"])

    def test_non_image_attachments_get_no_disclaimer(self):
        atts = [{"name": "a.pdf", "mime_type": "application/pdf", "data_base64": "aa=="}]
        result = {"type": "text", "content": "done"}
        out = _apply_attachment_signal(result, {"attachments_uploaded": 0}, atts)
        self.assertNotIn(_IMAGE_NOT_DELIVERED_NOTE, out["content"])

    def test_tool_use_result_gets_signal_but_no_content_edit(self):
        result = {"type": "tool_use", "name": "web_open", "input": {}}
        out = _apply_attachment_signal(result, {"attachments_uploaded": 0}, self.IMG)
        self.assertEqual(out["attachments_uploaded"], 0)
        self.assertNotIn("content", out)

    def test_garbage_signal_is_ignored(self):
        result = {"type": "text", "content": "x"}
        out = _apply_attachment_signal(result, {"attachments_uploaded": "wat"}, self.IMG)
        self.assertNotIn("attachments_uploaded", out)


# ---------------------------------------------------------------------------
# 7.2/7.3 — screenshot honesty: CDP clipboard paste reports success
# ---------------------------------------------------------------------------

class _FakeKeyboard:
    async def press(self, key):
        pass


class _FakeChatPage:
    def __init__(self, evaluate_raises=False):
        self.keyboard = _FakeKeyboard()
        self._evaluate_raises = evaluate_raises

    async def click(self, sel):
        pass

    async def evaluate(self, js, arg=None):
        if self._evaluate_raises:
            raise RuntimeError("clipboard blocked")
        return None


class TestInjectImageClipboardReturnsSuccess(unittest.TestCase):
    def _provider(self):
        from orchestrator.providers.browser.provider import BrowserProvider
        return BrowserProvider.__new__(BrowserProvider)

    def test_returns_false_when_editor_missing(self):
        prov = self._provider()

        async def find_none(page, selectors):
            return None
        prov._find_selector = find_none  # type: ignore[method-assign]
        ok = asyncio.run(
            prov._inject_image_clipboard(_FakeChatPage(), {"input_selectors": []}, "aa==")
        )
        self.assertFalse(ok)

    def test_returns_false_when_clipboard_write_fails(self):
        prov = self._provider()

        async def find_sel(page, selectors):
            return "#editor"
        prov._find_selector = find_sel  # type: ignore[method-assign]
        ok = asyncio.run(
            prov._inject_image_clipboard(
                _FakeChatPage(evaluate_raises=True), {"input_selectors": ["#editor"]}, "aa=="
            )
        )
        self.assertFalse(ok)

    def test_returns_true_on_successful_paste(self):
        prov = self._provider()

        async def find_sel(page, selectors):
            return "#editor"
        prov._find_selector = find_sel  # type: ignore[method-assign]
        ok = asyncio.run(
            prov._inject_image_clipboard(
                _FakeChatPage(), {"input_selectors": ["#editor"]}, "aa=="
            )
        )
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()

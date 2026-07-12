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
from typing import Any
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

    def test_tiny_budget_never_discards_user_task(self):
        task = "UNIQUE_USER_TASK_73a9"
        prompt, _atts, completion_hash, _sent = format_prompt(
            [{"role": "user", "content": task}], [], "x" * 5000,
            sent_system_prompt=False,
            max_inject_chars=32,
            use_webview_bridge=False,
        )
        self.assertIn(task, prompt)
        self.assertIn(transport_marker_instruction(completion_hash), prompt)

    def test_tiny_budget_preserves_complete_multiline_task(self):
        task = "FIRST_REQUIRED_STEP\nSECOND_REQUIRED_STEP\nFINAL_REQUIRED_STEP"
        prompt, _atts, completion_hash, _sent = format_prompt(
            [{"role": "user", "content": task}], [], "x" * 5000,
            sent_system_prompt=False,
            max_inject_chars=32,
            use_webview_bridge=False,
        )
        self.assertIn(task, prompt)
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
            self.unroute_all = None  # type: ignore[assignment]

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

    def test_unroute_all_recovery_reinstalls_ssrf_guard(self):
        # unroute_all() strips EVERY page.route() handler, including the
        # persistent SSRF navigation guard installed once per page by
        # _ensure_browser — not just the stuck auth route. Without
        # reinstating it, this recovery path would silently reopen #1 for
        # the rest of the page's lifetime.
        page = _FakePage(unroute_raises=True)
        self._open(page)
        self.assertEqual(page.unroute_all_called, 1)
        # After recovery, the LAST route registered on the page must be the
        # SSRF guard ("**/*"), not just the (now-cleared) auth route.
        self.assertTrue(page.routes, "expected at least one route re-registered")
        last_pattern, last_handler = page.routes[-1]
        self.assertEqual(last_pattern, "**/*")
        self.assertTrue(asyncio.iscoroutinefunction(last_handler))

    def test_no_credentials_installs_no_route(self):
        page = _FakePage()

        async def fake_page():
            return page
        with mock.patch.object(web_navigation.browser, "_page", fake_page):
            asyncio.run(web_navigation.handle_web_open({"url": self.URL}))
        self.assertEqual(page.routes, [])


# ---------------------------------------------------------------------------
# H1 (#1) — SSRF guard hooked into every navigation, not just the initial
# web_open call. Simulates what a redirect or web_click-driven navigation
# looks like at the Playwright request-interception layer.
# ---------------------------------------------------------------------------

class _FakeFrame:
    """Distinguishable sentinel: identity, not structure, is what matters."""


class _FakeRequest:
    def __init__(self, url: str, frame, is_navigation: bool = True):
        self.url = url
        self.frame = frame
        self._is_navigation = is_navigation

    def is_navigation_request(self) -> bool:
        return self._is_navigation


class _FakeRoute:
    def __init__(self):
        self.aborted = False
        self.abort_reason = None
        self.continued = False

    async def abort(self, reason=None):
        self.aborted = True
        self.abort_reason = reason

    async def continue_(self):
        self.continued = True


class _FakeGuardPage:
    """Minimal Playwright-page stand-in for _install_ssrf_guard."""

    def __init__(self):
        self.main_frame = _FakeFrame()
        self._route_handler: Any = None
        self.route_pattern = None

    async def route(self, pattern, handler):
        self.route_pattern = pattern
        self._route_handler = handler

    async def fire(self, request: _FakeRequest) -> _FakeRoute:
        """Simulate Playwright invoking the installed handler for *request*."""
        route = _FakeRoute()
        await self._route_handler(route, request)
        return route


class TestSsrfNavigationGuard(unittest.TestCase):
    """browser._install_ssrf_guard must catch redirects and click-driven
    navigation, not just the one-shot check web_open makes before goto()."""

    def _install(self) -> _FakeGuardPage:
        page = _FakeGuardPage()
        asyncio.run(web_browser._install_ssrf_guard(page))
        self.assertIsNotNone(page._route_handler, "guard must register a route handler")
        self.assertEqual(page.route_pattern, "**/*")
        return page

    def test_blocks_redirect_to_private_ip(self):
        # This is the scenario the one-time pre-goto check could never catch:
        # the initial URL (https://example.com) is public, but the server
        # 302s the SAME navigation to a link-local metadata endpoint.
        page = self._install()
        req = _FakeRequest("http://169.254.169.254/latest/meta-data/", page.main_frame)
        route = asyncio.run(page.fire(req))
        self.assertTrue(route.aborted, "redirect to a link-local address must be aborted")
        self.assertFalse(route.continued)

    def test_blocks_redirect_to_loopback(self):
        page = self._install()
        req = _FakeRequest("http://127.0.0.1:6379/", page.main_frame)
        route = asyncio.run(page.fire(req))
        self.assertTrue(route.aborted)

    def test_blocks_click_driven_navigation_to_private_ip(self):
        # web_click never called _is_ssrf_target at all — the guard is the
        # only thing that can catch a same-page link to an internal host.
        page = self._install()
        req = _FakeRequest("http://10.0.0.5/admin", page.main_frame)
        route = asyncio.run(page.fire(req))
        self.assertTrue(route.aborted)

    def test_allows_navigation_to_public_url(self):
        page = self._install()
        req = _FakeRequest("https://example.com/page", page.main_frame)
        route = asyncio.run(page.fire(req))
        self.assertFalse(route.aborted)
        self.assertTrue(route.continued)

    def test_blocks_subresource_to_metadata_endpoint(self):
        # F-C-4: the whole point — a public page's own JS `fetch`/`XHR`/`Image`
        # to the cloud-metadata endpoint is NOT a navigation request, so the old
        # nav-only guard let it through (IMDS-credential exfiltration). It must
        # now be aborted like any other internal-host request.
        page = self._install()
        req = _FakeRequest(
            "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
            page.main_frame,
            is_navigation=False,
        )
        route = asyncio.run(page.fire(req))
        self.assertTrue(route.aborted, "subresource to metadata endpoint must be aborted")
        self.assertFalse(route.continued)

    def test_blocks_subresource_to_loopback(self):
        # F-C-4: XHR/fetch to a loopback service (e.g. a local admin panel /
        # redis) from an attacker page must be aborted.
        page = self._install()
        req = _FakeRequest("http://127.0.0.1:6379/", page.main_frame, is_navigation=False)
        route = asyncio.run(page.fire(req))
        self.assertTrue(route.aborted)
        self.assertFalse(route.continued)

    def test_allows_subresource_to_public_host(self):
        # Ordinary page traffic to a public host must NOT be stalled/aborted.
        page = self._install()
        req = _FakeRequest(
            "https://cdn.example.com/app.js", page.main_frame, is_navigation=False
        )
        route = asyncio.run(page.fire(req))
        self.assertFalse(route.aborted)
        self.assertTrue(route.continued)

    def test_blocks_subframe_navigation_to_private_ip(self):
        # F-C-4: an <iframe> loading an internal host is SSRF too; the guard no
        # longer narrows to the main frame, so a subframe request to a private
        # address is aborted.
        page = self._install()
        other_frame = _FakeFrame()
        req = _FakeRequest("http://10.0.0.5/admin", other_frame, is_navigation=True)
        route = asyncio.run(page.fire(req))
        self.assertTrue(route.aborted)
        self.assertFalse(route.continued)

    def test_ensure_browser_installs_guard_on_newly_adopted_page(self):
        # The guard must be wired up automatically when a page is adopted —
        # nothing web_open-specific should be required to get protection.
        fake_page = mock.AsyncMock()
        fake_page.evaluate = mock.AsyncMock(return_value="1")
        fake_ctx = mock.Mock()
        fake_ctx.pages = []
        fake_ctx.new_page = mock.AsyncMock(return_value=fake_page)

        web_browser._active_page = None
        web_browser._browser_ctx = object()  # non-None so _connect_browser_ctx is skipped
        web_browser._playwright = mock.Mock()
        try:
            with mock.patch.object(
                web_browser, "_active_browser_context", mock.AsyncMock(return_value=fake_ctx)
            ), mock.patch.object(
                web_browser, "_install_ssrf_guard", mock.AsyncMock()
            ) as install_guard:
                asyncio.run(web_browser._ensure_browser())
            install_guard.assert_awaited_once_with(fake_page)
        finally:
            web_browser._active_page = None
            web_browser._browser_ctx = None
            web_browser._playwright = None


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

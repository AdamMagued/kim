"""Rb3 — auth-wall detection: a sign-in / Cloudflare tab fails fast with an
actionable AUTH_REQUIRED instead of hanging for the 600s generation wait.

Also covers Rb1's repair-visibility counters (proxy prose→tool-call salvage
and format nudges are counted into the thread-state sidecar).
"""

from __future__ import annotations

import asyncio
import unittest

from orchestrator.providers.browser.site_configs import detect_auth_wall


class DetectAuthWallTest(unittest.TestCase):
    def test_login_urls_detected(self):
        for url in (
            "https://chatgpt.com/auth/login",
            "https://claude.ai/login?returnTo=/new",
            "https://accounts.google.com/v3/signin/identifier?x=1",
            "https://auth.openai.com/authorize",
            "https://gemini.google.com/signin",
            "https://x.com/i/grok/sign-in",
        ):
            self.assertIsNotNone(detect_auth_wall(url), url)

    def test_cloudflare_challenges_detected(self):
        self.assertIsNotNone(detect_auth_wall("https://challenges.cloudflare.com/turnstile"))
        self.assertIsNotNone(detect_auth_wall("https://claude.ai/?__cf_chl_tk=abc"))
        self.assertIsNotNone(detect_auth_wall("https://claude.ai/cdn-cgi/challenge-platform/x"))

    def test_interstitial_titles_detected(self):
        self.assertIsNotNone(detect_auth_wall("https://claude.ai/new", "Just a moment..."))
        self.assertIsNotNone(detect_auth_wall("https://chatgpt.com/", "Sign in to ChatGPT"))
        self.assertIsNotNone(detect_auth_wall("https://claude.ai/", "Attention Required!"))

    def test_healthy_chat_urls_pass(self):
        for url in (
            "https://gemini.google.com/app/12ab34",
            "https://chatgpt.com/c/abc-123",
            "https://claude.ai/chat/uuid-1",
            "https://chat.deepseek.com/a/chat",
            "",
        ):
            self.assertIsNone(detect_auth_wall(url), url)
        # Titles of healthy chats don't trip the title markers.
        self.assertIsNone(detect_auth_wall("https://claude.ai/chat/x", "Claude"))


class _WallPage:
    """Minimal PageLike: only `url` is touched before the wall check fires."""

    def __init__(self, url: str) -> None:
        self.url = url


class _WallDriver:
    def __init__(self, url: str) -> None:
        self._page = _WallPage(url)

    async def acquire(self):
        return self._page, "chatgpt"


class AuthWallThroughProviderTest(unittest.TestCase):
    def test_complete_fails_fast_with_auth_required(self):
        from orchestrator.providers.browser_provider import BrowserProvider

        provider = BrowserProvider(
            {"project_root": ".", "browser_provider": {}},
            page_driver=_WallDriver("https://chatgpt.com/auth/login"),
        )
        result = asyncio.run(provider.complete(
            messages=[{"role": "user", "content": "hi"}], tools=[], system="s",
        ))
        content = str(result.get("content", ""))
        self.assertIn("NEED_HELP: AUTH_REQUIRED", content)
        self.assertIn("sign", content.lower())


class _FakePage:
    """PageLike with url + async title() + a goto that can simulate a redirect."""

    def __init__(self, url: str, title: str = "", goto_url: str | None = None) -> None:
        self.url = url
        self._title = title
        self._goto_url = goto_url

    async def title(self) -> str:
        return self._title

    async def goto(self, url: str, **_kwargs) -> None:
        self.url = self._goto_url if self._goto_url is not None else url


class _PageDriver:
    def __init__(self, page, site: str = "chatgpt") -> None:
        self._page, self._site = page, site

    async def acquire(self):
        return self._page, self._site


class AuthWallTitleAndRecheckTest(unittest.TestCase):
    """F-B-9: title markers are reachable, and the wall is re-checked after the
    clear_chat navigation."""

    def _provider(self, driver):
        from orchestrator.providers.browser_provider import BrowserProvider
        return BrowserProvider({"project_root": ".", "browser_provider": {}}, page_driver=driver)

    def test_title_interstitial_detected_through_provider(self):
        # Healthy URL but a Cloudflare "Just a moment..." title — previously
        # dead code (the title arg was never passed at the call site).
        page = _FakePage("https://chatgpt.com/", title="Just a moment...")
        provider = self._provider(_PageDriver(page))
        result = asyncio.run(provider.complete(
            messages=[{"role": "user", "content": "hi"}], tools=[], system="s",
        ))
        self.assertIn("AUTH_REQUIRED", str(result.get("content", "")))

    def test_clear_chat_redirect_to_login_is_rechecked(self):
        from unittest.mock import AsyncMock, patch
        from orchestrator.providers.browser import provider as bp
        # Healthy at first; the fresh-chat nav redirects a signed-out user to login.
        page = _FakePage(
            "https://chatgpt.com/c/abc", title="",
            goto_url="https://chatgpt.com/auth/login",
        )
        provider = self._provider(_PageDriver(page))
        with patch.object(bp.asyncio, "sleep", AsyncMock()):
            result = asyncio.run(provider.complete(
                messages=[{"role": "user", "content": "hi"}], tools=[], system="s",
                clear_chat=True,
            ))
        self.assertIn("AUTH_REQUIRED", str(result.get("content", "")))


class RepairMetricsTest(unittest.TestCase):
    def test_salvage_counts_into_metrics(self):
        from codex_engine.engine import _provider_response_to_responses_api

        metrics: dict = {}
        reply = {"type": "text", "content": "Here you go:\n```bash\ntouch pong.html\n```"}
        result = _provider_response_to_responses_api(
            reply, 1, request_tools=[{"name": "exec_command",
                                      "parameters": {"required": ["cmd"]}}],
            metrics=metrics,
        )
        self.assertTrue(any(item.get("type") == "function_call"
                            for item in result["output"]))
        self.assertEqual(metrics, {"salvages": 1})

    def test_clean_contract_reply_counts_nothing(self):
        from codex_engine.engine import _provider_response_to_responses_api

        metrics: dict = {}
        reply = {"type": "text", "content": '{"text": "all done"}'}
        _provider_response_to_responses_api(reply, 1, metrics=metrics)
        self.assertEqual(metrics, {})

    def test_nudge_counts_into_thread_state(self):
        from codex_engine.engine import _CodexProxy

        class RefusingProvider:
            async def complete(self, **kwargs):
                return {"type": "text", "content": "I cannot assist with that request."}

        state: dict = {}
        proxy = _CodexProxy(RefusingProvider(), provider_name="browser:gemini",
                            thread_state=state)
        original = {"type": "text", "content": "I am unable to run commands here."}
        result = asyncio.run(proxy._nudge_contract_retry(original, 1))
        self.assertEqual(result, original)  # retry failed too — original kept
        self.assertEqual(state["repairs"], {"nudges": 1})
        self.assertTrue(state.get("burned"))


if __name__ == "__main__":
    unittest.main()

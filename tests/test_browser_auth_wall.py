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

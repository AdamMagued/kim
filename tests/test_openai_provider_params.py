"""OpenAI provider: token-param selection (F-INH-2), remote-no-key warning
(F-B-12), and malformed tool-args re-emit signal (F-INH-3)."""
from __future__ import annotations

import asyncio
import types

import openai
import pytest

from orchestrator.providers.openai_provider import (
    OpenAIProvider,
    _default_token_param,
    _is_max_tokens_param_error,
)


# ── F-INH-2: token-param selection ──────────────────────────────────────────

@pytest.mark.parametrize("model,expected", [
    ("gpt-4o", "max_tokens"),
    ("gpt-4o-mini", "max_tokens"),
    ("o1-preview", "max_completion_tokens"),
    ("o3-mini", "max_completion_tokens"),
    ("o4-mini", "max_completion_tokens"),
    ("gpt-5", "max_completion_tokens"),
    ("gpt-5.1", "max_completion_tokens"),
    ("openai/o3-mini", "max_completion_tokens"),  # proxy-prefixed
])
def test_default_token_param(model, expected):
    assert _default_token_param(model) == expected


def test_is_max_tokens_param_error_detects_openai_wording():
    assert _is_max_tokens_param_error(Exception(
        "Unsupported parameter: 'max_tokens' is not supported with this model. "
        "Use 'max_completion_tokens' instead."
    ))
    assert not _is_max_tokens_param_error(Exception("some other 400"))


class _FakeBadRequest(openai.BadRequestError):
    def __init__(self, message):
        Exception.__init__(self, message)


def _fake_text_response():
    msg = types.SimpleNamespace(content="hello", tool_calls=None)
    choice = types.SimpleNamespace(message=msg, finish_reason="stop")
    return types.SimpleNamespace(choices=[choice], usage=None)


class _FakeCompletions:
    def __init__(self):
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if "max_tokens" in kwargs:
            raise _FakeBadRequest(
                "Unsupported parameter: 'max_tokens' is not supported with this "
                "model. Use 'max_completion_tokens' instead."
            )
        return _fake_text_response()


def _provider_with_fake_client(model):
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider._model = model
    provider._max_tokens = 4096
    provider._token_param = _default_token_param(model)
    fake_completions = _FakeCompletions()
    provider._client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=fake_completions)
    )
    return provider, fake_completions


def test_complete_self_corrects_from_max_tokens_to_max_completion_tokens():
    # A gpt-4o-named endpoint that actually requires max_completion_tokens:
    # first call 400s on max_tokens, provider retries once and remembers.
    provider, completions = _provider_with_fake_client("gpt-4o")
    out = asyncio.run(provider.complete([{"role": "user", "content": "hi"}], [], "sys"))
    assert out["type"] == "text"
    assert len(completions.calls) == 2
    assert "max_tokens" in completions.calls[0]
    assert "max_completion_tokens" in completions.calls[1]
    # Remembered for the rest of the session.
    assert provider._token_param == "max_completion_tokens"


def test_complete_uses_max_completion_tokens_upfront_for_o_series():
    provider, completions = _provider_with_fake_client("o3-mini")
    out = asyncio.run(provider.complete([{"role": "user", "content": "hi"}], [], "sys"))
    assert out["type"] == "text"
    assert len(completions.calls) == 1  # no wasted retry
    assert "max_completion_tokens" in completions.calls[0]
    assert "max_tokens" not in completions.calls[0]


# ── F-B-12: remote base_url with no key must warn loudly (not silent 401) ────

from orchestrator.providers.openai_provider import _is_localhost_url  # noqa: E402


@pytest.mark.parametrize("url,is_local", [
    ("http://localhost:8000/v1", True),
    ("http://127.0.0.1:1234/v1", True),
    ("http://[::1]:8080/v1", True),
    ("https://api.cerebras.ai/v1", False),
    ("https://api.groq.com/openai/v1", False),
])
def test_is_localhost_url(url, is_local):
    assert _is_localhost_url(url) is is_local


def test_remote_base_url_without_key_warns(monkeypatch, caplog):
    import logging
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(openai, "AsyncOpenAI", _FakeAsyncOpenAI)

    with caplog.at_level(logging.WARNING, logger="orchestrator.providers.openai_provider"):
        provider = OpenAIProvider({
            "openai_base_url": "https://api.cerebras.ai/v1",
            "openai_api_key_env": "CEREBRAS_API_KEY",
        })
    # Placeholder key is still used (some remote proxies are token-less), but the
    # user is warned instead of getting a silent cryptic 401.
    assert provider._client.kwargs["api_key"] == "placeholder"
    assert any("placeholder" in r.message and "CEREBRAS_API_KEY" in r.message for r in caplog.records)


def test_local_base_url_without_key_stays_silent(monkeypatch, caplog):
    import logging
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(openai, "AsyncOpenAI", _FakeAsyncOpenAI)

    with caplog.at_level(logging.WARNING, logger="orchestrator.providers.openai_provider"):
        OpenAIProvider({"openai_base_url": "http://localhost:11434/v1"})
    assert not any("placeholder" in r.message for r in caplog.records)

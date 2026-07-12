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

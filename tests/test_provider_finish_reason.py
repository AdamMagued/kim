"""Regression tests for provider stop/finish-reason surfacing + error
classification (findings 3.1, 3.4)."""
from __future__ import annotations

import json
import types

import pytest

from orchestrator.providers.base import (
    finalize_text_content,
    classify_provider_error,
)


# ── 3.1 finalize_text_content ──

def test_truncation_annotates_nonempty():
    out = finalize_text_content("half an answer", "max_tokens")
    assert out.startswith("half an answer")
    assert "truncated" in out.lower()


def test_truncation_stands_in_for_empty():
    out = finalize_text_content("", "length")
    assert "truncated" in out.lower()


def test_block_explains_empty():
    out = finalize_text_content("", "SAFETY")
    assert out and "safety" in out.lower()


def test_block_keeps_nonempty_content():
    # A block reason with partial content: don't clobber what was produced.
    assert finalize_text_content("partial", "content_filter") == "partial"


@pytest.mark.parametrize("reason", ["stop", "end_turn", "tool_use", None, ""])
def test_normal_stop_passes_through(reason):
    assert finalize_text_content("done", reason) == "done"


def test_gemini_uppercase_max_tokens():
    out = finalize_text_content("clipped", "MAX_TOKENS")
    assert "truncated" in out.lower()


# ── 3.4 JSONDecodeError is retryable network, not permanent invalid_request ──

def test_jsondecodeerror_is_retryable_network():
    try:
        json.loads("{ truncated")
    except json.JSONDecodeError as exc:
        pe = classify_provider_error(exc)
        assert pe.code == "network"
        assert pe.retryable is True
    else:  # pragma: no cover
        pytest.fail("expected JSONDecodeError")


def test_plain_valueerror_still_invalid_request():
    pe = classify_provider_error(ValueError("bad request"))
    assert pe.code == "invalid_request"
    assert pe.retryable is False


# ── 3.1 provider parsers attach stop_reason ──

def _fake_openai_response(content, finish_reason):
    msg = types.SimpleNamespace(content=content, tool_calls=None)
    choice = types.SimpleNamespace(message=msg, finish_reason=finish_reason)
    return types.SimpleNamespace(choices=[choice], usage=None)


def test_openai_parser_surfaces_truncation():
    from orchestrator.providers.openai_provider import OpenAIProvider
    prov = OpenAIProvider.__new__(OpenAIProvider)  # skip __init__ (no API key)
    out = prov._parse_response(_fake_openai_response("clipped text", "length"))
    assert out["type"] == "text"
    assert out["stop_reason"] == "length"
    assert "truncated" in out["content"].lower()


def test_openai_parser_normal_stop():
    from orchestrator.providers.openai_provider import OpenAIProvider
    prov = OpenAIProvider.__new__(OpenAIProvider)
    out = prov._parse_response(_fake_openai_response("all good", "stop"))
    assert out["content"] == "all good"
    assert out["stop_reason"] == "stop"


def test_gemini_parser_surfaces_truncation():
    from orchestrator.providers.gemini import GeminiProvider
    prov = GeminiProvider.__new__(GeminiProvider)
    resp = {
        "candidates": [
            {"content": {"parts": [{"text": "partial answer"}]}, "finishReason": "MAX_TOKENS"}
        ],
        "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2},
    }
    out = prov._parse_rest_response(resp)
    assert out["type"] == "text"
    assert out["stop_reason"] == "MAX_TOKENS"
    assert "truncated" in out["content"].lower()
    assert out["usage"]["cache_creation_tokens"] == 0  # 3.8 shape consistency

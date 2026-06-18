"""E1: GeminiProvider must not drop parallel functionCall parts."""

import pytest
from orchestrator.providers.gemini import GeminiProvider


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    return GeminiProvider({"api_key": "test-key"})


def _resp(parts):
    return {
        "candidates": [{"content": {"parts": parts}}],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2},
    }


def test_single_function_call(provider):
    out = provider._parse_rest_response(
        _resp([{"functionCall": {"name": "read_file", "args": {"path": "a"}}}])
    )
    assert out["type"] == "tool_call"
    assert out["tool"] == "read_file"
    assert out["args"] == {"path": "a"}


def test_multiple_function_calls_wrapped_as_batch(provider):
    out = provider._parse_rest_response(
        _resp(
            [
                {"functionCall": {"name": "read_file", "args": {"path": "a"}}},
                {"functionCall": {"name": "list_dir", "args": {"path": "b"}}},
            ]
        )
    )
    assert out["type"] == "tool_call"
    assert out["tool"] == "batch"
    calls = out["args"]["calls"]
    assert [c["tool"] for c in calls] == ["read_file", "list_dir"]
    assert calls[1]["args"] == {"path": "b"}


def test_text_only(provider):
    out = provider._parse_rest_response(_resp([{"text": "hello"}]))
    assert out["type"] == "text"
    assert out["content"] == "hello"

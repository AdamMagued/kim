"""F-B-2: claude.py must not submit an assistant-first message list.

A memory trim that walks the resume boundary back to an assistant tool-call turn
yields [assistant, user, …]. Anthropic 400s with "first message must use the
user role" (classified invalid_request / non-retryable), bricking the resumed
session. The provider prepends a synthetic user turn as belt-and-suspenders.
"""
from __future__ import annotations

from orchestrator.providers.claude import AnthropicProvider


def _provider():
    return object.__new__(AnthropicProvider)


def test_leading_assistant_gets_synthetic_user_prefix():
    p = _provider()
    trimmed = [
        {"role": "assistant", "content": '{"type":"tool_call","tool":"read_file","args":{"path":"a"}}'},
        {"role": "user", "content": "[Tool result: read_file]\ncontents"},
    ]
    out = p._to_claude_messages(trimmed)
    assert out[0]["role"] == "user", "first message must be a user turn for Anthropic"
    assert len(out) == 3
    # Original turns are preserved after the synthetic prefix.
    assert out[1]["role"] == "assistant"
    assert out[2]["role"] == "user"


def test_user_first_list_is_unchanged():
    p = _provider()
    normal = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    out = p._to_claude_messages(normal)
    assert len(out) == 2
    assert out[0]["role"] == "user"
    assert out[0]["content"] == "hi"


def test_empty_message_list_is_unchanged():
    p = _provider()
    assert p._to_claude_messages([]) == []

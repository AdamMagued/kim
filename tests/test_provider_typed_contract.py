"""F-B-14: the base TypedDict contract must declare every field the providers
actually return and the agent actually consumes.

Before the fix, `ToolCallResponse` declared only type/tool/args and `TextResponse`
omitted `stop_reason`, so the declared contract was not the one the agent reads
(`response["content"]` on tool calls, `stop_reason`/`usage` throughout). These
assertions pin the contract to the de-facto richest shape (the V-3 matrix).
"""
from __future__ import annotations

from orchestrator.providers.base import ToolCallResponse, TextResponse


def test_tool_call_response_declares_narration_stop_reason_usage():
    ann = ToolCallResponse.__annotations__
    # Always-present keys.
    assert "type" in ann and "tool" in ann and "args" in ann
    # Fields every provider attaches / the agent consumes (F-B-14).
    assert "content" in ann, "narration content is part of the tool_call contract"
    assert "stop_reason" in ann, "stop_reason is part of the tool_call contract"
    assert "usage" in ann, "usage is part of the tool_call contract"


def test_text_response_declares_stop_reason_and_usage():
    ann = TextResponse.__annotations__
    assert "type" in ann and "content" in ann
    assert "stop_reason" in ann, "TextResponse must declare stop_reason (F-B-14)"
    assert "usage" in ann


def test_typed_dicts_are_non_total_so_optional_fields_may_be_omitted():
    # Providers omit optional fields; the TypedDicts must not require them.
    assert ToolCallResponse.__total__ is False
    assert TextResponse.__total__ is False

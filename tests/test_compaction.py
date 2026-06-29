"""Regression tests for orchestrator/compaction.py."""

import pytest

from orchestrator.compaction import (
    compact_messages,
    should_compact,
    _estimate_message_tokens,
    _is_tool_call,
    IMAGE_TOKEN_ESTIMATE,
    MAX_ESTIMATED_TOKENS,
)


def test_compact_messages_empty_raises():
    """compact_messages([]) must raise ValueError."""
    with pytest.raises(ValueError):
        compact_messages([])


def test_image_token_estimate_applied():
    """_estimate_message_tokens counts IMAGE_TOKEN_ESTIMATE for image items, not len//4."""
    msg = {
        "role": "user",
        "content": [
            {"type": "image", "data": "abc", "media_type": "image/png"},
        ],
    }
    result = _estimate_message_tokens(msg)
    assert result == IMAGE_TOKEN_ESTIMATE


def test_is_tool_call_detects_structured_list():
    """_is_tool_call returns True for assistant message with list content containing a tool_call item."""
    msg = {
        "role": "assistant",
        "content": [
            {"type": "tool_call", "tool": "read_file", "args": {"path": "/foo"}},
        ],
    }
    assert _is_tool_call(msg) is True


def test_should_compact_threshold():
    """should_compact returns True above MAX_ESTIMATED_TOKENS and False below, ignoring leading compact_summary."""
    # Build a message whose content totals well above the threshold.
    # Each char contributes 1/4 token; 4 * MAX_ESTIMATED_TOKENS chars > threshold.
    long_text = "a" * (4 * MAX_ESTIMATED_TOKENS + 100)
    history_above = [{"role": "user", "content": long_text}]
    assert should_compact(history_above) is True

    # Small history should be below threshold.
    history_below = [{"role": "user", "content": "hello"}]
    assert should_compact(history_below) is False

    # A leading compact_summary must be ignored; only real history counts.
    messages_with_summary = [
        {"role": "compact_summary", "content": "x" * (4 * MAX_ESTIMATED_TOKENS + 100)},
        {"role": "user", "content": "hello"},
    ]
    assert should_compact(messages_with_summary) is False

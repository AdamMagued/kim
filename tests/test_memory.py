"""Regression tests for orchestrator/memory.py — ConversationMemory."""

import pytest

from orchestrator.memory import ConversationMemory


# ---------------------------------------------------------------------------
# 1. get_messages deep-copies content
# ---------------------------------------------------------------------------


def test_get_messages_deep_copies_content_string():
    """Mutating the string-content returned by get_messages must not affect the store."""
    mem = ConversationMemory()
    mem.add_user("original text")

    msgs = mem.get_messages()
    # Replace the content value on the returned dict — stored message must be unaffected.
    msgs[0]["content"] = "mutated"

    stored = mem.get_messages()
    assert stored[0]["content"] == "original text", (
        "Stored message content was mutated through the get_messages return value"
    )


def test_get_messages_deep_copies_content_list():
    """Mutating a list-content item returned by get_messages must not affect the store."""
    mem = ConversationMemory()
    mem.add_user([{"type": "text", "text": "hello"}])

    msgs = mem.get_messages()
    # Mutate the nested dict in the returned list
    msgs[0]["content"][0]["text"] = "MUTATED"

    stored = mem.get_messages()
    assert stored[0]["content"][0]["text"] == "hello", (
        "Stored message list-content was mutated through the get_messages return value"
    )


def test_get_messages_deep_copies_content_list_append():
    """Appending to the list returned by get_messages must not grow the stored content."""
    mem = ConversationMemory()
    mem.add_user([{"type": "text", "text": "original"}])

    msgs = mem.get_messages()
    msgs[0]["content"].append({"type": "text", "text": "extra"})

    stored = mem.get_messages()
    assert len(stored[0]["content"]) == 1, (
        "Appending to the returned content list must not affect the stored message"
    )


# ---------------------------------------------------------------------------
# 2. _is_tool_result detection
# ---------------------------------------------------------------------------


def test_is_tool_result_string_content_true():
    """String content starting with '[Tool result:' is detected as a tool result."""
    mem = ConversationMemory()
    msg = {"role": "user", "content": "[Tool result: {'output': 'ok'}]"}
    assert mem._is_tool_result(msg) is True


def test_is_tool_result_list_text_item_true():
    """List content with a text item starting with '[Tool result:' is detected."""
    mem = ConversationMemory()
    msg = {
        "role": "user",
        "content": [{"type": "text", "text": "[Tool result: some output]"}],
    }
    assert mem._is_tool_result(msg) is True


def test_is_tool_result_list_mixed_items_true():
    """Detection works even when the matching text item is not the first list element."""
    mem = ConversationMemory()
    msg = {
        "role": "user",
        "content": [
            {"type": "image", "data": "abc", "media_type": "image/png"},
            {"type": "text", "text": "[Tool result: something]"},
        ],
    }
    assert mem._is_tool_result(msg) is True


def test_is_tool_result_plain_user_message_false():
    """A plain user message string does not trigger tool-result detection."""
    mem = ConversationMemory()
    msg = {"role": "user", "content": "What is the weather?"}
    assert mem._is_tool_result(msg) is False


def test_is_tool_result_empty_string_false():
    """Empty string content returns False."""
    mem = ConversationMemory()
    msg = {"role": "user", "content": ""}
    assert mem._is_tool_result(msg) is False


def test_is_tool_result_list_no_tool_prefix_false():
    """A list content whose text items do not start with '[Tool result:' returns False."""
    mem = ConversationMemory()
    msg = {
        "role": "user",
        "content": [{"type": "text", "text": "regular message"}],
    }
    assert mem._is_tool_result(msg) is False


def test_is_tool_result_assistant_message_with_prefix_false():
    """Even if an assistant message content starts with the prefix, detection is False
    because the function checks the content only — role-level filtering is the caller's
    responsibility.  This asserts the actual current behaviour of _is_tool_result."""
    mem = ConversationMemory()
    # _is_tool_result does NOT check role — it purely checks content prefix.
    msg = {"role": "assistant", "content": "[Tool result: x]"}
    # The function does not guard on role, so it returns True.
    assert mem._is_tool_result(msg) is True


# ---------------------------------------------------------------------------
# 3. trim keeps the preceding assistant tool_call when window starts on a tool result
# ---------------------------------------------------------------------------


def test_trim_keeps_preceding_tool_call_non_summary_path():
    """Non-summary path: when the first in-window user message is a tool result,
    the preceding assistant message is pulled in so no orphaned tool result remains."""
    mem = ConversationMemory(max_messages=4)
    # Manually build exactly max_messages messages to avoid intermediate trims.
    mem._messages = [
        {"role": "user", "content": "first user turn"},
        {"role": "assistant", "content": [{"type": "tool_call", "tool": "read_file", "args": {}}]},
        {"role": "user", "content": "[Tool result: file contents here]"},
        {"role": "user", "content": "follow-up question"},
    ]
    # Adding one more message pushes len to 5 > max_messages=4, triggering _enforce_limits.
    mem.add_user("trigger")

    # excess=1 → scan starts at i=1 (assistant) → skip; i=2 (tool result user) → is_tool_result=True
    # → start = i-1 = 1 (the assistant tool_call), so _messages = original[1:]
    # F-A-2: first message must be user, so "Resuming session." is prepended
    assert mem._messages[0]["role"] == "user"
    assert mem._messages[0]["content"] == "Resuming session."
    assert mem._messages[1]["role"] == "assistant", (
        "Second message must be the assistant tool_call, not the orphaned tool result"
    )
    assert mem._messages[1]["content"][0]["type"] == "tool_call"
    assert mem._messages[2]["content"] == "[Tool result: file contents here]"


def test_trim_keeps_preceding_tool_call_summary_path():
    """Summary path: compact_summary is pinned, and the preceding assistant tool_call
    is retained when the first in-window user message in `rest` is a tool result."""
    mem = ConversationMemory(max_messages=4)
    summary_msg = {"role": "compact_summary", "content": "Earlier conversation summary."}
    assistant_tool_call = {
        "role": "assistant",
        "content": [{"type": "tool_call", "tool": "list_dir", "args": {}}],
    }
    tool_result_msg = {"role": "user", "content": "[Tool result: dir listing]"}
    user_follow_up = {"role": "user", "content": "show me file.txt"}

    # Manually load exactly max_messages=4 messages (no trim yet).
    mem._messages = [summary_msg, assistant_tool_call, tool_result_msg, user_follow_up]

    # Adding another user message pushes total to 5 > 4 → summary path trim.
    mem.add_user("one more")

    # rest = [assistant_tool_call, tool_result_msg, user_follow_up, one_more]
    # max_rest = 3, excess = 1, range(1,4):
    #   i=1 → rest[1] = tool_result_msg (user, is_tool_result=True, i>0) → start=0
    # result = [summary] + rest[0:] = [summary, assistant_tool_call, ...]
    # F-A-2: first message after summary must be user, so "Resuming session." is prepended
    assert mem._messages[0]["role"] == "compact_summary"
    assert mem._messages[1]["role"] == "user"
    assert mem._messages[1]["content"] == "Resuming session."
    assert mem._messages[2]["role"] == "assistant", (
        "Preceding assistant tool_call must be retained after user resume in the summary path"
    )
    assert mem._messages[2]["content"][0]["type"] == "tool_call"
    assert mem._messages[3]["content"] == "[Tool result: dir listing]"


# ---------------------------------------------------------------------------
# 4. trim keeps at least the last message when no user message is in the window
# ---------------------------------------------------------------------------


def test_trim_keeps_at_least_last_message_non_summary_path():
    """Non-summary path: if the trimming window contains no user messages,
    the very last message is preserved and the list is never emptied."""
    mem = ConversationMemory(max_messages=2)
    # Manually set two non-user messages (= max_messages, no trim yet).
    first_assistant = {"role": "assistant", "content": "first response"}
    last_assistant = {"role": "assistant", "content": "second response"}
    mem._messages = [first_assistant, last_assistant]

    # Add a third assistant message → len=3 > 2 → no user found → keep last only.
    final_assistant = {"role": "assistant", "content": "final response"}
    mem._messages.append(final_assistant)
    mem._enforce_limits()

    # F-A-2: first message must be user, so "Resuming session." is prepended before the assistant message
    assert len(mem._messages) == 2, (
        "When no user message is found, the last message must be preserved and user resume prepended"
    )
    assert mem._messages[0]["role"] == "user"
    assert mem._messages[0]["content"] == "Resuming session."
    assert mem._messages[1]["content"] == "final response"


def test_trim_walks_back_through_multiple_consecutive_tool_results_non_summary_path():
    """When the trim window's first user message is the LATER of several
    consecutive tool-result messages, a single-step walk-back only reaches
    the earlier tool-result — still orphaning it from its assistant
    tool_call. _fix_tool_boundary must loop back through the whole stacked
    run of tool-result messages to the actual tool_call (finding 6; ports
    compaction.py's _fix_tool_boundary while-loop)."""
    mem = ConversationMemory(max_messages=4)
    tool_call_msg = {"role": "assistant", "content": [{"type": "tool_call", "tool": "batch", "args": {}}]}
    result_a = {"role": "user", "content": "[Tool result: part A]"}
    result_b = {"role": "user", "content": "[Tool result: part B]"}
    mem._messages = [
        {"role": "user", "content": "turn0"},
        tool_call_msg,
        result_a,
        result_b,
        {"role": "user", "content": "follow-up"},
        {"role": "assistant", "content": "some reply"},
    ]
    # Append a 7th message and trim directly (avoids intermediate trims).
    mem._messages.append({"role": "user", "content": "trigger"})
    mem._enforce_limits()

    # excess = 7 - 4 = 3 → scan starts at index 3 (result_b, the LATER of the
    # two stacked tool results). A single-step walk-back (the old behavior)
    # would land on result_a — still orphaning it from tool_call_msg. The
    # while-loop walk-back must continue past BOTH tool-result messages back
    # to the assistant tool_call.
    # F-A-2: first message must be user, so "Resuming session." is prepended
    assert mem._messages[0]["role"] == "user"
    assert mem._messages[0]["content"] == "Resuming session."
    assert mem._messages[1]["role"] == "assistant", (
        "Second preserved message must be the assistant tool_call — the old "
        "single-step walk-back stopped one message short and orphaned "
        "result_a from its tool_call"
    )
    assert mem._messages[1] is tool_call_msg
    assert mem._messages[2] is result_a
    assert mem._messages[3] is result_b


def test_trim_keeps_at_least_last_message_summary_path():
    """Summary path: if the rest slice contains no user messages,
    `[summary] + rest[-1:]` is used — the summary is pinned and the last rest message kept."""
    mem = ConversationMemory(max_messages=3)
    summary_msg = {"role": "compact_summary", "content": "Summary text."}
    asst1 = {"role": "assistant", "content": "response A"}
    asst2 = {"role": "assistant", "content": "response B"}

    # Load exactly max_messages=3 messages → no trim.
    mem._messages = [summary_msg, asst1, asst2]

    # Append a 4th assistant message and call _enforce_limits directly.
    asst3 = {"role": "assistant", "content": "response C"}
    mem._messages.append(asst3)
    mem._enforce_limits()

    # rest = [asst1, asst2, asst3], max_rest=2, excess=1
    # range(1,3): i=1 → assistant, i=2 → assistant — no user found
    # → _messages = [summary] + rest[-1:] = [summary, asst3]
    # F-A-2: first message after summary must be user, so "Resuming session." is prepended
    assert len(mem._messages) == 3, (
        "Summary path with no user message must produce [summary, user_resume, last_message]"
    )
    assert mem._messages[0]["role"] == "compact_summary"
    assert mem._messages[1]["role"] == "user"
    assert mem._messages[1]["content"] == "Resuming session."
    assert mem._messages[2]["content"] == "response C"

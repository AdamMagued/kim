"""Golden tests for codex_engine/responses_passthrough.py and _CodexProxy's
"responses-passthrough" mode.

codex 0.144.3 removed the chat-completions wire API entirely (``WireApi`` in
codex-rs/model-provider-info/src/lib.rs has only the ``Responses`` variant),
so API providers (claude, gemini, deepseek, ollama-behind-proxy) are served
natively on /v1/responses in this mode instead of via
codex_engine/chat_passthrough.py. Covers: request parsing (incl.
function_call_output pairing by call_id), text/tool-call replies via
engine.py's existing _make_responses_text_reply/_make_responses_tool_reply
emitters, the granular SSE event sequence codex requires, and the relay-cap
turn-reset (TUI fix 1, still applied in this mode).
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from codex_engine.engine import _CodexProxy, _make_sse_response
from codex_engine.responses_passthrough import (
    canonical_reply_to_responses_parts,
    responses_request_to_canonical,
)


# ---------------------------------------------------------------------------
# Part 1 — pure translation golden tests
# ---------------------------------------------------------------------------


class RequestToCanonicalTests(unittest.TestCase):
    def test_instructions_becomes_system_prompt(self):
        body = {"instructions": "You are Kim.", "input": [{"role": "user", "content": "hi"}]}
        messages, tools, system = responses_request_to_canonical(body)
        self.assertEqual(system, "You are Kim.")
        self.assertEqual(messages, [{"role": "user", "content": "hi"}])
        self.assertEqual(tools, [])

    def test_no_instructions_returns_none_system(self):
        body = {"input": [{"role": "user", "content": "hi"}]}
        _, _, system = responses_request_to_canonical(body)
        self.assertIsNone(system)

    def test_bare_string_input(self):
        body = {"input": "just a plain string task"}
        messages, _, _ = responses_request_to_canonical(body)
        self.assertEqual(messages, [{"role": "user", "content": "just a plain string task"}])

    def test_bare_string_item_in_list(self):
        body = {"input": ["a bare string item"]}
        messages, _, _ = responses_request_to_canonical(body)
        self.assertEqual(messages, [{"role": "user", "content": "a bare string item"}])

    def test_message_item_with_content_parts(self):
        body = {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "what is this?"},
                        {"type": "input_image", "image_url": "data:image/png;base64,QUJD"},
                    ],
                }
            ]
        }
        messages, _, _ = responses_request_to_canonical(body)
        content = messages[0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "what is this?"})
        self.assertEqual(content[1], {"type": "image", "data": "QUJD", "media_type": "image/png"})

    def test_output_text_content_part_from_prior_assistant_turn(self):
        body = {"input": [{"role": "assistant", "content": [{"type": "output_text", "text": "prior answer"}]}]}
        messages, _, _ = responses_request_to_canonical(body)
        self.assertEqual(messages[0]["content"], [{"type": "text", "text": "prior answer"}])

    def test_non_data_uri_image_is_skipped(self):
        body = {
            "input": [{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "hi"},
                    {"type": "input_image", "image_url": "https://example.com/x.png"},
                ],
            }]
        }
        messages, _, _ = responses_request_to_canonical(body)
        self.assertEqual(messages[0]["content"], [{"type": "text", "text": "hi"}])

    def test_function_call_becomes_canonical_tool_call_turn(self):
        body = {
            "input": [
                {"role": "user", "content": "list files"},
                {"type": "function_call", "call_id": "call_abc", "name": "shell", "arguments": '{"cmd": "ls"}'},
            ]
        }
        messages, _, _ = responses_request_to_canonical(body)
        self.assertEqual(messages[0], {"role": "user", "content": "list files"})
        parsed = json.loads(messages[1]["content"])
        self.assertEqual(parsed, {"type": "tool_call", "tool": "shell", "args": {"cmd": "ls"}, "content": ""})

    def test_function_call_output_paired_by_call_id(self):
        body = {
            "input": [
                {"type": "function_call", "call_id": "call_abc", "name": "shell", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call_abc", "output": "file1\nfile2"},
            ]
        }
        messages, _, _ = responses_request_to_canonical(body)
        self.assertEqual(messages[1], {"role": "user", "content": "[Tool result: shell]\nfile1\nfile2"})

    def test_two_function_calls_pair_with_correct_outputs(self):
        # #40-style: two distinct pending calls, results must not cross-pair.
        body = {
            "input": [
                {"type": "function_call", "call_id": "call_1", "name": "a", "arguments": "{}"},
                {"type": "function_call", "call_id": "call_2", "name": "b", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call_2", "output": "result b"},
                {"type": "function_call_output", "call_id": "call_1", "output": "result a"},
            ]
        }
        messages, _, _ = responses_request_to_canonical(body)
        self.assertEqual(messages[2]["content"], "[Tool result: b]\nresult b")
        self.assertEqual(messages[3]["content"], "[Tool result: a]\nresult a")

    def test_orphan_function_call_output_uses_unknown(self):
        body = {"input": [{"type": "function_call_output", "call_id": "call_missing", "output": "late result"}]}
        messages, _, _ = responses_request_to_canonical(body)
        self.assertEqual(messages[0]["content"], "[Tool result: unknown]\nlate result")

    def test_function_call_output_list_content(self):
        body = {
            "input": [
                {"type": "function_call", "call_id": "call_1", "name": "read_file", "arguments": "{}"},
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": [{"type": "output_text", "text": "line one"}, {"type": "output_text", "text": "line two"}],
                },
            ]
        }
        messages, _, _ = responses_request_to_canonical(body)
        self.assertEqual(messages[1]["content"], "[Tool result: read_file]\nline one\nline two")

    def test_malformed_function_call_arguments_become_empty_dict(self):
        body = {"input": [{"type": "function_call", "call_id": "c1", "name": "x", "arguments": "{not json"}]}
        messages, _, _ = responses_request_to_canonical(body)
        self.assertEqual(json.loads(messages[0]["content"])["args"], {})

    def test_flat_tools_translation(self):
        body = {
            "input": [],
            "tools": [{
                "type": "function", "name": "shell", "description": "Run a shell command.",
                "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}, "strict": False,
            }],
        }
        _, tools, _ = responses_request_to_canonical(body)
        self.assertEqual(tools, [{
            "name": "shell", "description": "Run a shell command.",
            "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
        }])

    def test_function_nested_tools_translation_is_tolerated(self):
        body = {"input": [], "tools": [{"function": {"name": "shell", "description": "d", "parameters": {}}}]}
        _, tools, _ = responses_request_to_canonical(body)
        self.assertEqual(tools[0]["name"], "shell")


class CanonicalReplyToResponsesPartsTests(unittest.TestCase):
    def test_text_reply(self):
        text, tool_calls = canonical_reply_to_responses_parts({"type": "text", "content": "hello"})
        self.assertEqual(text, "hello")
        self.assertIsNone(tool_calls)

    def test_tool_call_reply(self):
        resp = {"type": "tool_call", "tool": "shell", "args": {"cmd": "ls"}, "content": "Listing."}
        text, tool_calls = canonical_reply_to_responses_parts(resp)
        self.assertEqual(text, "Listing.")
        self.assertEqual(tool_calls, [{"name": "shell", "input": {"cmd": "ls"}}])

    def test_batch_tool_call_expands_to_multiple_entries(self):
        resp = {
            "type": "tool_call", "tool": "batch",
            "args": {"calls": [{"tool": "a", "args": {}}, {"tool": "b", "args": {"x": 1}}]},
        }
        _, tool_calls = canonical_reply_to_responses_parts(resp)
        self.assertEqual(tool_calls, [{"name": "a", "input": {}}, {"name": "b", "input": {"x": 1}}])


# ---------------------------------------------------------------------------
# Part 2 — _CodexProxy(mode="responses-passthrough") integration
# ---------------------------------------------------------------------------


class _RecordingBaseProvider:
    """Plain BaseProvider-shaped stand-in — no clear_chat/handoff kwargs."""

    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0
        self.calls: list[dict] = []

    async def complete(self, messages, tools, system):
        self.calls.append({"messages": messages, "tools": tools, "system": system})
        resp = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return resp


def _request(proxy: _CodexProxy, body: dict):
    return SimpleNamespace(
        headers={"Authorization": f"Bearer {proxy._bearer_token}"},
        json=AsyncMock(return_value=body),
    )


def _sse_events(resp) -> list:
    raw = resp.body
    assert isinstance(raw, (bytes, bytearray))
    events = []
    for frame in bytes(raw).decode().split("\n\n"):
        frame = frame.strip()
        if not frame:
            continue
        assert frame.startswith("data: "), f"bad SSE frame: {frame!r}"
        payload = frame[len("data: "):]
        events.append("[DONE]" if payload == "[DONE]" else json.loads(payload))
    return events


def _event_types(events: list) -> list:
    return [ev if ev == "[DONE]" else ev["type"] for ev in events]


class ResponsesPassthroughModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_reply_end_to_end(self):
        provider = _RecordingBaseProvider([{"type": "text", "content": "hello there"}])
        proxy = _CodexProxy(provider, mode="responses-passthrough")
        body = {"instructions": "be nice", "input": [{"role": "user", "content": "hi"}]}
        resp = await proxy._handle_responses(_request(proxy, body))
        self.assertEqual(resp.status, 200)
        payload = json.loads(resp.body)
        self.assertEqual(payload["object"], "response")
        self.assertEqual(payload["output"][0]["content"][0]["text"], "hello there")
        # Native BaseProvider signature — no clear_chat/handoff kwargs.
        self.assertEqual(provider.calls[0]["system"], "be nice")
        self.assertEqual(provider.calls[0]["messages"], [{"role": "user", "content": "hi"}])

    async def test_tool_call_reply_end_to_end(self):
        provider = _RecordingBaseProvider([{"type": "tool_call", "tool": "shell", "args": {"cmd": "ls"}}])
        proxy = _CodexProxy(provider, mode="responses-passthrough")
        body = {"input": [{"role": "user", "content": "list files"}]}
        resp = await proxy._handle_responses(_request(proxy, body))
        payload = json.loads(resp.body)
        call = next(item for item in payload["output"] if item["type"] == "function_call")
        self.assertEqual(call["name"], "shell")
        self.assertEqual(json.loads(call["arguments"]), {"cmd": "ls"})
        self.assertTrue(call["call_id"].startswith("call_"))

    async def test_function_call_output_reaches_provider_paired(self):
        provider = _RecordingBaseProvider([{"type": "text", "content": "done"}])
        proxy = _CodexProxy(provider, mode="responses-passthrough")
        body = {
            "input": [
                {"role": "user", "content": "list files"},
                {"type": "function_call", "call_id": "call_1", "name": "shell", "arguments": '{"cmd": "ls"}'},
                {"type": "function_call_output", "call_id": "call_1", "output": "a.txt\nb.txt"},
            ]
        }
        await proxy._handle_responses(_request(proxy, body))
        sent = provider.calls[0]["messages"]
        self.assertEqual(sent[2], {"role": "user", "content": "[Tool result: shell]\na.txt\nb.txt"})

    async def test_sse_stream_uses_granular_event_sequence(self):
        provider = _RecordingBaseProvider([{"type": "tool_call", "tool": "shell", "args": {"cmd": "ls"}, "content": "ok"}])
        proxy = _CodexProxy(provider, mode="responses-passthrough")
        body = {"input": [{"role": "user", "content": "list files"}], "stream": True}
        resp = await proxy._handle_responses(_request(proxy, body))
        events = _sse_events(resp)
        self.assertEqual(
            _event_types(events),
            [
                "response.created",
                "response.output_item.added", "response.output_text.delta",
                "response.output_text.done", "response.output_item.done",
                "response.output_item.added", "response.function_call_arguments.delta",
                "response.function_call_arguments.done", "response.output_item.done",
                "response.completed",
                "[DONE]",
            ],
        )

    async def test_no_compaction_no_delta_cursor_full_input_sent_every_relay(self):
        # Same reply every relay regardless of the growing history — proves
        # the FULL canonical translation is sent each time (no delta cursor).
        provider = _RecordingBaseProvider([{"type": "text", "content": "ok"}])
        proxy = _CodexProxy(provider, mode="responses-passthrough")

        body1 = {"input": [{"role": "user", "content": "x" * 5000} for _ in range(30)]}
        await proxy._handle_responses(_request(proxy, body1))
        self.assertEqual(len(provider.calls[0]["messages"]), 30)
        self.assertEqual(proxy._compaction_cache, {})  # compaction never runs in this mode

        body2 = {"input": [*body1["input"], {"role": "user", "content": "one more"}]}
        await proxy._handle_responses(_request(proxy, body2))
        self.assertEqual(len(provider.calls[1]["messages"]), 31)  # full list again, not a delta

    async def test_relay_cap_resets_on_new_user_turn(self):
        provider = _RecordingBaseProvider([{"type": "text", "content": "ok"}])
        proxy = _CodexProxy(provider, mode="responses-passthrough", max_relays=2)

        body1 = {"input": [{"role": "user", "content": "task one"}]}
        r1 = await proxy._handle_responses(_request(proxy, body1))
        self.assertEqual(r1.status, 200)

        body1b = {"input": [
            *body1["input"],
            {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "done"},
        ]}
        r1b = await proxy._handle_responses(_request(proxy, body1b))
        self.assertEqual(r1b.status, 200)
        self.assertEqual(proxy._relay_count, 2)

        # A third relay in the SAME turn (only tool call/result items, no new
        # user message) exceeds max_relays=2.
        body1c = {"input": [*body1b["input"], {"type": "function_call_output", "call_id": "stale", "output": "x"}]}
        r1c = await proxy._handle_responses(_request(proxy, body1c))
        self.assertEqual(r1c.status, 429)

        # A genuinely new user turn resets the budget.
        body2 = {"input": [*body1c["input"], {"role": "user", "content": "task two"}]}
        r2 = await proxy._handle_responses(_request(proxy, body2))
        self.assertEqual(r2.status, 200)
        self.assertEqual(proxy._relay_count, 1)


if __name__ == "__main__":
    unittest.main()

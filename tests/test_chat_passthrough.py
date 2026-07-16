"""Golden translation tests for codex_engine/chat_passthrough.py.

Covers the OpenAI /v1/chat/completions <-> BaseProvider canonical translation
used by _CodexProxy's "chat-passthrough" mode: text/tool_call/image/tool-result
requests, finish_reason mapping, and the SSE chunk-frame sequence.
"""

import json
import unittest

from codex_engine.chat_passthrough import (
    canonical_to_chat_response,
    chat_request_to_canonical,
    stream_chat_response,
)


class ChatRequestToCanonicalTests(unittest.TestCase):
    def test_system_message_becomes_system_prompt(self):
        body = {
            "messages": [
                {"role": "system", "content": "You are Kim."},
                {"role": "user", "content": "hi"},
            ]
        }
        messages, tools, system = chat_request_to_canonical(body)
        self.assertEqual(system, "You are Kim.")
        self.assertEqual(messages, [{"role": "user", "content": "hi"}])
        self.assertEqual(tools, [])

    def test_developer_and_system_messages_concatenate_in_order(self):
        body = {
            "messages": [
                {"role": "system", "content": "First."},
                {"role": "developer", "content": "Second."},
                {"role": "user", "content": "go"},
            ]
        }
        _, _, system = chat_request_to_canonical(body)
        self.assertEqual(system, "First.\n\nSecond.")

    def test_no_system_message_returns_none(self):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        _, _, system = chat_request_to_canonical(body)
        self.assertIsNone(system)

    def test_plain_user_text_passthrough(self):
        body = {"messages": [{"role": "user", "content": "hello there"}]}
        messages, _, _ = chat_request_to_canonical(body)
        self.assertEqual(messages, [{"role": "user", "content": "hello there"}])

    def test_user_message_with_image_data_uri(self):
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this?"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,QUJD"},
                        },
                    ],
                }
            ]
        }
        messages, _, _ = chat_request_to_canonical(body)
        self.assertEqual(len(messages), 1)
        content = messages[0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "what is this?"})
        self.assertEqual(content[1], {"type": "image", "data": "QUJD", "media_type": "image/png"})

    def test_non_data_uri_image_url_is_skipped(self):
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "hi"},
                        {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}},
                    ],
                }
            ]
        }
        messages, _, _ = chat_request_to_canonical(body)
        # The remote URL carries no inline data to forward — only the text part survives.
        self.assertEqual(messages[0]["content"], [{"type": "text", "text": "hi"}])

    def test_assistant_tool_call_becomes_canonical_json_content(self):
        body = {
            "messages": [
                {"role": "user", "content": "list files"},
                {
                    "role": "assistant",
                    "content": "Listing now.",
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {"name": "shell", "arguments": '{"cmd": "ls"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_abc", "content": "file1\nfile2"},
            ]
        }
        messages, _, _ = chat_request_to_canonical(body)
        self.assertEqual(messages[0], {"role": "user", "content": "list files"})
        assistant_turn = messages[1]
        self.assertEqual(assistant_turn["role"], "assistant")
        parsed = json.loads(assistant_turn["content"])
        self.assertEqual(parsed, {
            "type": "tool_call", "tool": "shell", "args": {"cmd": "ls"}, "content": "Listing now.",
        })
        self.assertEqual(messages[2], {"role": "user", "content": "[Tool result: shell]\nfile1\nfile2"})

    def test_multiple_tool_calls_degrade_to_sequential_turns(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": "Doing two things.",
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
                        {"id": "call_2", "type": "function", "function": {"name": "b", "arguments": "{}"}},
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "result a"},
                {"role": "tool", "tool_call_id": "call_2", "content": "result b"},
            ]
        }
        messages, _, _ = chat_request_to_canonical(body)
        # Two separate assistant tool-call turns (sequential degrade) in wire
        # order, followed by their tool-result turns in wire order.
        self.assertEqual(len(messages), 4)
        first = json.loads(messages[0]["content"])
        second = json.loads(messages[1]["content"])
        self.assertEqual(first["tool"], "a")
        self.assertEqual(first["content"], "Doing two things.")
        self.assertEqual(second["tool"], "b")
        self.assertEqual(second["content"], "")  # narration only on the first call
        self.assertEqual(messages[2]["content"], "[Tool result: a]\nresult a")
        self.assertEqual(messages[3]["content"], "[Tool result: b]\nresult b")

    def test_malformed_tool_call_arguments_become_empty_dict(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "x", "arguments": "{not json"}},
                    ],
                },
            ]
        }
        messages, _, _ = chat_request_to_canonical(body)
        parsed = json.loads(messages[0]["content"])
        self.assertEqual(parsed["args"], {})

    def test_orphan_tool_result_uses_unknown_name(self):
        body = {"messages": [{"role": "tool", "tool_call_id": "call_missing", "content": "late result"}]}
        messages, _, _ = chat_request_to_canonical(body)
        self.assertEqual(messages[0]["content"], "[Tool result: unknown]\nlate result")

    def test_tools_array_translation(self):
        body = {
            "messages": [],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "shell",
                        "description": "Run a shell command.",
                        "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
                    },
                }
            ],
        }
        _, tools, _ = chat_request_to_canonical(body)
        self.assertEqual(tools, [{
            "name": "shell",
            "description": "Run a shell command.",
            "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
        }])


class CanonicalToChatResponseTests(unittest.TestCase):
    def test_text_reply(self):
        resp = {"type": "text", "content": "hello", "stop_reason": "end_turn"}
        reply = canonical_to_chat_response(resp, model="kim-proxy-model", request_id="chatcmpl_1")
        self.assertEqual(reply["id"], "chatcmpl_1")
        self.assertEqual(reply["object"], "chat.completion")
        self.assertEqual(reply["model"], "kim-proxy-model")
        choice = reply["choices"][0]
        self.assertEqual(choice["message"], {"role": "assistant", "content": "hello"})
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertNotIn("usage", reply)

    def test_tool_call_reply(self):
        resp = {"type": "tool_call", "tool": "shell", "args": {"cmd": "ls"}, "content": "Listing."}
        reply = canonical_to_chat_response(resp, model="m", request_id="r1")
        choice = reply["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        message = choice["message"]
        self.assertEqual(message["content"], "Listing.")
        self.assertEqual(len(message["tool_calls"]), 1)
        tc = message["tool_calls"][0]
        self.assertTrue(tc["id"].startswith("call_"))
        self.assertEqual(tc["type"], "function")
        self.assertEqual(tc["function"]["name"], "shell")
        self.assertEqual(json.loads(tc["function"]["arguments"]), {"cmd": "ls"})

    def test_batch_tool_call_expands_to_parallel_tool_calls(self):
        resp = {
            "type": "tool_call",
            "tool": "batch",
            "args": {"calls": [{"tool": "a", "args": {}}, {"tool": "b", "args": {"x": 1}}]},
            "content": "",
        }
        reply = canonical_to_chat_response(resp, model="m", request_id="r1")
        tool_calls = reply["choices"][0]["message"]["tool_calls"]
        self.assertEqual(len(tool_calls), 2)
        self.assertEqual(tool_calls[0]["function"]["name"], "a")
        self.assertEqual(tool_calls[1]["function"]["name"], "b")
        self.assertEqual(json.loads(tool_calls[1]["function"]["arguments"]), {"x": 1})

    def test_usage_passthrough(self):
        resp = {"type": "text", "content": "hi", "usage": {"input": 10, "output": 5}}
        reply = canonical_to_chat_response(resp, model="m", request_id="r1")
        self.assertEqual(reply["usage"], {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})

    def test_finish_reason_truncation_mapping(self):
        resp = {"type": "text", "content": "cut off", "stop_reason": "max_tokens"}
        reply = canonical_to_chat_response(resp, model="m", request_id="r1")
        self.assertEqual(reply["choices"][0]["finish_reason"], "length")

    def test_finish_reason_content_filter_mapping(self):
        resp = {"type": "text", "content": "", "stop_reason": "safety"}
        reply = canonical_to_chat_response(resp, model="m", request_id="r1")
        self.assertEqual(reply["choices"][0]["finish_reason"], "content_filter")


class StreamChatResponseTests(unittest.TestCase):
    @staticmethod
    def _parse(frames: list) -> list:
        out = []
        for frame in frames:
            assert frame.startswith("data: ")
            payload = frame[len("data: "):].rstrip("\n")
            out.append("[DONE]" if payload == "[DONE]" else json.loads(payload))
        return out

    def test_text_frame_sequence(self):
        resp = {"type": "text", "content": "hi there", "stop_reason": "end_turn"}
        frames = list(stream_chat_response(resp, model="m", request_id="r1"))
        events = self._parse(frames)
        self.assertEqual(events[0]["choices"][0]["delta"], {"role": "assistant"})
        self.assertEqual(events[1]["choices"][0]["delta"], {"content": "hi there"})
        self.assertEqual(events[2]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(events[2]["choices"][0]["delta"], {})
        self.assertEqual(events[3], "[DONE]")

    def test_tool_call_frame_sequence(self):
        resp = {"type": "tool_call", "tool": "shell", "args": {"cmd": "ls"}, "content": ""}
        frames = list(stream_chat_response(resp, model="m", request_id="r1"))
        events = self._parse(frames)
        self.assertEqual(events[0]["choices"][0]["delta"], {"role": "assistant"})
        tool_delta = events[1]["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(tool_delta["function"]["name"], "shell")
        self.assertEqual(json.loads(tool_delta["function"]["arguments"]), {"cmd": "ls"})
        self.assertEqual(events[2]["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(events[3], "[DONE]")

    def test_empty_text_reply_skips_content_frame(self):
        resp = {"type": "text", "content": ""}
        frames = list(stream_chat_response(resp, model="m", request_id="r1"))
        events = self._parse(frames)
        # role delta -> finish chunk -> [DONE], no empty content delta in between.
        self.assertEqual(len(events), 3)
        self.assertEqual(events[1]["choices"][0]["finish_reason"], "stop")


if __name__ == "__main__":
    unittest.main()

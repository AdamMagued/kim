"""Unit tests for codex_engine/responses_streaming.py and reasoning SSE events."""

import json
import unittest

from codex_engine.engine import (
    _make_responses_text_reply,
    _make_responses_tool_reply,
    _make_sse_response,
)


def _parse_sse_frames(resp) -> list:
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


class ResponsesStreamingUnitTests(unittest.TestCase):

    def test_reasoning_item_sse_framing(self):
        reply = _make_responses_text_reply("resp_test123", "Reasoning step 1")
        resp = _make_sse_response(reply)
        events = _parse_sse_frames(resp)

        event_types = [ev if ev == "[DONE]" else ev["type"] for ev in events]
        self.assertIn("response.output_item.added", event_types)
        self.assertIn("response.reasoning.text.delta", event_types)
        self.assertIn("response.reasoning.text.done", event_types)
        self.assertIn("response.output_item.done", event_types)
        self.assertIn("response.completed", event_types)
        self.assertEqual(events[-1], "[DONE]")

        # Check reasoning item added structure
        added = next(ev for ev in events if isinstance(ev, dict) and ev["type"] == "response.output_item.added")
        self.assertEqual(added["item"]["type"], "reasoning")
        self.assertEqual(added["item"]["status"], "in_progress")

        # Check reasoning delta
        delta = next(ev for ev in events if isinstance(ev, dict) and ev["type"] == "response.reasoning.text.delta")
        self.assertEqual(delta["delta"], "Reasoning step 1")

    def test_tool_reply_includes_reasoning_and_function_call(self):
        tools = [{"type": "function", "name": "shell", "parameters": {}}]
        tool_calls = [{"name": "shell", "input": {"cmd": "ls"}}]
        reply = _make_responses_tool_reply("resp_tool123", "Executing command", tool_calls, tools)

        resp = _make_sse_response(reply)
        events = _parse_sse_frames(resp)

        reasoning_deltas = [ev for ev in events if isinstance(ev, dict) and ev["type"] == "response.reasoning.text.delta"]
        fn_deltas = [ev for ev in events if isinstance(ev, dict) and ev["type"] == "response.function_call_arguments.delta"]

        self.assertTrue(len(reasoning_deltas) >= 1)
        self.assertTrue(len(fn_deltas) >= 1)
        self.assertIn("ls", fn_deltas[0]["delta"])


if __name__ == "__main__":
    unittest.main()

"""Golden translation tests for the codex proxy.

Canned browser reply -> _provider_response_to_responses_api -> _make_sse_response,
asserting the exact SSE frame sequence Codex consumes. Codex builds its item
stream from the granular response.output_item.* events (a bare
response.completed is ignored), so the framing itself is load-bearing.
"""

import json
import unittest

from codex_engine.engine import (
    _make_sse_response,
    _provider_response_to_responses_api,
    _sse_or_json,
)

# A request tool list with a real "shell" exec tool, as codex sends it. Needed
# for the salvage path: bash fences are salvaged as name "exec" and only
# normalized onto the real tool when the request declares one.
SHELL_TOOLS = [
    {
        "type": "function",
        "name": "shell",
        "parameters": {
            "type": "object",
            "properties": {"cmd": {"type": "string"}},
            "required": ["cmd"],
        },
    }
]


def _sse_events(resp) -> list:
    """Parse an SSE web.Response body into a list of event dicts (+ '[DONE]')."""
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


class ContractToolCallGoldenTests(unittest.TestCase):
    """Contract JSON with one shell tool_call -> full SSE frame sequence."""

    CONTENT = (
        '{"text": "Listing the files first.", '
        '"tool_calls": [{"name": "shell", "input": {"cmd": "ls -la"}}]}'
    )

    def _reply(self) -> dict:
        return _provider_response_to_responses_api(
            {"type": "text", "content": self.CONTENT},
            relay_num=1,
            request_tools=SHELL_TOOLS,
        )

    def test_responses_dict_shape(self):
        reply = self._reply()
        self.assertEqual(reply["object"], "response")
        self.assertEqual(reply["status"], "completed")
        self.assertEqual(len(reply["output"]), 2)
        message, call = reply["output"]
        self.assertEqual(message["type"], "message")
        self.assertEqual(
            message["content"][0]["text"], "Listing the files first."
        )
        self.assertEqual(call["type"], "function_call")
        self.assertEqual(call["name"], "shell")
        self.assertEqual(json.loads(call["arguments"]), {"cmd": "ls -la"})
        self.assertTrue(call["call_id"].startswith("call_"))

    def test_sse_frame_sequence(self):
        reply = self._reply()
        events = _sse_events(_make_sse_response(reply))
        self.assertEqual(
            _event_types(events),
            [
                "response.created",
                # message item: added -> text delta -> text done -> item done
                "response.output_item.added",
                "response.output_text.delta",
                "response.output_text.done",
                "response.output_item.done",
                # function_call item: added -> args delta -> args done -> item done
                "response.output_item.added",
                "response.function_call_arguments.delta",
                "response.function_call_arguments.done",
                "response.output_item.done",
                "response.completed",
                "[DONE]",
            ],
        )

    def test_created_frame_is_in_progress(self):
        reply = self._reply()
        events = _sse_events(_make_sse_response(reply))
        created = events[0]
        self.assertEqual(created["response"]["status"], "in_progress")
        self.assertEqual(created["response"]["id"], reply["id"])

    def test_function_call_frames_carry_name_and_arguments(self):
        reply = self._reply()
        events = _sse_events(_make_sse_response(reply))
        added = events[5]
        self.assertEqual(added["item"]["type"], "function_call")
        self.assertEqual(added["item"]["name"], "shell")
        # Arguments stream via the delta frames; the added frame is empty.
        self.assertEqual(added["item"]["arguments"], "")
        self.assertEqual(added["item"]["status"], "in_progress")

        delta, done, item_done = events[6], events[7], events[8]
        self.assertEqual(json.loads(delta["delta"]), {"cmd": "ls -la"})
        self.assertEqual(json.loads(done["arguments"]), {"cmd": "ls -la"})
        self.assertEqual(delta["item_id"], added["item"]["id"])
        self.assertEqual(item_done["item"]["status"], "completed")
        self.assertEqual(
            json.loads(item_done["item"]["arguments"]), {"cmd": "ls -la"}
        )

    def test_completed_frame_matches_reply_dict(self):
        reply = self._reply()
        events = _sse_events(_make_sse_response(reply))
        completed = events[-2]
        self.assertEqual(completed["type"], "response.completed")
        self.assertEqual(completed["response"], reply)

    def test_response_headers(self):
        resp = _make_sse_response(self._reply())
        self.assertEqual(resp.content_type, "text/event-stream")
        self.assertEqual(resp.headers["Cache-Control"], "no-cache")


class ContractTextOnlyGoldenTests(unittest.TestCase):
    """Contract JSON with text only -> message item frames + [DONE]."""

    CONTENT = '{"text": "The port is 8123 per config.yaml.", "tool_calls": []}'

    def test_message_frames_and_done_sentinel(self):
        reply = _provider_response_to_responses_api(
            {"type": "text", "content": self.CONTENT}, relay_num=1
        )
        self.assertEqual(len(reply["output"]), 1)
        self.assertEqual(reply["output"][0]["type"], "message")

        events = _sse_events(_make_sse_response(reply))
        self.assertEqual(
            _event_types(events),
            [
                "response.created",
                "response.output_item.added",
                "response.output_text.delta",
                "response.output_text.done",
                "response.output_item.done",
                "response.completed",
                "[DONE]",
            ],
        )
        delta = events[2]
        self.assertEqual(delta["delta"], "The port is 8123 per config.yaml.")
        self.assertEqual(delta["content_index"], 0)
        self.assertEqual(events[3]["text"], "The port is 8123 per config.yaml.")
        self.assertEqual(events[-1], "[DONE]")


class SalvageBashFenceGoldenTests(unittest.TestCase):
    """Prose with a ```bash fence -> salvaged shell function_call."""

    CONTENT = (
        "Sure — let's check what's in the directory first.\n"
        "```bash\nls -la\n```\n"
        "Paste the output back and I'll continue."
    )

    def test_salvaged_command_becomes_shell_function_call(self):
        reply = _provider_response_to_responses_api(
            {"type": "text", "content": self.CONTENT},
            relay_num=1,
            request_tools=SHELL_TOOLS,
        )
        calls = [i for i in reply["output"] if i["type"] == "function_call"]
        self.assertEqual(len(calls), 1)
        # "exec" (the salvage default) is normalized onto the request's tool.
        self.assertEqual(calls[0]["name"], "shell")
        self.assertEqual(json.loads(calls[0]["arguments"]), {"cmd": "ls -la"})
        # Salvage suppresses the prose: activity lines narrate it instead.
        self.assertEqual(
            [i for i in reply["output"] if i["type"] == "message"], []
        )

    def test_salvaged_call_streams_as_function_call_frames(self):
        reply = _provider_response_to_responses_api(
            {"type": "text", "content": self.CONTENT},
            relay_num=1,
            request_tools=SHELL_TOOLS,
        )
        events = _sse_events(_make_sse_response(reply))
        self.assertEqual(
            _event_types(events),
            [
                "response.created",
                "response.output_item.added",
                "response.function_call_arguments.delta",
                "response.function_call_arguments.done",
                "response.output_item.done",
                "response.completed",
                "[DONE]",
            ],
        )
        self.assertIn("ls -la", events[2]["delta"])


class DoneMarkerGoldenTests(unittest.TestCase):
    """A standalone DONE line ends the turn as text, marker stripped."""

    def test_done_marker_is_stripped_from_text_reply(self):
        content = "Built the pong game — arrow keys to move.\nDONE"
        reply = _provider_response_to_responses_api(
            {"type": "text", "content": content}, relay_num=2
        )
        self.assertEqual(len(reply["output"]), 1)
        item = reply["output"][0]
        self.assertEqual(item["type"], "message")
        text = item["content"][0]["text"]
        self.assertEqual(text, "Built the pong game — arrow keys to move.")
        self.assertNotIn("DONE", text)

        events = _sse_events(_make_sse_response(reply))
        self.assertEqual(
            events[2]["delta"], "Built the pong game — arrow keys to move."
        )


class NonStreamJsonGoldenTests(unittest.TestCase):
    """stream=False -> plain JSON response, body round-trips to the reply."""

    def test_sse_or_json_returns_json_when_not_streaming(self):
        reply = _provider_response_to_responses_api(
            {"type": "text", "content": '{"text": "hi", "tool_calls": []}'},
            relay_num=1,
        )
        resp = _sse_or_json(False, reply)
        self.assertEqual(resp.content_type, "application/json")
        raw = resp.body
        self.assertIsInstance(raw, (bytes, bytearray))
        assert isinstance(raw, (bytes, bytearray))
        self.assertEqual(json.loads(bytes(raw).decode()), reply)

    def test_sse_or_json_returns_sse_when_streaming(self):
        reply = _provider_response_to_responses_api(
            {"type": "text", "content": '{"text": "hi", "tool_calls": []}'},
            relay_num=1,
        )
        resp = _sse_or_json(True, reply)
        self.assertEqual(resp.content_type, "text/event-stream")
        events = _sse_events(resp)
        self.assertEqual(events[0]["type"], "response.created")
        self.assertEqual(events[-1], "[DONE]")


if __name__ == "__main__":
    unittest.main()

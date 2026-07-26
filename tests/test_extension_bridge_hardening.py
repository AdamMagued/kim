"""Regression tests for the Chrome-Extension bridge and the codex proxy's
multi-turn state handling (audit/kim-engine-hardening).

Each test pins one defect found during the end-to-end audit:

  * pending futures were never failed when the extension went away, so every
    caller blocked for the full 180s send timeout after a tab close / reload;
  * a failed ``send_json`` leaked the request's future and delta callback into
    ``pending_requests``/``streaming_callbacks`` forever;
  * a raising delta callback escaped the WebSocket read loop and disconnected
    the extension for every other in-flight request;
  * a ``clear_chat=True`` side-call (compaction's summarizer) overwrote the
    live thread's conversation/parent-message pointers, so every turn after a
    compaction silently continued inside the summarizer's throwaway chat;
  * a *different* codex session_id reused the previous session's relay cursor;
  * the extension bridge answered requests aimed at a non-chatgpt site;
  * ``_check_auth`` accepted any string beginning with "Bearer " (and an empty
    header), making the per-run token a no-op;
  * ``_normalize_tool_calls`` never populated ``by_name``, which made the
    schema coercion + jsonschema validation below it unreachable;
  * ``_extract_shell_blocks``'s fallback matched a command verb anywhere in a
    prose line, executing narration as a shell command.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from orchestrator.providers.browser import extension_bridge as eb


class _FakeWS:
    """Minimal stand-in for aiohttp's WebSocketResponse."""

    def __init__(self, fail_send: bool = False):
        self.closed = False
        self.sent: list[dict] = []
        self._fail_send = fail_send

    async def send_json(self, payload):
        if self._fail_send:
            raise ConnectionResetError("socket went away")
        self.sent.append(payload)

    async def close(self):
        self.closed = True


def _bridge_with_ws(ws) -> eb.ExtensionBridgeServer:
    bridge = eb.ExtensionBridgeServer()
    bridge.active_ws = ws
    bridge.bridge_ready = True
    bridge._connected_event.set()
    return bridge


class PendingRequestLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_fails_pending_instead_of_hanging(self):
        bridge = _bridge_with_ws(_FakeWS())
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        bridge.pending_requests["r1"] = fut
        bridge.streaming_callbacks["r1"] = lambda _d: None

        bridge._fail_pending(RuntimeError("extension disconnected"))

        self.assertTrue(fut.done())
        with self.assertRaises(RuntimeError):
            fut.result()
        # Maps must not accrete across reconnect cycles.
        self.assertEqual(bridge.pending_requests, {})
        self.assertEqual(bridge.streaming_callbacks, {})

    async def test_failed_send_does_not_leak_request_state(self):
        bridge = _bridge_with_ws(_FakeWS(fail_send=True))

        with self.assertRaises(ConnectionResetError):
            await bridge.send_completion("hi", on_delta=lambda _d: None, timeout=1.0)

        self.assertEqual(bridge.pending_requests, {})
        self.assertEqual(bridge.streaming_callbacks, {})

    async def test_timeout_cancels_the_browser_turn(self):
        ws = _FakeWS()
        bridge = _bridge_with_ws(ws)

        with self.assertRaises(asyncio.TimeoutError):
            await bridge.send_completion("hi", timeout=0.01)

        cancels = [m for m in ws.sent if m.get("type") == "cancel"]
        self.assertEqual(len(cancels), 1, "a timed-out request must tell the tab to stop")
        self.assertEqual(bridge.pending_requests, {})


class DeltaCallbackIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_raising_delta_callback_does_not_kill_other_requests(self):
        bridge = _bridge_with_ws(_FakeWS())
        loop = asyncio.get_running_loop()

        def boom(_delta):
            raise ValueError("consumer blew up")

        bridge.streaming_callbacks["bad"] = boom
        other = loop.create_future()
        bridge.pending_requests["good"] = other

        # Must not raise — the frame dispatcher swallows consumer errors.
        bridge._handle_response_frame(
            {"type": "response", "requestId": "bad", "event": "delta", "delta": "x"}
        )
        self.assertNotIn("bad", bridge.streaming_callbacks)

        bridge._handle_response_frame(
            {"type": "response", "requestId": "good", "event": "done", "fullText": "ok"}
        )
        self.assertEqual((await other)["fullText"], "ok")

    async def test_unknown_request_id_frame_is_ignored(self):
        bridge = _bridge_with_ws(_FakeWS())
        bridge._handle_response_frame(
            {"type": "response", "requestId": "nobody", "event": "done"}
        )
        bridge._handle_response_frame({"type": "response", "event": "done"})  # no id


class ThreadPointerPreservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_clear_chat_side_call_does_not_steal_the_live_thread(self):
        bridge = eb.ExtensionBridgeServer()
        bridge.restore_thread_state("conv-real", "msg-real")

        with mock.patch.object(eb, "_bridge_server", bridge):
            async with eb.preserved_thread_state():
                # What a clear_chat=True summarizer turn does to the pointers.
                bridge._current_conversation_id = "conv-throwaway"
                bridge._current_message_id = "msg-throwaway"

        self.assertEqual(
            bridge.snapshot_thread_state(), ("conv-real", "msg-real"),
            "the user's thread pointers must survive a background side-call",
        )

    async def test_preserved_thread_state_is_a_noop_without_a_bridge(self):
        with mock.patch.object(eb, "_bridge_server", None):
            async with eb.preserved_thread_state():
                pass

    async def test_clear_chat_drops_stored_pointers_for_a_real_new_chat(self):
        ws = _FakeWS()
        bridge = _bridge_with_ws(ws)
        bridge.restore_thread_state("conv-old", "msg-old")

        task = asyncio.create_task(
            bridge.send_completion("new task", clear_chat=True, timeout=5.0)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        payload = next(m for m in ws.sent if m.get("type") == "request")
        self.assertNotIn("conversationId", payload)
        self.assertNotIn("parentMessageId", payload)

        bridge._handle_response_frame({
            "type": "response", "requestId": payload["requestId"], "event": "done",
            "fullText": "ok", "conversationId": "conv-new", "messageId": "msg-new",
        })
        await task
        self.assertEqual(bridge.snapshot_thread_state(), ("conv-new", "msg-new"))

    async def test_continuing_turn_reuses_stored_pointers(self):
        ws = _FakeWS()
        bridge = _bridge_with_ws(ws)
        bridge.restore_thread_state("conv-1", "msg-1")

        task = asyncio.create_task(bridge.send_completion("next turn", timeout=5.0))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        payload = next(m for m in ws.sent if m.get("type") == "request")
        self.assertEqual(payload["conversationId"], "conv-1")
        self.assertEqual(payload["parentMessageId"], "msg-1")

        bridge._handle_response_frame({
            "type": "response", "requestId": payload["requestId"], "event": "done",
            "fullText": "ok", "conversationId": "conv-1", "messageId": "msg-2",
        })
        await task
        self.assertEqual(bridge.snapshot_thread_state(), ("conv-1", "msg-2"))


class ExtensionSiteRoutingTests(unittest.TestCase):
    def test_only_chatgpt_routes_through_the_extension(self):
        from orchestrator.providers.browser.bridge_client import _extension_bridge_serves

        self.assertTrue(_extension_bridge_serves(None))
        self.assertTrue(_extension_bridge_serves("chatgpt"))
        self.assertTrue(_extension_bridge_serves("chatgpt:gpt-5.6-sol"))
        # The extension posts to chatgpt.com's /backend-api/conversation only —
        # answering a gemini/deepseek request from it is a silent provider swap.
        self.assertFalse(_extension_bridge_serves("gemini"))
        self.assertFalse(_extension_bridge_serves("deepseek"))


class ProxyAuthTests(unittest.TestCase):
    @staticmethod
    def _proxy():
        from codex_engine.engine import _CodexProxy
        return _CodexProxy(provider=object(), provider_name="browser:chatgpt")

    @staticmethod
    def _req(auth):
        import types
        headers = {} if auth is None else {"Authorization": auth}
        return types.SimpleNamespace(headers=headers)

    def test_missing_or_wrong_token_is_rejected(self):
        proxy = self._proxy()
        self.assertFalse(proxy._check_auth(self._req(None)))
        self.assertFalse(proxy._check_auth(self._req("")))
        self.assertFalse(proxy._check_auth(self._req("Bearer ")))
        # The regression that made the per-run token (#47) a no-op.
        self.assertFalse(proxy._check_auth(self._req("Bearer anything-at-all")))

    def test_correct_token_is_accepted(self):
        proxy = self._proxy()
        self.assertTrue(proxy._check_auth(self._req(f"Bearer {proxy._bearer_token}")))


class SessionIdIsolationTests(unittest.IsolatedAsyncioTestCase):
    """A new codex session must not inherit the previous session's cursor."""

    class _Provider:
        def __init__(self):
            self.calls = []

        async def complete(self, messages, tools=None, system=None, **kw):
            self.calls.append({"messages": messages, **kw})
            import json as _json
            return {"type": "text", "content": _json.dumps({"text": "ok"})}

    @staticmethod
    def _request(proxy, body):
        import types

        async def _json():
            return body

        return types.SimpleNamespace(
            headers={"Authorization": f"Bearer {proxy._bearer_token}"},
            content_type="application/json",
            json=_json,
        )

    @staticmethod
    def _body(session_id, texts):
        return {
            "session_id": session_id,
            "input": [{"role": "user", "content": t} for t in texts],
            "tools": [],
        }

    async def _proxy(self):
        from codex_engine.engine import _CodexProxy
        provider = self._Provider()
        return _CodexProxy(
            provider, provider_name="browser:chatgpt", thread_state={}, stateful=False
        ), provider

    async def test_session_change_resets_the_relay_cursor(self):
        proxy, provider = await self._proxy()

        await proxy._handle_responses(
            self._request(proxy, self._body("sess-A", ["first task", "more", "more2"]))
        )
        self.assertEqual(proxy._last_sent_count, 3)

        # A different codex session with a similar-length item list: without
        # the session check, detect_conversation_reset saw no reset and the
        # second session's first turn was treated as session A's 4th relay.
        await proxy._handle_responses(
            self._request(proxy, self._body("sess-B", ["brand new task", "x", "y"]))
        )
        self.assertEqual(proxy._relay_count, 1, "a new session starts a new relay budget")
        self.assertEqual(proxy._current_codex_session_id, "sess-B")
        # Full context (not a delta) went to the browser on the new session.
        self.assertIn("brand new task", provider.calls[-1]["messages"][0]["content"])
        self.assertTrue(provider.calls[-1]["clear_chat"])

    async def test_same_session_keeps_its_state(self):
        proxy, _provider = await self._proxy()
        await proxy._handle_responses(
            self._request(proxy, self._body("sess-A", ["task"]))
        )
        await proxy._handle_responses(
            self._request(proxy, self._body("sess-A", ["task", "Continue."]))
        )
        self.assertEqual(proxy._current_codex_session_id, "sess-A")
        self.assertGreater(proxy._relay_count, 1)


class ToolNormalizationTests(unittest.TestCase):
    """`by_name` was never populated, so everything below it was dead code."""

    _TOOLS = [{
        "type": "function",
        "name": "exec_command",
        "parameters": {
            "type": "object",
            "properties": {"cmd": {"type": "string"}, "workdir": {"type": "string"}},
            "required": ["cmd"],
        },
    }]

    def test_request_tool_names_are_registered(self):
        from codex_engine.engine import _normalize_tool_calls

        calls = _normalize_tool_calls(
            [{"name": "shell", "input": {"command": "ls -la"}}], self._TOOLS
        )
        self.assertEqual(calls[0]["name"], "exec_command")
        self.assertEqual(calls[0]["input"]["cmd"], "ls -la")

    def test_schema_validation_actually_runs(self):
        """F-H-7 validation sat behind `by_name.get(...)`, which was always
        None — so a malformed input reached codex unchecked."""
        import jsonschema
        from codex_engine.engine import _normalize_tool_calls

        with self.assertRaises(jsonschema.ValidationError):
            _normalize_tool_calls(
                [{"name": "exec_command", "input": {"nonsense": "x"}}], self._TOOLS
            )

    def test_no_request_tools_is_a_passthrough(self):
        from codex_engine.engine import _normalize_tool_calls

        original = [{"name": "whatever", "input": {"cmd": "ls"}}]
        self.assertEqual(_normalize_tool_calls(original, None), original)


class ShellFenceFallbackTests(unittest.TestCase):
    def test_prose_narration_is_not_executed_as_a_command(self):
        from codex_engine.engine import _extract_shell_blocks

        # The greedy `re.search(r"\b(cat|open|...)\b.*$")` fallback lifted a
        # command verb out of ordinary prose and ran the rest of the sentence.
        for prose in (
            "Sure — you can open the file in your editor when you're ready.",
            "I'll create a cat picture generator for you.",
            "Next we should touch base about the API design.",
        ):
            self.assertEqual(_extract_shell_blocks(prose), [], prose)

    def test_real_bash_fences_still_win(self):
        from codex_engine.engine import _extract_shell_blocks

        self.assertEqual(
            _extract_shell_blocks("Writing it now.\n```bash\nprintf 'x' > a.txt\n```"),
            ["printf 'x' > a.txt"],
        )

    def test_dangling_fragment_fallback_is_preserved(self):
        from codex_engine.engine import _extract_shell_blocks

        self.assertEqual(_extract_shell_blocks("open pong.html\n```"), ["open pong.html"])


if __name__ == "__main__":
    unittest.main()


class StatelessTitleTests(unittest.TestCase):
    """Codex Desktop sends user content as a block list, not a bare string."""

    def test_title_extracted_from_block_list_content(self):
        from codex_engine.engine import _generate_stateless_title
        import json as _json

        items = [{
            "role": "user",
            "content": [{
                "type": "input_text",
                "text": "Generate a concise UI title.\nUser prompt: build a pong game in html\nINSTRUCTION: reply with JSON",
            }],
        }]
        title = _json.loads(_generate_stateless_title(items))["title"]
        self.assertNotEqual(title, "Coding Task", "block-list content must be read")
        self.assertIn("pong", title.lower())

    def test_plain_string_content_still_works(self):
        from codex_engine.engine import _generate_stateless_title
        import json as _json

        items = [{"role": "user", "content": "User prompt: refactor the parser\nINSTRUCTION: x"}]
        title = _json.loads(_generate_stateless_title(items))["title"]
        self.assertIn("refactor", title.lower())

    def test_empty_items_are_safe(self):
        from codex_engine.engine import _generate_stateless_title
        import json as _json

        payload = _json.loads(_generate_stateless_title([]))
        self.assertEqual(payload["title"], "Coding Task")


class TitleInterceptionShapeTests(unittest.IsolatedAsyncioTestCase):
    """The interception must answer in the Responses API shape codex parses.

    It was the one branch of _handle_responses that returned the raw provider
    dict ({"type": "text", "content": ...}) instead of a Responses payload, so
    codex read `output[]` and found nothing there.
    """

    class _Provider:
        def __init__(self):
            self.calls = 0

        async def complete(self, *a, **kw):
            self.calls += 1
            return {"type": "text", "content": "{}"}

    async def test_title_reply_is_a_responses_payload_and_skips_the_browser(self):
        import json as _json
        import types
        from codex_engine.engine import _CodexProxy

        provider = self._Provider()
        proxy = _CodexProxy(
            provider, provider_name="browser:chatgpt", thread_state={}, stateful=False
        )
        body = {
            "input": [{
                "role": "user",
                "content": "Generate a concise UI title.\nUser prompt: build a snake game\nINSTRUCTION: json",
            }],
            "tools": [],
        }

        async def _json_body():
            return body

        request = types.SimpleNamespace(
            headers={"Authorization": f"Bearer {proxy._bearer_token}"},
            content_type="application/json",
            json=_json_body,
        )
        resp = await proxy._handle_responses(request)
        payload = _json.loads(resp.body.decode())

        self.assertEqual(payload["object"], "response")
        self.assertEqual(payload["output"][0]["type"], "message")
        title = _json.loads(payload["output"][0]["content"][0]["text"])["title"]
        self.assertIn("snake", title.lower())
        # Stateless: the browser thread must not be touched at all.
        self.assertEqual(provider.calls, 0)
        self.assertEqual(proxy._last_sent_count, 0)


class ExtensionBridgeFailureReportingTests(unittest.IsolatedAsyncioTestCase):
    """A failed extension turn must not fall through to a bridge that isn't there.

    Observed live: a 180s browser timeout fell through to the desktop webview
    path with bridge_url="", so the user's actual error read
    "Bridge /v1/send failed — Request URL is missing an 'http://' or 'https://'
    protocol" — which says nothing about what went wrong.
    """

    async def test_timeout_reports_the_real_cause(self):
        from orchestrator.providers.browser import bridge_client as bc

        async def _boom(**_kw):
            return None, True  # attempted, then failed

        with mock.patch.object(bc, "_try_extension_bridge", _boom):
            result = await bc.complete_via_webview_bridge(
                bridge_url="", bridge_token="", preferred_site="chatgpt", prompt="hi",
            )

        self.assertEqual(result["type"], "text")
        self.assertIn("NEED_HELP", result["content"])
        self.assertIn("Chrome Extension bridge", result["content"])
        self.assertNotIn("http://", result["content"].replace("https://", ""))

    async def test_never_connected_still_falls_through(self):
        """No extension at all is a different case — the desktop bridge owns it."""
        from orchestrator.providers.browser import bridge_client as bc

        async def _absent(**_kw):
            return None, False  # never attempted

        with mock.patch.object(bc, "_try_extension_bridge", _absent):
            result = await bc.complete_via_webview_bridge(
                bridge_url="", bridge_token="", preferred_site="chatgpt", prompt="hi",
            )
        # Falls through to the webview path (which then reports its own error).
        self.assertNotIn("Chrome Extension bridge did not answer", result["content"])

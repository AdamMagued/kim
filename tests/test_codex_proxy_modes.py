"""Tests for _CodexProxy's mode generalization and the two TUI fixes.

Covers:
  * mode="browser-contract" (default) vs mode="chat-passthrough" — the
    latter translates /v1/chat/completions via codex_engine/chat_passthrough.py,
    400s /v1/responses, and skips auto-compaction (codex manages its own
    context there).
  * TUI fix 1 — the per-turn relay budget resets at a new user-turn boundary
    instead of being a lifetime cap across a long-lived proxy process.
  * TUI fix 2 — a fresh (shorter, or discontinuous) input list (codex's
    ``/new``) resets the proxy's whole cached-conversation state.

All in-process against ``_CodexProxy`` directly — no subprocess (see
tests/test_standalone_proxy.py for the standalone kimcli runtime's own
subprocess-level tests).
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from codex_engine.engine import _CodexProxy


class _RecordingBaseProvider:
    """A plain BaseProvider-shaped stand-in — NO clear_chat/handoff kwargs,
    matching real (non-browser) providers like claude.py/openai_provider.py.
    chat-passthrough mode must call complete() with exactly this signature."""

    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0
        self.calls: list[dict] = []

    async def complete(self, messages, tools, system):
        self.calls.append({"messages": messages, "tools": tools, "system": system})
        resp = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return resp


class _RecordingBrowserLikeProvider:
    """Browser-provider stand-in accepting the full complete() kwarg surface
    (clear_chat, handoff, ...) the browser-contract path uses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0
        self.calls: list[dict] = []
        self._sent_system_prompt = True

    async def complete(self, messages, tools, system, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        resp = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return resp


def _request(proxy: _CodexProxy, body: dict):
    return SimpleNamespace(
        headers={"Authorization": f"Bearer {proxy._bearer_token}"},
        json=AsyncMock(return_value=body),
    )


class ModeDefaultsTests(unittest.TestCase):
    """Defaults must be bit-identical to before mode/max_relays existed."""

    def test_default_mode_is_browser_contract(self):
        proxy = _CodexProxy(_RecordingBaseProvider([]), provider_name="claude")
        self.assertEqual(proxy._mode, "browser-contract")

    def test_default_max_relays_is_module_constant(self):
        from codex_engine.engine import MAX_RELAYS
        proxy = _CodexProxy(_RecordingBaseProvider([]))
        self.assertEqual(proxy._max_relays, MAX_RELAYS)

    def test_custom_max_relays_overrides_default(self):
        proxy = _CodexProxy(_RecordingBaseProvider([]), max_relays=3)
        self.assertEqual(proxy._max_relays, 3)

    def test_positional_provider_arg_still_works(self):
        # The __init__ param was renamed browser_provider -> provider; the
        # sole real call site (codex_bridge_service.py) passes it positionally.
        provider = _RecordingBaseProvider([])
        proxy = _CodexProxy(provider, provider_name="claude", thread_state={}, stateful=True)
        self.assertIs(proxy._provider, provider)
        self.assertTrue(proxy._stateful)


class ChatPassthroughModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_responses_endpoint_400s_in_chat_passthrough_mode(self):
        proxy = _CodexProxy(_RecordingBaseProvider([]), mode="chat-passthrough")
        resp = await proxy._handle_responses(_request(proxy, {"input": []}))
        self.assertEqual(resp.status, 400)

    async def test_chat_completions_translates_and_calls_provider(self):
        provider = _RecordingBaseProvider([{"type": "text", "content": "hi"}])
        proxy = _CodexProxy(provider, mode="chat-passthrough")
        body = {
            "model": "gpt-x",
            "messages": [
                {"role": "system", "content": "be nice"},
                {"role": "user", "content": "hello"},
            ],
        }
        resp = await proxy._handle_chat_completions(_request(proxy, body))
        self.assertEqual(resp.status, 200)
        payload = json.loads(resp.body)
        self.assertEqual(payload["choices"][0]["message"]["content"], "hi")
        self.assertEqual(payload["model"], "gpt-x")
        # Plain BaseProvider signature — no clear_chat/handoff kwargs.
        self.assertEqual(len(provider.calls), 1)
        call = provider.calls[0]
        self.assertEqual(call["system"], "be nice")
        self.assertEqual(call["messages"], [{"role": "user", "content": "hello"}])

    async def test_tool_call_reply_becomes_openai_tool_calls(self):
        provider = _RecordingBaseProvider([{"type": "tool_call", "tool": "shell", "args": {"cmd": "ls"}}])
        proxy = _CodexProxy(provider, mode="chat-passthrough")
        body = {"model": "m", "messages": [{"role": "user", "content": "list files"}]}
        resp = await proxy._handle_chat_completions(_request(proxy, body))
        payload = json.loads(resp.body)
        choice = payload["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertEqual(choice["message"]["tool_calls"][0]["function"]["name"], "shell")

    async def test_streaming_chat_completions_response(self):
        provider = _RecordingBaseProvider([{"type": "text", "content": "streamed"}])
        proxy = _CodexProxy(provider, mode="chat-passthrough")
        body = {"model": "m", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
        resp = await proxy._handle_chat_completions(_request(proxy, body))
        self.assertIn("text/event-stream", resp.content_type)
        text = resp.body.decode()
        self.assertIn("streamed", text)
        self.assertTrue(text.rstrip().endswith("data: [DONE]"))

    async def test_compaction_is_skipped_in_chat_passthrough_mode(self):
        # A huge message list would trigger auto-compaction in browser-contract
        # mode; chat-passthrough must never call the (browser-only) compactor.
        provider = _RecordingBaseProvider([{"type": "text", "content": "ok"}])
        proxy = _CodexProxy(provider, mode="chat-passthrough")
        huge_messages = [{"role": "user", "content": "x" * 5000} for _ in range(50)]
        body = {"model": "m", "messages": huge_messages}
        resp = await proxy._handle_chat_completions(_request(proxy, body))
        self.assertEqual(resp.status, 200)
        # No compaction cache entries were ever written.
        self.assertEqual(proxy._compaction_cache, {})


class RelayCapTurnBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """TUI fix 1: the per-turn relay budget resets at a new user-turn boundary
    instead of being a lifetime cap across a long-lived proxy process."""

    async def test_budget_resets_on_new_user_turn_not_on_tool_result_deltas(self):
        provider = _RecordingBrowserLikeProvider([{"type": "text", "content": json.dumps({"text": "ok"})}])
        proxy = _CodexProxy(provider, provider_name="browser:gemini", max_relays=2)

        body1 = {"input": [{"role": "user", "content": "task one"}]}
        r1 = await proxy._handle_responses(_request(proxy, body1))
        self.assertEqual(r1.status, 200)

        body1b = {"input": [
            {"role": "user", "content": "task one"},
            {"type": "function_call_output", "call_id": "c1", "output": "done"},
        ]}
        r1b = await proxy._handle_responses(_request(proxy, body1b))
        self.assertEqual(r1b.status, 200)
        self.assertEqual(proxy._relay_count, 2)

        # A third relay in the SAME turn (only a tool result, no new user
        # message) exceeds max_relays=2.
        body1c = {"input": [
            *body1b["input"],
            {"type": "function_call_output", "call_id": "c2", "output": "done2"},
        ]}
        r1c = await proxy._handle_responses(_request(proxy, body1c))
        self.assertEqual(r1c.status, 429)

        # A genuinely NEW user turn resets the budget, even though the
        # lifetime relay count is already past max_relays.
        body2 = {"input": [*body1c["input"], {"role": "user", "content": "task two, a new request"}]}
        r2 = await proxy._handle_responses(_request(proxy, body2))
        self.assertEqual(r2.status, 200)
        self.assertEqual(proxy._relay_count, 1)

    async def test_single_task_exec_behavior_unchanged_with_default_params(self):
        # Regression guard: one turn, well under the default 50-relay budget,
        # behaves exactly as before (no params passed -> module defaults).
        provider = _RecordingBrowserLikeProvider([{"type": "text", "content": json.dumps({"text": "done"})}])
        proxy = _CodexProxy(provider, provider_name="browser:claude")
        body = {"input": [{"role": "user", "content": "do the thing"}]}
        resp = await proxy._handle_responses(_request(proxy, body))
        self.assertEqual(resp.status, 200)
        self.assertEqual(proxy._relay_count, 1)
        self.assertTrue(provider.calls[0]["kwargs"].get("clear_chat"))


class DeltaCursorResetTests(unittest.IsolatedAsyncioTestCase):
    """TUI fix 2: a fresh (shorter, or discontinuous) input list — codex's
    /new — resets the proxy's whole cached-conversation state."""

    async def test_shorter_input_list_forces_a_fresh_conversation(self):
        provider = _RecordingBrowserLikeProvider([{"type": "text", "content": json.dumps({"text": "ok"})}])
        proxy = _CodexProxy(provider, provider_name="browser:gemini")

        body1 = {"input": [
            {"role": "user", "content": "old task"},
            {"type": "function_call_output", "call_id": "c1", "output": "done"},
        ]}
        r1 = await proxy._handle_responses(_request(proxy, body1))
        self.assertEqual(r1.status, 200)
        self.assertEqual(proxy._last_sent_count, 2)

        body2 = {"input": [{"role": "user", "content": "brand new task after /new"}]}
        r2 = await proxy._handle_responses(_request(proxy, body2))
        self.assertEqual(r2.status, 200)
        self.assertEqual(proxy._last_sent_count, 1)
        self.assertEqual(proxy._relay_count, 1)
        # Fresh conversation -> full-context resend, same as any first relay.
        self.assertTrue(provider.calls[-1]["kwargs"].get("clear_chat"))

    async def test_diverged_first_item_forces_a_fresh_conversation(self):
        provider = _RecordingBrowserLikeProvider([{"type": "text", "content": json.dumps({"text": "ok"})}])
        proxy = _CodexProxy(provider, provider_name="browser:gemini")

        body1 = {"input": [{"role": "user", "content": "old task"}]}
        r1 = await proxy._handle_responses(_request(proxy, body1))
        self.assertEqual(r1.status, 200)

        # Same length-or-longer, but item[0] no longer matches — a
        # discontinuous restart, not organic growth of the same conversation.
        body2 = {"input": [
            {"role": "user", "content": "an unrelated fresh task"},
            {"role": "assistant", "content": "ok"},
        ]}
        r2 = await proxy._handle_responses(_request(proxy, body2))
        self.assertEqual(r2.status, 200)
        self.assertEqual(proxy._last_sent_count, 2)
        self.assertTrue(provider.calls[-1]["kwargs"].get("clear_chat"))


if __name__ == "__main__":
    unittest.main()

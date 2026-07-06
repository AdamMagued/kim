"""Tests for stateful browser threads on the codex bridge / Code-tab route.

Covers the cross-task machinery that lets the Code tab (and CLI code mode)
treat a browser LLM chat as a persistent session instead of replaying the
transcript every task:

  * codex_engine/thread_state.py     — the per-(cwd, provider) sidecar
  * _CodexProxy._note_relay_result   — per-relay turn/token accounting
  * _CodexProxy continuation gating   — stateful delta vs fresh-chat decision
  * _is_thread_send_failure          — degrade-to-fresh-chat trigger
  * codex_bridge_service._compact_browser_thread + /compact control task

These are the pure/unit-testable seams; the live browser send is exercised
manually in the running app (see the stateful-threads memory note).
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from codex_engine import thread_state as ts
from codex_engine.engine import _CodexProxy, _is_thread_send_failure


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class _RecordingProvider:
    """Minimal browser-provider stand-in.

    Accepts the full ``complete`` kwarg surface the proxy uses (clear_chat,
    handoff, …) and records every call so tests can assert what was sent.
    """

    def __init__(self, responses):
        # responses: list of dicts returned in order (loops on the last).
        self._responses = responses
        self._i = 0
        self.calls = []
        self.continuation_marks = 0
        self._sent_system_prompt = True

    def mark_thread_continuation(self):
        self.continuation_marks += 1

    async def complete(self, messages, tools, system, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        resp = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return resp


def _proxy(thread_state=None, stateful=False, responses=None):
    provider = _RecordingProvider(responses or [{"type": "text", "content": "ok"}])
    proxy = _CodexProxy(
        provider,
        provider_name="browser:gemini",
        thread_state=thread_state if thread_state is not None else {},
        stateful=stateful,
    )
    return proxy, provider


# ---------------------------------------------------------------------------
# thread_state.py — the cross-task sidecar
# ---------------------------------------------------------------------------

class TestThreadStateSidecar(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        # Redirect the sidecar dir away from the real kim_sessions/ tree.
        self._patch = patch.object(ts, "_STATE_DIR", Path(self._tmp.name))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_load_missing_returns_empty_dict(self):
        self.assertEqual(ts.load_thread_state("/proj/a", "browser:gemini"), {})

    def test_save_then_load_round_trips_and_stamps_time(self):
        ts.save_thread_state("/proj/a", "browser:gemini", {"turns": 3, "est_tokens": 900})
        loaded = ts.load_thread_state("/proj/a", "browser:gemini")
        self.assertEqual(loaded["turns"], 3)
        self.assertEqual(loaded["est_tokens"], 900)
        self.assertIn("updated_at", loaded)  # save stamps a timestamp

    def test_distinct_cwd_or_provider_do_not_collide(self):
        ts.save_thread_state("/proj/a", "browser:gemini", {"turns": 1})
        ts.save_thread_state("/proj/b", "browser:gemini", {"turns": 2})
        ts.save_thread_state("/proj/a", "browser:claude", {"turns": 3})
        self.assertEqual(ts.load_thread_state("/proj/a", "browser:gemini")["turns"], 1)
        self.assertEqual(ts.load_thread_state("/proj/b", "browser:gemini")["turns"], 2)
        self.assertEqual(ts.load_thread_state("/proj/a", "browser:claude")["turns"], 3)

    def test_reset_clears_accounting_and_carries_handoff(self):
        ts.save_thread_state(
            "/proj/a", "browser:gemini",
            {"sent_instructions": True, "turns": 12, "est_tokens": 90_000, "handoff": None},
        )
        state = ts.reset_thread_state("/proj/a", "browser:gemini", handoff="continue X")
        self.assertFalse(state["sent_instructions"])
        self.assertEqual(state["turns"], 0)
        self.assertEqual(state["est_tokens"], 0)
        self.assertEqual(state["handoff"], "continue X")
        # Persisted, not just returned.
        self.assertEqual(ts.load_thread_state("/proj/a", "browser:gemini")["handoff"], "continue X")

    def test_reset_without_handoff_stores_none(self):
        state = ts.reset_thread_state("/proj/a", "browser:gemini")
        self.assertIsNone(state["handoff"])

    def test_corrupt_sidecar_is_treated_as_empty(self):
        path = ts._state_path("/proj/a", "browser:gemini")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(ts.load_thread_state("/proj/a", "browser:gemini"), {})


# ---------------------------------------------------------------------------
# _CodexProxy._note_relay_result — per-relay accounting
# ---------------------------------------------------------------------------

class TestNoteRelayResult(unittest.TestCase):
    def test_fresh_chat_resets_then_counts_this_turn(self):
        proxy, _ = _proxy(thread_state={"turns": 9, "est_tokens": 50_000}, stateful=True)
        proxy._note_relay_result(
            is_first_relay=True,
            cleared_chat=True,
            consumed_handoff=None,
            response={"usage": {"input": 100, "output": 40}},
        )
        st = proxy._thread_state
        self.assertEqual(st["turns"], 1)            # reset to 0, then +1
        self.assertEqual(st["est_tokens"], 140)     # reset to 0, then +usage
        self.assertTrue(st["sent_instructions"])    # first relay armed the thread

    def test_sent_instructions_only_persisted_in_stateful_mode(self):
        # Legacy (stateful off) clears the chat every task, so it must NOT
        # persist sent_instructions — otherwise the first stateful task later
        # would wrongly skip the system prompt (send a delta into a chat that
        # never received it).
        off, _ = _proxy(thread_state={}, stateful=False)
        off._note_relay_result(
            is_first_relay=True, cleared_chat=True, consumed_handoff=None,
            response={"usage": {}},
        )
        self.assertFalse(off._thread_state.get("sent_instructions"))

        on, _ = _proxy(thread_state={}, stateful=True)
        on._note_relay_result(
            is_first_relay=True, cleared_chat=True, consumed_handoff=None,
            response={"usage": {}},
        )
        self.assertTrue(on._thread_state.get("sent_instructions"))

    def test_continuation_accumulates(self):
        proxy, _ = _proxy(thread_state={"turns": 2, "est_tokens": 500, "sent_instructions": True})
        proxy._note_relay_result(
            is_first_relay=False,
            cleared_chat=False,
            consumed_handoff=None,
            response={"usage": {"input": 30, "output": 10}},
        )
        st = proxy._thread_state
        self.assertEqual(st["turns"], 3)            # 2 + 1
        self.assertEqual(st["est_tokens"], 540)     # 500 + 40

    def test_consumed_handoff_is_cleared(self):
        proxy, _ = _proxy(thread_state={"handoff": "seed", "turns": 0})
        proxy._note_relay_result(
            is_first_relay=True,
            cleared_chat=True,
            consumed_handoff="seed",
            response={"usage": {}},
        )
        self.assertIsNone(proxy._thread_state["handoff"])

    def test_missing_usage_still_counts_the_turn(self):
        proxy, _ = _proxy(thread_state={})
        proxy._note_relay_result(
            is_first_relay=True, cleared_chat=True, consumed_handoff=None,
            response={"type": "text", "content": "no usage key"},
        )
        self.assertEqual(proxy._thread_state["turns"], 1)
        self.assertEqual(proxy._thread_state["est_tokens"], 0)


# ---------------------------------------------------------------------------
# _is_thread_send_failure — degrade-to-fresh-chat trigger
# ---------------------------------------------------------------------------

class TestThreadSendFailureDetection(unittest.TestCase):
    def test_matches_known_failure_signatures(self):
        for content in (
            "Send did not register in the browser editor",
            "No response turn detected after submit",
            "It looks like Kim lost the active browser chat",
        ):
            self.assertTrue(
                _is_thread_send_failure({"type": "text", "content": content}),
                content,
            )

    def test_normal_text_is_not_a_failure(self):
        self.assertFalse(
            _is_thread_send_failure({"type": "text", "content": "Here is the answer."})
        )

    def test_non_text_and_non_dict_are_not_failures(self):
        self.assertFalse(_is_thread_send_failure({"type": "tool_call", "content": "Send did not register"}))
        self.assertFalse(_is_thread_send_failure("Send did not register"))
        self.assertFalse(_is_thread_send_failure(None))


# ---------------------------------------------------------------------------
# _compact_browser_thread — in-thread compaction + handoff seeding
# ---------------------------------------------------------------------------

class TestCompactBrowserThread(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._patch = patch.object(ts, "_STATE_DIR", Path(self._tmp.name))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    async def test_successful_compact_persists_handoff_and_resets(self):
        from orchestrator import codex_bridge_service as svc

        artifact = {
            "summary": "Refactoring the auth module.",
            "next_steps": ["wire the login route", "add a test"],
            "paths": ["auth/login.py"],
        }
        provider = _RecordingProvider([{"type": "text", "content": json.dumps(artifact)}])
        ok, handoff = await svc._compact_browser_thread(provider, "/proj/a", "browser:gemini")

        self.assertTrue(ok)
        self.assertIn("Refactoring the auth module.", handoff)
        self.assertIn("wire the login route", handoff)
        # Delta-only compact: it must not re-inject the system prompt.
        self.assertEqual(provider.continuation_marks, 1)
        # State reset with the handoff carried for the next fresh chat.
        saved = ts.load_thread_state("/proj/a", "browser:gemini")
        self.assertFalse(saved["sent_instructions"])
        self.assertEqual(saved["turns"], 0)
        self.assertIn("Refactoring the auth module.", saved["handoff"])

    async def test_need_help_response_resets_without_handoff(self):
        from orchestrator import codex_bridge_service as svc

        provider = _RecordingProvider(
            [{"type": "text", "content": "NEED_HELP: could not read the thread"}]
        )
        ok, handoff = await svc._compact_browser_thread(provider, "/proj/a", "browser:gemini")
        self.assertFalse(ok)
        self.assertEqual(handoff, "")
        saved = ts.load_thread_state("/proj/a", "browser:gemini")
        self.assertIsNone(saved["handoff"])
        self.assertFalse(saved["sent_instructions"])

    async def test_provider_error_still_resets_to_fresh_chat(self):
        from orchestrator import codex_bridge_service as svc

        class _Boom:
            async def complete(self, *a, **k):
                raise RuntimeError("browser bridge down")

        ok, handoff = await svc._compact_browser_thread(_Boom(), "/proj/a", "browser:gemini")
        self.assertFalse(ok)
        self.assertEqual(handoff, "")
        # Even on failure the thread is reset so the next task starts clean.
        saved = ts.load_thread_state("/proj/a", "browser:gemini")
        self.assertIsNone(saved["handoff"])


# ---------------------------------------------------------------------------
# /compact control-task routing parity with the chat agent
# ---------------------------------------------------------------------------

class TestIsGitRepo(unittest.TestCase):
    """The non-git-repo gate that decides whether Codex needs
    --skip-git-repo-check (codex_bridge_service._is_git_repo)."""

    def test_dir_with_dot_git_is_a_repo(self):
        from orchestrator import codex_bridge_service as svc

        with TemporaryDirectory() as d:
            (Path(d) / ".git").mkdir()
            self.assertTrue(svc._is_git_repo(d))

    def test_nested_dir_walks_up_to_dot_git(self):
        from orchestrator import codex_bridge_service as svc

        with TemporaryDirectory() as d:
            (Path(d) / ".git").mkdir()
            nested = Path(d) / "src" / "deep"
            nested.mkdir(parents=True)
            self.assertTrue(svc._is_git_repo(str(nested)))

    def test_dir_without_dot_git_is_not_a_repo(self):
        from orchestrator import codex_bridge_service as svc

        with TemporaryDirectory() as d:
            # A bare temp dir has no .git anywhere up its tree.
            self.assertFalse(svc._is_git_repo(str(Path(d) / "child")))


class TestContinueOnlyDelta(unittest.TestCase):
    """The keepalive matcher must not swallow real user instructions."""

    @staticmethod
    def _user(text):
        return {"role": "user", "content": text}

    def test_exact_keepalive_matches(self):
        from codex_engine.engine import _is_continue_only_delta

        self.assertTrue(_is_continue_only_delta([self._user("Continue.")]))

    def test_keepalive_with_marker_instruction_matches(self):
        from codex_engine.engine import _is_continue_only_delta

        self.assertTrue(
            _is_continue_only_delta(
                [self._user("Continue. End your reply with [END_OF_RESPONSE_x].")]
            )
        )

    def test_real_user_message_starting_with_continue_does_not_match(self):
        from codex_engine.engine import _is_continue_only_delta

        self.assertFalse(
            _is_continue_only_delta([self._user("Continue working on the game")])
        )

    def test_tool_result_never_matches(self):
        from codex_engine.engine import _is_continue_only_delta

        self.assertFalse(
            _is_continue_only_delta(
                [{"type": "function_call_output", "output": "ok"},
                 self._user("Continue.")]
            )
        )


class TestContinueCachedAccounting(unittest.IsolatedAsyncioTestCase):
    """The cached Continue.-only return must consume the keepalive items —
    otherwise they are re-sent to the browser in every later delta."""

    @staticmethod
    def _request(proxy, body):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        return SimpleNamespace(
            headers={"Authorization": f"Bearer {proxy._bearer_token}"},
            json=AsyncMock(return_value=body),
        )

    @staticmethod
    def _body(last_user_text):
        return {
            "stream": False,
            "input": [
                {"role": "user", "content": "do the thing"},
                {"role": "assistant", "content": "answer"},
                {"role": "user", "content": last_user_text},
            ],
        }

    async def test_cached_return_advances_sent_cursor(self):
        proxy, provider = _proxy()
        proxy._last_sent_count = 2
        proxy._last_proxy_response = {"id": "cached"}
        await proxy._handle_responses(
            self._request(proxy, self._body("Continue."))
        )
        self.assertEqual(proxy._last_sent_count, 3)
        self.assertEqual(provider.calls, [])  # cache hit — no browser send

    async def test_real_followup_is_not_swallowed_by_cache(self):
        # Contract-format reply — prose would (correctly) draw a format nudge
        # and a second browser call (TestContractNudge).
        proxy, provider = _proxy(
            responses=[{"type": "text", "content": json.dumps({"text": "ok"})}]
        )
        proxy._last_sent_count = 2
        proxy._last_proxy_response = {"id": "cached"}
        await proxy._handle_responses(
            self._request(proxy, self._body("Continue working on the game"))
        )
        self.assertEqual(len(provider.calls), 1)  # reached the browser
        self.assertEqual(proxy._last_sent_count, 3)


class TestSseGranularEvents(unittest.TestCase):
    """Codex builds its item stream from granular response.output_item.*
    events — a bare response.completed is silently ignored (verified against
    codex-cli 0.134: the agent message never surfaced and -o wrote an empty
    file). The SSE wrapper must stream every output item."""

    @staticmethod
    def _events(reply):
        from codex_engine.engine import _make_sse_response

        raw = _make_sse_response(reply).body
        assert raw is not None
        body = raw.decode()
        events = []
        for line in body.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                events.append(json.loads(line[len("data: "):]))
        return events, body

    def test_text_reply_streams_item_events(self):
        from codex_engine.engine import _make_responses_text_reply

        events, body = self._events(_make_responses_text_reply("r1", "hello!"))
        types = [e["type"] for e in events]
        self.assertEqual(types[0], "response.created")
        self.assertEqual(types[-1], "response.completed")
        self.assertIn("response.output_item.added", types)
        self.assertIn("response.output_text.delta", types)
        self.assertIn("response.output_text.done", types)
        self.assertIn("response.output_item.done", types)
        delta = next(e for e in events if e["type"] == "response.output_text.delta")
        self.assertEqual(delta["delta"], "hello!")
        done_item = next(
            e for e in events if e["type"] == "response.output_item.done"
        )["item"]
        self.assertEqual(done_item["type"], "message")
        self.assertTrue(done_item.get("id"))
        self.assertTrue(body.rstrip().endswith("data: [DONE]"))

    def test_tool_reply_streams_function_call_arguments(self):
        from codex_engine.engine import _make_responses_tool_reply

        reply = _make_responses_tool_reply(
            "r2", "", [{"name": "shell", "input": {"command": ["echo", "hi"]}}]
        )
        events, _ = self._events(reply)
        types = [e["type"] for e in events]
        self.assertIn("response.function_call_arguments.delta", types)
        self.assertIn("response.function_call_arguments.done", types)
        args_done = next(
            e for e in events if e["type"] == "response.function_call_arguments.done"
        )
        self.assertEqual(
            json.loads(args_done["arguments"]), {"command": ["echo", "hi"]}
        )
        added = next(
            e for e in events if e["type"] == "response.output_item.added"
        )["item"]
        self.assertEqual(added["type"], "function_call")
        self.assertEqual(added["name"], "shell")
        self.assertEqual(added["arguments"], "")  # streams via deltas

    def test_ordering_added_before_deltas_before_done(self):
        from codex_engine.engine import _make_responses_text_reply

        events, _ = self._events(_make_responses_text_reply("r3", "x"))
        types = [e["type"] for e in events]
        self.assertLess(
            types.index("response.output_item.added"),
            types.index("response.output_text.delta"),
        )
        self.assertLess(
            types.index("response.output_text.delta"),
            types.index("response.output_text.done"),
        )
        self.assertLess(
            types.index("response.output_text.done"),
            types.index("response.output_item.done"),
        )
        self.assertLess(
            types.index("response.output_item.done"),
            types.index("response.completed"),
        )


class TestCompactControlTasks(unittest.TestCase):
    def test_compact_control_tasks_recognized(self):
        from orchestrator import codex_bridge_service as svc

        for task in ("/compact", "compact", "__kim_compact_context__"):
            self.assertIn(task, svc._COMPACT_CONTROL_TASKS)

    def test_ordinary_task_is_not_a_compact_control_task(self):
        from orchestrator import codex_bridge_service as svc

        self.assertNotIn("fix the login bug", svc._COMPACT_CONTROL_TASKS)


# ---------------------------------------------------------------------------
# Sandbox-change detection — a stored thread taught "read-only" must not be
# reused after the user grants full access (it keeps refusing writes)
# ---------------------------------------------------------------------------

class TestSandboxFingerprint(unittest.TestCase):
    def test_default_when_env_unset_or_empty(self):
        from orchestrator import codex_bridge_service as svc

        env = {k: v for k, v in os.environ.items() if k != "KIM_CODEX_BYPASS_SANDBOX"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(svc._sandbox_fingerprint(), "default")
        with patch.dict(os.environ, {"KIM_CODEX_BYPASS_SANDBOX": ""}):
            self.assertEqual(svc._sandbox_fingerprint(), "default")

    def test_bypass_when_flag_is_1(self):
        from orchestrator import codex_bridge_service as svc

        with patch.dict(os.environ, {"KIM_CODEX_BYPASS_SANDBOX": "1"}):
            self.assertEqual(svc._sandbox_fingerprint(), "bypass")
        # Whitespace-tolerant, same as the launch-flag check.
        with patch.dict(os.environ, {"KIM_CODEX_BYPASS_SANDBOX": " 1 "}):
            self.assertEqual(svc._sandbox_fingerprint(), "bypass")

    def test_non_1_values_are_default(self):
        from orchestrator import codex_bridge_service as svc

        for value in ("0", "true", "yes"):
            with patch.dict(os.environ, {"KIM_CODEX_BYPASS_SANDBOX": value}):
                self.assertEqual(svc._sandbox_fingerprint(), "default")


class TestThreadSandboxChanged(unittest.TestCase):
    def test_fresh_thread_never_counts_as_changed(self):
        from orchestrator import codex_bridge_service as svc

        self.assertFalse(svc._thread_sandbox_changed({}, "bypass"))
        self.assertFalse(
            svc._thread_sandbox_changed({"sent_instructions": False, "sandbox": "default"}, "bypass")
        )

    def test_legacy_sidecar_without_field_counts_as_default(self):
        from orchestrator import codex_bridge_service as svc

        legacy = {"sent_instructions": True, "turns": 4}
        # The exact bug: thread taught read-only, user relaunches with bypass.
        self.assertTrue(svc._thread_sandbox_changed(legacy, "bypass"))
        self.assertFalse(svc._thread_sandbox_changed(legacy, "default"))

    def test_matching_fingerprint_reuses_thread(self):
        from orchestrator import codex_bridge_service as svc

        state = {"sent_instructions": True, "sandbox": "bypass"}
        self.assertFalse(svc._thread_sandbox_changed(state, "bypass"))
        self.assertTrue(svc._thread_sandbox_changed(state, "default"))


# ---------------------------------------------------------------------------
# Codex tool forwarding — codex sends tool definitions in body["tools"];
# without rendering them the browser model sees no tool list and honesty-
# tuned models refuse ("there are no tools available")
# ---------------------------------------------------------------------------

class TestRenderCodexTools(unittest.TestCase):
    def test_responses_shape_renders_names_descriptions_and_schema(self):
        from codex_engine.engine import _extract_prompt_from_responses_request

        body = {
            "instructions": "You are Codex.",
            "input": [{"role": "user", "content": "make pong"}],
            "tools": [
                {
                    "type": "function",
                    "name": "shell",
                    "description": "Runs a shell command.",
                    "parameters": {"type": "object", "properties": {"command": {"type": "array"}}},
                },
                {"type": "web_search"},
            ],
        }
        prompt = _extract_prompt_from_responses_request(body)
        self.assertIn("[AVAILABLE CODEX TOOLS]", prompt)
        self.assertIn("- shell: Runs a shell command.", prompt)
        self.assertIn('"command"', prompt)  # schema forwarded
        self.assertIn("- web_search", prompt)
        # Section sits between the system prompt and the task.
        self.assertLess(prompt.index("[SYSTEM PROMPT]"), prompt.index("[AVAILABLE CODEX TOOLS]"))
        self.assertLess(prompt.index("[AVAILABLE CODEX TOOLS]"), prompt.index("make pong"))

    def test_chat_completions_nested_shape_is_supported(self):
        from codex_engine.engine import _render_codex_tools

        section = _render_codex_tools([
            {
                "type": "function",
                "function": {
                    "name": "apply_patch",
                    "description": "Applies a diff.",
                    "parameters": {"type": "object"},
                },
            }
        ])
        self.assertIn("- apply_patch: Applies a diff.", section)

    def test_missing_or_empty_tools_render_nothing(self):
        from codex_engine.engine import _extract_prompt_from_responses_request, _render_codex_tools

        self.assertEqual(_render_codex_tools(None), "")
        self.assertEqual(_render_codex_tools([]), "")
        self.assertEqual(_render_codex_tools(["junk", 42]), "")
        prompt = _extract_prompt_from_responses_request(
            {"instructions": "x", "input": [{"role": "user", "content": "hi"}]}
        )
        self.assertNotIn("[AVAILABLE CODEX TOOLS]", prompt)


# ---------------------------------------------------------------------------
# Tool-name normalization — browser models shorten/alias tool names ("exec"
# for exec_command); codex rejects unknown tools, so the proxy snaps them
# onto the real names from the request
# ---------------------------------------------------------------------------

_CODEX_TOOLS = [
    {
        "type": "function",
        "name": "exec_command",
        "description": "Runs a command in a PTY.",
        "parameters": {
            "type": "object",
            "properties": {"cmd": {"type": "string"}, "workdir": {"type": "string"}},
            "required": ["cmd"],
        },
    },
    {"type": "function", "name": "update_plan", "parameters": {"required": ["plan"]}},
    {"type": "web_search"},
]


class TestCoerceContractDict(unittest.TestCase):
    """Multi-object replies: the model emits tool_calls, an orphaned call
    fragment (malformed bracket), and a FABRICATED completion report —
    salvage the actions, drop the role-played epilogue."""

    # Faithful shape of the live ChatGPT reply (unescaped inner quotes,
    # broken ]} after call 1, second call orphaned, fake completion text).
    _LIVE_BLOB = (
        '{"text":"creating pong game file and opening it ",'
        '"tool_calls":[{"name":"exec","input":{"cmd":"cat > index.html << \'EOF\'\\n'
        '<!DOCTYPE html>\\n<html lang="en">\\n'
        '<canvas id="game" width="800" height="500"></canvas>\\nEOF"}}]},'
        '{"name":"exec","input":{"cmd":"open index.html"}}]}'
        '\n\n{"text":"pong game created and opened "}'
    )

    def test_live_double_json_blob_salvages_both_tool_calls(self):
        from codex_engine.engine import _parse_contract

        parsed = _parse_contract(self._LIVE_BLOB)
        self.assertIsInstance(parsed, dict)
        calls = parsed["tool_calls"]
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]["input"]["cmd"].startswith("cat > index.html"))
        self.assertTrue(calls[0]["input"]["cmd"].rstrip().endswith("EOF"))
        self.assertEqual(calls[1]["input"]["cmd"], "open index.html")
        # The fabricated "created and opened" report must NOT become the text.
        self.assertNotIn("created and opened", parsed.get("text", ""))

    def test_live_blob_end_to_end_produces_normalized_function_calls(self):
        from codex_engine.engine import _provider_response_to_responses_api

        reply = _provider_response_to_responses_api(
            {"type": "text", "content": self._LIVE_BLOB},
            relay_num=1,
            request_tools=_CODEX_TOOLS,
        )
        calls = [o for o in reply["output"] if o["type"] == "function_call"]
        self.assertEqual([c["name"] for c in calls], ["exec_command", "exec_command"])
        self.assertIn("open index.html", calls[1]["arguments"])

    def test_text_only_multi_object_falls_back_to_first_text(self):
        from codex_engine.engine import _coerce_contract_dict

        out = _coerce_contract_dict([{"text": "first"}, {"text": "second"}])
        self.assertEqual(out, {"text": "first"})

    def test_list_without_contract_dicts_is_none(self):
        from codex_engine.engine import _coerce_contract_dict

        self.assertIsNone(_coerce_contract_dict(["a", 1]))
        self.assertIsNone(_coerce_contract_dict("prose"))

    def test_plain_dict_passes_through(self):
        from codex_engine.engine import _coerce_contract_dict

        d = {"text": "hi", "tool_calls": []}
        self.assertIs(_coerce_contract_dict(d), d)


class TestShellFenceSalvage(unittest.TestCase):
    """Protocol-refusing models (ChatGPT-web calls the JSON contract an
    'injected format') still write the exact commands in ```bash fences —
    execute what they wrote instead of arguing."""

    # Shape of the live refusal run: prose + heredoc fence + open fence.
    _PROSE = (
        "I'll give you a ready-to-run Pong game.\n\n"
        "Run this in your terminal:\n\n"
        "```bash\ncat > index.html <<'EOF'\n<!doctype html>\n"
        '<canvas id="game" width="800" height="400"></canvas>\nEOF\n```\n\n'
        "Now open it:\n\n```bash\nopen index.html\n```\n\n"
        "You'll get a playable Pong game."
    )

    def test_extracts_only_shell_fences(self):
        from codex_engine.engine import _extract_shell_blocks

        blocks = _extract_shell_blocks(self._PROSE)
        self.assertEqual(len(blocks), 2)
        self.assertTrue(blocks[0].startswith("cat > index.html"))
        self.assertEqual(blocks[1], "open index.html")
        # Non-shell fences are never executed.
        self.assertEqual(
            _extract_shell_blocks("```html\n<b>hi</b>\n```\n```python\nprint(1)\n```"), []
        )
        self.assertEqual(_extract_shell_blocks("no fences here"), [])

    def test_prose_with_fences_becomes_normalized_tool_calls(self):
        from codex_engine.engine import _provider_response_to_responses_api

        reply = _provider_response_to_responses_api(
            {"type": "text", "content": self._PROSE},
            relay_num=1,
            request_tools=_CODEX_TOOLS,
        )
        calls = [o for o in reply["output"] if o["type"] == "function_call"]
        self.assertEqual([c["name"] for c in calls], ["exec_command", "exec_command"])
        self.assertIn("cat > index.html", calls[0]["arguments"])
        self.assertIn("open index.html", calls[1]["arguments"])

    def test_contract_final_answer_with_fences_also_executes(self):
        from codex_engine.engine import _provider_response_to_responses_api

        reply = _provider_response_to_responses_api(
            {"type": "text", "content": json.dumps(
                {"text": "To launch it:\n```bash\nopen index.html\n```"}
            )},
            relay_num=1,
            request_tools=_CODEX_TOOLS,
        )
        calls = [o for o in reply["output"] if o["type"] == "function_call"]
        self.assertEqual(len(calls), 1)
        self.assertIn("open index.html", calls[0]["arguments"])

    def test_prose_without_fences_stays_a_text_reply(self):
        from codex_engine.engine import _provider_response_to_responses_api

        reply = _provider_response_to_responses_api(
            {"type": "text", "content": "Here is how Pong works: paddles bounce a ball."},
            relay_num=1,
            request_tools=_CODEX_TOOLS,
        )
        self.assertEqual(reply["output"][0]["type"], "message")


class TestJsonFenceSalvage(unittest.TestCase):
    """The model writes a literal tool call in a ```json fence ('Run this in
    your Codex environment') instead of emitting it as the reply."""

    # Live shape: bare {"cmd": "bash -lc '...heredoc... open index.html'"}.
    _PROSE = (
        "I can't open a window, but here's the game.\n\n"
        "Run this in your Codex environment:\n\n"
        '```json\n{"cmd":"bash -lc \'cat > index.html << \\"EOF\\"\\n'
        '<!DOCTYPE html>\\n<canvas></canvas>\\nEOF\\nopen index.html\'"}\n```\n\n'
        "After it runs your browser should open."
    )

    def test_bare_cmd_object_becomes_exec_call(self):
        from codex_engine.engine import _extract_json_tool_fences

        calls = _extract_json_tool_fences(self._PROSE)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "exec")
        cmd = calls[0]["input"]["cmd"]
        self.assertIn("cat > index.html", cmd)
        self.assertIn("open index.html", cmd)

    def test_full_contract_in_json_fence(self):
        from codex_engine.engine import _extract_json_tool_fences

        prose = (
            "Here you go:\n```json\n"
            '{"text":"","tool_calls":[{"name":"exec_command","input":{"cmd":"ls"}}]}'
            "\n```"
        )
        calls = _extract_json_tool_fences(prose)
        self.assertEqual(calls, [{"name": "exec_command", "input": {"cmd": "ls"}}])

    def test_single_call_object_in_json_fence(self):
        from codex_engine.engine import _extract_json_tool_fences

        prose = '```json\n{"name":"exec","input":{"cmd":"open x.html"}}\n```'
        calls = _extract_json_tool_fences(prose)
        self.assertEqual(calls[0]["input"]["cmd"], "open x.html")

    def test_non_tool_json_fence_ignored(self):
        from codex_engine.engine import _extract_json_tool_fences

        self.assertEqual(_extract_json_tool_fences('```json\n{"score": 42}\n```'), [])
        self.assertEqual(_extract_json_tool_fences("no fences"), [])

    def test_json_fence_end_to_end_normalizes_to_exec_command(self):
        from codex_engine.engine import _provider_response_to_responses_api

        reply = _provider_response_to_responses_api(
            {"type": "text", "content": self._PROSE},
            relay_num=1,
            request_tools=_CODEX_TOOLS,
        )
        calls = [o for o in reply["output"] if o["type"] == "function_call"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "exec_command")
        self.assertIn("open index.html", calls[0]["arguments"])


class TestFileDirectiveSalvage(unittest.TestCase):
    """'Save this code as X' replies: filename directive + fenced body →
    Kim synthesizes the write+open commands the model refused to emit."""

    # Shape of the live run: html fence (never executed as shell) + explicit
    # filename + "double-click / open with Chrome" instruction.
    _PROSE = (
        "Here's a complete single-file Pong game.\n\n"
        "1. Create a file called: `pong.html`\n"
        "2. Paste the code below into it\n"
        "3. Double-click the file or open it with Chrome\n\n"
        "```html\n<!DOCTYPE html>\n<html lang=\"en\">\n<canvas id=\"game\"></canvas>\n"
        "<script>const ctx = canvas.getContext(\"2d\");</script>\n</html>\n```\n\n"
        "If you want, I can upgrade it with sound effects."
    )

    def test_directive_extracted_with_filename_body_and_open(self):
        from codex_engine.engine import _extract_file_directive

        name, body, wants_open = _extract_file_directive(self._PROSE)
        self.assertEqual(name, "pong.html")
        self.assertTrue(body.startswith("<!DOCTYPE html>"))
        self.assertTrue(wants_open)

    def test_save_as_phrasing_also_matches(self):
        from codex_engine.engine import _extract_file_directive

        prose = "Just save it as `index.html`:\n```html\n<b>hi</b>\n```"
        name, body, wants_open = _extract_file_directive(prose)
        self.assertEqual(name, "index.html")
        self.assertFalse(wants_open)

    def test_no_filename_or_no_fence_returns_none(self):
        from codex_engine.engine import _extract_file_directive

        self.assertIsNone(_extract_file_directive("```html\n<b>hi</b>\n```"))
        self.assertIsNone(_extract_file_directive("Save it as `pong.html` — code coming later."))

    def test_dotfiles_and_paths_are_rejected(self):
        from codex_engine.engine import _extract_file_directive

        for bad in (".env", "../x.sh", "a/b.sh"):
            prose = f"Save it as `{bad}`:\n```\ndata\n```"
            self.assertIsNone(_extract_file_directive(prose), bad)

    def test_prose_reply_becomes_write_and_open_tool_calls(self):
        from codex_engine.engine import _provider_response_to_responses_api

        reply = _provider_response_to_responses_api(
            {"type": "text", "content": self._PROSE},
            relay_num=1,
            request_tools=_CODEX_TOOLS,
        )
        calls = [o for o in reply["output"] if o["type"] == "function_call"]
        self.assertEqual([c["name"] for c in calls], ["exec_command", "exec_command"])
        self.assertIn("cat > pong.html", calls[0]["arguments"])
        self.assertIn("<!DOCTYPE html>", calls[0]["arguments"])
        self.assertIn("pong.html", calls[1]["arguments"])  # the open command

    def test_bash_fence_takes_priority_over_directive(self):
        from codex_engine.engine import _provider_response_to_responses_api

        prose = (
            "Save it as `pong.html`. Or just run:\n"
            "```bash\ncat > pong.html <<'EOF'\n<b>hi</b>\nEOF\n```"
        )
        reply = _provider_response_to_responses_api(
            {"type": "text", "content": prose},
            relay_num=1,
            request_tools=_CODEX_TOOLS,
        )
        calls = [o for o in reply["output"] if o["type"] == "function_call"]
        self.assertEqual(len(calls), 1)  # the explicit command only, no double-write

    def test_heredoc_delimiter_collision_is_avoided(self):
        from codex_engine.engine import _file_directive_tool_calls

        body = "line1\nKIM_EOF_7f3a\nline2"
        calls = _file_directive_tool_calls("x.txt", body, wants_open=False)
        cmd = calls[0]["input"]["cmd"]
        self.assertIn("KIM_EOF_7f3ax", cmd)
        self.assertIn(body, cmd)


class TestDoneReply(unittest.TestCase):
    """A DONE signal ends the turn cleanly instead of salvaging trailing
    'you could also…' chatter into another relay (the browser-chat hang)."""

    def test_bare_done_is_a_final_text_answer(self):
        from codex_engine.engine import _provider_response_to_responses_api

        reply = _provider_response_to_responses_api(
            {"type": "text", "content": "DONE"}, relay_num=1, request_tools=_CODEX_TOOLS
        )
        self.assertEqual(reply["output"][0]["type"], "message")

    def test_done_suppresses_trailing_chatter_command(self):
        from codex_engine.engine import _provider_response_to_responses_api

        # After finishing, the model wraps up with a stray idempotent command.
        # DONE means stop — that block must NOT become another tool call.
        content = "All set.\n```bash\nopen pong.html\n```\nDONE"
        reply = _provider_response_to_responses_api(
            {"type": "text", "content": content}, relay_num=1, request_tools=_CODEX_TOOLS
        )
        self.assertFalse([o for o in reply["output"] if o["type"] == "function_call"])

    def test_done_does_not_swallow_a_real_file_write(self):
        from codex_engine.engine import _provider_response_to_responses_api

        # If the model (misbehaving) says DONE but still includes a file-write,
        # run it — never drop real file creation.
        content = "```bash\nprintf '%s' '<html>' > pong.html\n```\nDONE"
        reply = _provider_response_to_responses_api(
            {"type": "text", "content": content}, relay_num=1, request_tools=_CODEX_TOOLS
        )
        calls = [o for o in reply["output"] if o["type"] == "function_call"]
        self.assertEqual(len(calls), 1)
        self.assertIn("pong.html", calls[0]["arguments"])

    def test_done_inside_json_contract_ends_turn(self):
        from codex_engine.engine import _provider_response_to_responses_api

        reply = _provider_response_to_responses_api(
            {"type": "text", "content": json.dumps({"text": "DONE"})},
            relay_num=1,
            request_tools=_CODEX_TOOLS,
        )
        self.assertEqual(reply["output"][0]["type"], "message")

    def test_word_done_in_prose_is_not_a_done_signal(self):
        from codex_engine.engine import _is_done_reply

        # "done" mid-sentence must not end the turn.
        self.assertFalse(_is_done_reply("I have done the first part, next:"))
        self.assertTrue(_is_done_reply("DONE"))
        self.assertTrue(_is_done_reply("open pong.html\nDONE"))

    def test_done_detection_is_case_insensitive(self):
        from codex_engine.engine import _is_done_reply

        # The real miss: the model wrote "Done." (title case) and the turn
        # didn't end, firing a needless nudge. Both forms must end the turn.
        self.assertTrue(_is_done_reply("Done."))
        self.assertTrue(_is_done_reply("Done"))
        # ...but not when a real command follows/precedes on the same idea.
        self.assertFalse(_is_done_reply("Done, but first run this:"))


class TestChatgptTerminalNudge(unittest.IsolatedAsyncioTestCase):
    async def test_chatgpt_gets_terminal_nudge_not_json_nudge(self):
        from codex_engine.engine import _CodexProxy, _TERMINAL_NUDGE

        provider = _RecordingProvider([{"type": "text", "content": "still just chatting"}])
        proxy = _CodexProxy(
            provider, provider_name="browser:chatgpt", thread_state={}, stateful=False
        )
        original = {"type": "text", "content": "Here's how you'd do it, in theory."}
        await proxy._nudge_contract_retry(original, relay_num=1)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0]["messages"][0]["content"], _TERMINAL_NUDGE)

    async def test_gemini_still_gets_json_nudge(self):
        from codex_engine.engine import _CodexProxy, _CONTRACT_NUDGE

        provider = _RecordingProvider([{"type": "text", "content": "prose only"}])
        proxy = _CodexProxy(
            provider, provider_name="browser:gemini", thread_state={}, stateful=False
        )
        original = {"type": "text", "content": "I'll explain the approach."}
        await proxy._nudge_contract_retry(original, relay_num=1)
        self.assertEqual(provider.calls[0]["messages"][0]["content"], _CONTRACT_NUDGE)


class TestShellFenceNudgeInteraction(unittest.IsolatedAsyncioTestCase):
    async def test_prose_with_fences_skips_the_nudge(self):
        proxy, provider = _proxy()
        original = {"type": "text", "content": "Run:\n```bash\nopen index.html\n```"}
        result = await proxy._nudge_contract_retry(original, relay_num=1)
        self.assertIs(result, original)
        self.assertEqual(provider.calls, [])  # no round trip wasted

    async def test_prose_with_file_directive_skips_the_nudge(self):
        proxy, provider = _proxy()
        original = {"type": "text", "content": (
            "Create a file called `pong.html` and double-click it:\n"
            "```html\n<b>pong</b>\n```"
        )}
        result = await proxy._nudge_contract_retry(original, relay_num=1)
        self.assertIs(result, original)
        self.assertEqual(provider.calls, [])

    async def test_refusal_retry_with_fences_is_used_and_not_burned(self):
        retry = {"type": "text", "content": (
            "I won't use that JSON format. But here you go:\n"
            "```bash\ntouch pong.html\n```"
        )}
        proxy, provider = _proxy(responses=[retry])
        original = {"type": "text", "content": "Here is the code, save it yourself."}
        result = await proxy._nudge_contract_retry(original, relay_num=1)
        self.assertIs(result, retry)
        self.assertFalse(proxy._thread_state.get("burned"))

    async def test_self_help_answer_with_fence_is_not_burned(self):
        retry = {"type": "text", "content": json.dumps(
            {"text": "Then run:\n```bash\nopen index.html\n```"}
        )}
        proxy, provider = _proxy(responses=[retry])
        original = {"type": "text", "content": "Save it as index.html yourself."}
        result = await proxy._nudge_contract_retry(original, relay_num=1)
        self.assertIs(result, retry)
        self.assertFalse(proxy._thread_state.get("burned"))


class TestNormalizeToolCalls(unittest.TestCase):
    def test_exec_prefix_snaps_to_exec_command(self):
        from codex_engine.engine import _normalize_tool_calls

        out = _normalize_tool_calls(
            [{"name": "exec", "input": {"cmd": "open pong.html"}}], _CODEX_TOOLS
        )
        self.assertEqual(out[0]["name"], "exec_command")
        self.assertEqual(out[0]["input"], {"cmd": "open pong.html"})

    def test_shell_alias_snaps_to_exec_command(self):
        from codex_engine.engine import _normalize_tool_calls

        for alias in ("shell", "bash", "run_command", "terminal"):
            out = _normalize_tool_calls([{"name": alias, "input": {"cmd": "ls"}}], _CODEX_TOOLS)
            self.assertEqual(out[0]["name"], "exec_command", alias)

    def test_command_argv_list_coerced_to_cmd_string(self):
        from codex_engine.engine import _normalize_tool_calls

        out = _normalize_tool_calls(
            [{"name": "shell", "input": {"command": ["echo", "hello world"]}}], _CODEX_TOOLS
        )
        self.assertEqual(out[0]["name"], "exec_command")
        self.assertEqual(out[0]["input"], {"cmd": "echo 'hello world'"})

    def test_valid_names_and_inputs_untouched(self):
        from codex_engine.engine import _normalize_tool_calls

        calls = [{"name": "exec_command", "input": {"cmd": "ls"}},
                 {"name": "update_plan", "input": {"plan": []}}]
        out = _normalize_tool_calls(calls, _CODEX_TOOLS)
        self.assertEqual(out, calls)

    def test_unknown_name_without_match_passes_through(self):
        from codex_engine.engine import _normalize_tool_calls

        out = _normalize_tool_calls([{"name": "teleport", "input": {}}], _CODEX_TOOLS)
        self.assertEqual(out[0]["name"], "teleport")

    def test_no_request_tools_is_a_no_op(self):
        from codex_engine.engine import _normalize_tool_calls

        calls = [{"name": "exec", "input": {"cmd": "ls"}}]
        self.assertEqual(_normalize_tool_calls(calls, None), calls)
        self.assertEqual(_normalize_tool_calls(calls, []), calls)

    def test_converter_applies_normalization_end_to_end(self):
        from codex_engine.engine import _provider_response_to_responses_api

        reply = _provider_response_to_responses_api(
            {
                "type": "text",
                "content": json.dumps({
                    "text": "creating file",
                    "tool_calls": [{"name": "exec", "input": {"cmd": "touch pong.html"}}],
                }),
            },
            relay_num=1,
            request_tools=_CODEX_TOOLS,
        )
        call = next(o for o in reply["output"] if o["type"] == "function_call")
        self.assertEqual(call["name"], "exec_command")
        self.assertIn("touch pong.html", call["arguments"])


# ---------------------------------------------------------------------------
# Contract nudge — a prose reply ("I'll create the files…") must get ONE
# format re-ask instead of being handed to codex as a final answer
# ---------------------------------------------------------------------------

class TestContractNudge(unittest.IsolatedAsyncioTestCase):
    async def test_prose_reply_is_retried_and_retry_used(self):
        retry_reply = {
            "type": "text",
            "content": json.dumps({
                "text": "creating the file",
                "tool_calls": [{"name": "shell", "input": {"command": ["touch", "pong.html"]}}],
            }),
        }
        proxy, provider = _proxy(responses=[retry_reply])
        original = {"type": "text", "content": "I'll create the files and open them for you."}

        result = await proxy._nudge_contract_retry(original, relay_num=1)

        self.assertIs(result, retry_reply)
        self.assertEqual(len(provider.calls), 1)
        sent = provider.calls[0]["messages"][0]["content"]
        # Accommodation framing: the nudge asks for the format as the reader's
        # constraint, not as an adversarial "FORMAT ERROR" correction.
        self.assertIn("JSON", sent)
        self.assertIn("tool_calls", sent)
        # Same thread — the nudge must never wipe the chat.
        self.assertFalse(provider.calls[0]["kwargs"]["clear_chat"])

    async def test_contract_reply_is_not_nudged(self):
        proxy, provider = _proxy()
        for content in (
            json.dumps({"text": "final answer"}),
            json.dumps({"text": "", "tool_calls": [{"name": "shell", "input": {}}]}),
        ):
            original = {"type": "text", "content": content}
            result = await proxy._nudge_contract_retry(original, relay_num=1)
            self.assertIs(result, original)
        self.assertEqual(provider.calls, [])

    async def test_failed_retry_keeps_original_and_nudges_only_once(self):
        proxy, provider = _proxy(responses=[{"type": "text", "content": "still prose, sorry"}])
        original = {"type": "text", "content": "Sure, I'll do that right away."}
        result = await proxy._nudge_contract_retry(original, relay_num=1)
        self.assertIs(result, original)
        self.assertEqual(len(provider.calls), 1)
        # Ignoring the explicit nudge burns the thread: refusals compound, so
        # the next task must start a fresh chat instead of resuming this one.
        self.assertTrue(proxy._thread_state.get("burned"))

    async def test_successful_retry_does_not_burn_thread(self):
        proxy, provider = _proxy(
            responses=[{"type": "text", "content": json.dumps({"text": "The answer is 42."})}]
        )
        original = {"type": "text", "content": "Sure, I'll do that right away."}
        await proxy._nudge_contract_retry(original, relay_num=1)
        self.assertFalse(proxy._thread_state.get("burned"))

    async def test_do_it_yourself_final_answer_after_nudge_burns_thread(self):
        # The exact dodge observed live: format-compliant JSON whose text
        # tells the USER to save the file and run the command.
        dodge = {
            "type": "text",
            "content": json.dumps({
                "text": "Save the Pong HTML I provided into a file named pong.html "
                        "in /Users/adammaged/Desktop/test, then run: open pong.html",
            }),
        }
        proxy, provider = _proxy(responses=[dodge])
        original = {"type": "text", "content": "Here is the code. Create pong.html yourself."}
        result = await proxy._nudge_contract_retry(original, relay_num=1)
        self.assertIs(result, dodge)  # answer still surfaces to the user
        self.assertTrue(proxy._thread_state.get("burned"))

    async def test_tool_call_retry_with_self_help_text_is_not_burned(self):
        reply = {
            "type": "text",
            "content": json.dumps({
                "text": "Creating the file, then run happens automatically.",
                "tool_calls": [{"name": "exec_command", "input": {"cmd": "touch pong.html"}}],
            }),
        }
        proxy, provider = _proxy(responses=[reply])
        original = {"type": "text", "content": "I'll set that up."}
        await proxy._nudge_contract_retry(original, relay_num=1)
        self.assertFalse(proxy._thread_state.get("burned"))

    def test_reset_thread_state_clears_burned(self):
        with TemporaryDirectory() as tmp:
            with patch.object(ts, "_STATE_DIR", Path(tmp)):
                ts.save_thread_state("/proj/x", "browser:chatgpt",
                                     {"sent_instructions": True, "burned": True})
                state = ts.reset_thread_state("/proj/x", "browser:chatgpt")
                self.assertNotIn("burned", state)
                self.assertNotIn("burned", ts.load_thread_state("/proj/x", "browser:chatgpt"))

    async def test_provider_error_keeps_original(self):
        class _Boom:
            async def complete(self, *a, **k):
                raise RuntimeError("bridge down")

        proxy = _CodexProxy(_Boom(), provider_name="browser:gemini", thread_state={}, stateful=False)
        original = {"type": "text", "content": "Narrating instead of acting."}
        result = await proxy._nudge_contract_retry(original, relay_num=1)
        self.assertIs(result, original)

    async def test_send_failure_and_non_text_are_not_nudged(self):
        proxy, provider = _proxy()
        failure = {"type": "text", "content": "No response turn detected after submit"}
        self.assertIs(await proxy._nudge_contract_retry(failure, relay_num=1), failure)
        tool = {"type": "tool_call", "content": "irrelevant"}
        self.assertIs(await proxy._nudge_contract_retry(tool, relay_num=1), tool)
        self.assertEqual(provider.calls, [])


# ---------------------------------------------------------------------------
# Relay-reasoning preview must stay on ONE [STATUS] line — the stdout parser
# is line-based, so a newline inside the preview leaks its tail into the
# answer stream (shows up as a truncated "Kim: …" fragment in the CLI)
# ---------------------------------------------------------------------------

class TestRelayReasoningPreview(unittest.TestCase):
    @staticmethod
    def _capture(response):
        import contextlib
        import io

        from codex_engine.engine import _surface_relay_reasoning

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _surface_relay_reasoning(response, relay_num=1)
        return buf.getvalue()

    def test_multiline_content_collapses_to_one_status_line(self):
        content = (
            "Yes—I can generate the code for a Pong game.\n\n"
            "However, I still can't truthfully claim that I've created files "
            "or opened them because I don't have write access."
        )
        out = self._capture({"content": content})
        lines = [line for line in out.splitlines() if line]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("[STATUS] "))
        self.assertIn("However, I still can't", lines[0])

    def test_brand_names_are_masked(self):
        out = self._capture({"content": "ChatGPT will handle this task."})
        self.assertIn("[STATUS] Kim will handle this task.", out)

    def test_json_content_is_not_previewed(self):
        out = self._capture({"content": '  {"text": "raw contract reply"}'})
        self.assertEqual(out, "")

    def test_only_prose_before_code_fence_is_previewed(self):
        # A reply that is prose + a command must preview only the prose — never
        # dump raw "```bash printf '%s' '<!doctype…" into the status line.
        content = (
            "I'll drop a game in your folder and open it.\n\n"
            "```bash\nprintf '%s' '<!doctype html>...' > index.html\n```"
        )
        out = self._capture({"content": content})
        self.assertIn("drop a game", out)
        self.assertNotIn("printf", out)
        self.assertNotIn("```", out)

    def test_pure_code_block_reply_previews_nothing(self):
        out = self._capture({"content": "```bash\nopen index.html\n```"})
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()

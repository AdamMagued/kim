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
        proxy, provider = _proxy()
        proxy._last_sent_count = 2
        proxy._last_proxy_response = {"id": "cached"}
        await proxy._handle_responses(
            self._request(proxy, self._body("Continue working on the game"))
        )
        self.assertEqual(len(provider.calls), 1)  # reached the browser
        self.assertEqual(proxy._last_sent_count, 3)


class TestCompactControlTasks(unittest.TestCase):
    def test_compact_control_tasks_recognized(self):
        from orchestrator import codex_bridge_service as svc

        for task in ("/compact", "compact", "__kim_compact_context__"):
            self.assertIn(task, svc._COMPACT_CONTROL_TASKS)

    def test_ordinary_task_is_not_a_compact_control_task(self):
        from orchestrator import codex_bridge_service as svc

        self.assertNotIn("fix the login bug", svc._COMPACT_CONTROL_TASKS)


if __name__ == "__main__":
    unittest.main()

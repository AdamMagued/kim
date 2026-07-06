"""Stateful browser threads: system-prompt-once + in-chat compact-and-rollover.

Covers:
- format_prompt handoff seeding on the first send of a fresh thread
- compact_messages summary override (model-written handoff replaces local summary)
- the in-thread compact prompt (transcript-free) + handoff rendering
- KimAgent thread decision (fresh per session vs continue, nonce lifecycle)
- rollover triggers (context ratio / turn cap) and the rollover state machine
- the reused-thread send-failure fallback inside the run loop
- __init__ config plumbing and persisted-state restore
"""
from __future__ import annotations

import asyncio
import json
import re
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import make_test_agent

from orchestrator import compaction
from orchestrator.compact_prompt import (
    build_in_thread_compact_prompt,
    render_handoff_text,
)
from orchestrator.providers.browser.prompt_builder import format_prompt


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _event_loop_guard():
    """Provide a fresh event loop per test (mirrors other suites here)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield
    loop.close()


# ---------------------------------------------------------------------------
# Fake browser provider — class NAME matters (_is_browser_provider matches it)
# ---------------------------------------------------------------------------

class BrowserProvider:
    """Scriptable stand-in whose class name matches the real provider."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls: list[dict] = []
        self.continued = False
        self.reset_count = 0

    def reset_session(self):
        self.reset_count += 1

    def mark_thread_continuation(self):
        self.continued = True

    async def complete(self, messages, tools=None, system="",
                       clear_chat=False, handoff=None, **kwargs):
        self.calls.append({
            "messages": messages,
            "system": system,
            "clear_chat": clear_chat,
            "handoff": handoff,
        })
        if not self.responses:
            return {"type": "text", "content": "TASK_COMPLETE: default"}
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _make_stateful_agent(provider, **overrides):
    """Factory agent pre-wired for stateful browser-thread tests."""
    agent = make_test_agent(provider=provider)
    agent._browser_stateful = True
    agent._browser_compact_at_ratio = 0.80
    agent._browser_max_thread_turns = 40
    agent._browser_thread_turns = 0
    agent._browser_thread_nonce = ""
    agent._browser_continuing_thread = False
    agent._pending_handoff = None
    agent._thread_send_failures = 0
    agent._log = lambda level, msg: None
    agent._persist_context_state_extra = lambda *a, **kw: None
    agent._print_context_json = lambda snapshot: None
    agent._track_context_usage = lambda *a, **kw: None
    agent._context_meter = MagicMock()
    agent._context_meter.budget = 100_000
    agent._context_meter.cumulative_input = 0
    agent._context_meter.reset_after_compact.return_value.to_log_line.return_value = "ctx"
    for key, value in overrides.items():
        setattr(agent, key, value)
    return agent


# ---------------------------------------------------------------------------
# format_prompt — handoff seeding
# ---------------------------------------------------------------------------

def _fmt(messages, *, handoff=None, sent=False, system="SYS"):
    return format_prompt(
        messages,
        [{"name": "run_command", "description": "d", "parameters": {}}],
        system,
        sent_system_prompt=sent,
        max_inject_chars=120_000,
        use_webview_bridge=True,
        handoff_summary=handoff,
    )


def test_handoff_block_included_on_first_send():
    messages = [
        {"role": "user", "content": "Task: earlier"},
        {"role": "assistant", "content": "did a thing"},
        {"role": "user", "content": "[Tool result: run_command]\nok"},
    ]
    prompt, _, _, sent = _fmt(messages, handoff="HANDOFF-TEXT next: finish x")
    assert "[HANDOFF FROM PREVIOUS CHAT" in prompt
    assert "HANDOFF-TEXT next: finish x" in prompt
    assert "[END HANDOFF]" in prompt
    assert "[SYSTEM]" in prompt
    assert sent is True


def test_handoff_precedes_history_recap():
    messages = [
        {"role": "user", "content": "Task: earlier"},
        {"role": "assistant", "content": "assistant reply text"},
        {"role": "user", "content": "next question"},
    ]
    prompt, _, _, _ = _fmt(messages, handoff="THE-HANDOFF")
    handoff_pos = prompt.index("THE-HANDOFF")
    recap_pos = prompt.index("[PRIOR CONVERSATION")
    assert handoff_pos < recap_pos


def test_handoff_precedes_system_prompt():
    # The handoff must appear BEFORE the (large) [SYSTEM] block so a fresh chat
    # anchors on the prior conversation instead of the operating instructions.
    messages = [{"role": "user", "content": "what did we discuss?"}]
    prompt, _, _, _ = _fmt(messages, handoff="THE-HANDOFF", system="BIG SYSTEM PROMPT")
    assert prompt.index("THE-HANDOFF") < prompt.index("[SYSTEM]")


def test_no_handoff_block_when_none():
    messages = [{"role": "user", "content": "Task: t"}]
    prompt, _, _, _ = _fmt(messages)
    assert "HANDOFF" not in prompt


def test_no_handoff_after_system_prompt_sent():
    messages = [
        {"role": "user", "content": "Task: t"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "follow-up"},
    ]
    prompt, _, _, _ = _fmt(messages, handoff="LATE-HANDOFF", sent=True)
    assert "LATE-HANDOFF" not in prompt
    assert "[SYSTEM]" not in prompt
    assert "follow-up" in prompt


def test_handoff_block_in_codex_bridge_prompt():
    messages = [{"role": "user", "content": "please continue"}]
    prompt, _, _, _ = _fmt(
        messages,
        handoff="CODEX-HANDOFF",
        system="You are Codex, a coding agent.",
    )
    assert "CODEX-HANDOFF" in prompt
    assert "[AVAILABLE TOOLS]" not in prompt  # codex branch has no tool JSON


# ---------------------------------------------------------------------------
# compaction — summary override
# ---------------------------------------------------------------------------

def _history(n=12):
    out = []
    for i in range(n):
        out.append({"role": "user", "content": f"user message {i}"})
        out.append({"role": "assistant", "content": f"assistant reply {i}"})
    return out


def test_compact_messages_uses_summary_override():
    compacted = compaction.compact_messages(_history(), summary_text="MODEL WROTE THIS")
    assert compacted[0]["role"] == "compact_summary"
    assert "MODEL WROTE THIS" in compacted[0]["content"]
    # Recent tail preserved verbatim
    assert compacted[-1]["content"] == "assistant reply 11"


def test_compact_messages_blank_override_falls_back_to_local():
    compacted = compaction.compact_messages(_history(), summary_text="   ")
    assert compacted[0]["role"] == "compact_summary"
    # Local deterministic summary sections appear instead
    assert "## " in compacted[0]["content"]


# ---------------------------------------------------------------------------
# compact_prompt — in-thread prompt + handoff rendering
# ---------------------------------------------------------------------------

def test_in_thread_compact_prompt_carries_no_transcript():
    prompt = build_in_thread_compact_prompt()
    assert "Transcript:" not in prompt
    assert "conversation above" in prompt
    assert '"summary"' in prompt


def test_render_handoff_text_sections():
    text = render_handoff_text({
        "summary": "Half-way through renaming the module.",
        "decisions": ["keep the old API"],
        "paths": ["orchestrator/agent.py"],
        "next_steps": ["update imports", "run tests"],
        "open_questions": [],
        "need_help": None,
    })
    assert "Half-way through renaming the module." in text
    assert "Decisions:\n- keep the old API" in text
    assert "Key files/paths:\n- orchestrator/agent.py" in text
    assert "Next steps:\n- update imports\n- run tests" in text
    assert "Open questions" not in text
    assert "Blockers" not in text


def test_render_handoff_text_empty_artifact():
    assert render_handoff_text({}) == ""


# ---------------------------------------------------------------------------
# _decide_browser_thread
# ---------------------------------------------------------------------------

def test_decide_first_task_starts_fresh_thread():
    provider = BrowserProvider()
    agent = _make_stateful_agent(provider)
    agent._decide_browser_thread(has_prior_history=False)
    assert agent._clear_chat_on_next_call is True
    assert agent._browser_continuing_thread is False
    assert re.fullmatch(r"[0-9a-f]{32}", agent._browser_thread_nonce)
    assert provider.continued is False


def test_decide_continues_thread_with_history_and_nonce():
    provider = BrowserProvider()
    agent = _make_stateful_agent(provider)
    agent._browser_thread_nonce = "a" * 32
    agent._browser_thread_turns = 7
    agent._decide_browser_thread(has_prior_history=True)
    assert agent._clear_chat_on_next_call is False
    assert agent._browser_continuing_thread is True
    assert agent._browser_thread_nonce == "a" * 32   # preserved
    assert agent._browser_thread_turns == 7          # preserved
    assert provider.continued is True                # provider told directly


def test_decide_missing_nonce_forces_fresh_thread():
    provider = BrowserProvider()
    agent = _make_stateful_agent(provider)
    agent._browser_thread_nonce = ""
    agent._decide_browser_thread(has_prior_history=True)
    assert agent._clear_chat_on_next_call is True
    assert agent._browser_continuing_thread is False
    assert agent._browser_thread_nonce != ""


def test_decide_needs_fresh_chat_flag_starts_fresh():
    """A pending /compact (needs_fresh_chat) overrides thread continuation."""
    provider = BrowserProvider()
    agent = _make_stateful_agent(provider)
    agent._browser_thread_nonce = "b" * 32
    agent._clear_chat_on_next_call = True
    agent._decide_browser_thread(has_prior_history=True)
    assert agent._browser_continuing_thread is False
    assert agent._browser_thread_nonce != "b" * 32   # fresh thread → fresh nonce


def test_decide_legacy_mode_clears_stale_nonce():
    provider = BrowserProvider()
    agent = _make_stateful_agent(provider, _browser_stateful=False)
    agent._browser_thread_nonce = "stale"
    agent._decide_browser_thread(has_prior_history=True)
    assert agent._browser_thread_nonce == ""
    assert agent._browser_continuing_thread is False


def test_decide_non_browser_provider_is_noop():
    agent = make_test_agent()  # FakeProvider
    agent._browser_stateful = True
    agent._browser_thread_nonce = "leftover"
    agent._decide_browser_thread(has_prior_history=True)
    assert agent._browser_thread_nonce == ""
    assert agent._browser_continuing_thread is False


def test_wrap_task_for_thread_uses_thread_nonce():
    agent = _make_stateful_agent(BrowserProvider())
    agent._browser_thread_nonce = "c" * 32
    wrapped = agent._wrap_task_for_thread("do the thing")
    assert wrapped == (
        f"<<<BEGIN_USER_INSTRUCTION_{'c' * 32}>>>\ndo the thing\n"
        f"<<<END_USER_INSTRUCTION_{'c' * 32}>>>"
    )


# ---------------------------------------------------------------------------
# rollover triggers
# ---------------------------------------------------------------------------

def test_should_rollover_on_context_ratio():
    agent = _make_stateful_agent(BrowserProvider())
    agent.memory.add_user("hi")
    agent._context_meter.cumulative_input = 85_000
    assert agent._should_rollover_browser_thread() is True


def test_should_rollover_on_turn_cap():
    agent = _make_stateful_agent(BrowserProvider())
    agent.memory.add_user("hi")
    agent._browser_thread_turns = 40
    assert agent._should_rollover_browser_thread() is True


def test_should_not_rollover_below_thresholds():
    agent = _make_stateful_agent(BrowserProvider())
    agent.memory.add_user("hi")
    agent._context_meter.cumulative_input = 10_000
    agent._browser_thread_turns = 3
    assert agent._should_rollover_browser_thread() is False


def test_should_not_rollover_with_empty_memory():
    agent = _make_stateful_agent(BrowserProvider())
    agent._context_meter.cumulative_input = 99_000
    assert agent._should_rollover_browser_thread() is False


# ---------------------------------------------------------------------------
# _rollover_browser_thread
# ---------------------------------------------------------------------------

def _artifact_response():
    return {
        "type": "text",
        "content": json.dumps({
            "summary": "Renamed half the modules; imports still broken.",
            "decisions": ["use absolute imports"],
            "paths": ["orchestrator/agent.py"],
            "open_questions": [],
            "need_help": [],
            "next_steps": ["fix imports in tests"],
        }),
    }


def test_rollover_uses_model_written_handoff():
    provider = BrowserProvider(responses=[_artifact_response()])
    agent = _make_stateful_agent(provider)
    for msg in _history(10):
        agent.memory.load_from_messages(_history(10))

    _run(agent._rollover_browser_thread("SYSTEM-PROMPT"))

    # In-thread compact request carried no transcript
    sent = provider.calls[0]["messages"][0]["content"]
    assert "Transcript:" not in sent
    assert "conversation above" in sent
    # Fresh chat armed and seeded with the model-written handoff
    assert agent._clear_chat_on_next_call is True
    assert agent._pending_handoff is not None
    assert "Renamed half the modules" in agent._pending_handoff
    assert "fix imports in tests" in agent._pending_handoff
    assert agent._browser_thread_turns == 0
    # Kim-side memory now pins the same summary
    assert agent.memory.compact_summary is not None
    assert "Renamed half the modules" in agent.memory.compact_summary
    # Artifact persisted
    assert agent._session_store.save_compact_artifact.called
    saved = agent._session_store.save_compact_artifact.call_args[0][0]
    assert saved["kind"] == "kim_browser_thread_rollover"
    # Meter reset
    assert agent._context_meter.reset_after_compact.called


def test_rollover_falls_back_to_local_summary_on_provider_failure():
    provider = BrowserProvider(responses=[RuntimeError("thread died")])
    agent = _make_stateful_agent(provider)
    agent._max_retries = 1
    agent.memory.load_from_messages(_history(10))

    _run(agent._rollover_browser_thread("SYSTEM-PROMPT"))

    assert agent._clear_chat_on_next_call is True
    # Local deterministic handoff still produced
    assert agent._pending_handoff
    assert agent.memory.compact_summary is not None
    # No artifact was written (nothing model-written to save)
    assert not agent._session_store.save_compact_artifact.called


def test_rollover_never_raises_even_if_local_compaction_fails():
    provider = BrowserProvider(responses=[RuntimeError("dead")])
    agent = _make_stateful_agent(provider)
    agent._max_retries = 1
    # Empty memory → compact_messages raises ValueError internally
    _run(agent._rollover_browser_thread("SYSTEM-PROMPT"))
    assert agent._clear_chat_on_next_call is True
    assert agent._pending_handoff is None


# ---------------------------------------------------------------------------
# run-loop integration: fresh thread per session, send-failure fallback
# ---------------------------------------------------------------------------

def _wire_for_run(agent):
    """Stub the environment-touching pieces so run() exercises the real loop."""
    agent._resume_session_id = None
    agent._log = lambda level, msg: None
    agent._is_cancelled = lambda: False
    agent._emit_plan_markers = lambda _content: None
    agent._complete_run = lambda result: result
    agent._emit_context_snapshot = lambda: None
    agent._build_system_prompt = lambda task, **kw: "system"
    agent._persist_context_state_extra = lambda *a, **kw: None
    agent._track_context_usage = lambda *a, **kw: None
    agent._print_context_json = lambda snapshot: None
    agent._drain_steers = lambda: None
    agent._steer_inbox = []

    async def _refresh_tools():
        pass

    agent._refresh_tools = _refresh_tools

    async def _generate_and_save_summary(task, summary):
        pass

    agent._generate_and_save_summary = _generate_and_save_summary
    return agent


def test_run_stateful_first_task_fresh_thread_and_no_legacy_clear():
    provider = BrowserProvider(responses=[
        {"type": "text", "content": "TASK_COMPLETE: hello"},
    ])
    agent = _wire_for_run(_make_stateful_agent(provider))

    result = _run(agent.run("say hello"))

    assert result["success"] is True
    assert len(provider.calls) == 1
    # First task of a session starts a fresh thread even in stateful mode
    assert provider.calls[0]["clear_chat"] is True
    assert provider.calls[0]["handoff"] is None
    # Flag consumed — a follow-up call in this run would reuse the thread
    assert agent._clear_chat_on_next_call is False


def test_run_legacy_mode_still_clears_every_message():
    provider = BrowserProvider(responses=[
        {"type": "text", "content": "TASK_COMPLETE: hi"},
    ])
    agent = _wire_for_run(_make_stateful_agent(provider, _browser_stateful=False))

    result = _run(agent.run("say hi"))

    assert result["success"] is True
    assert provider.calls[0]["clear_chat"] is True


def test_run_send_failure_retries_once_on_fresh_chat_with_handoff():
    provider = BrowserProvider(responses=[
        {"type": "text", "content": (
            "NEED_HELP: In-app browser execution failed — Send did not "
            "register — prompt still in the input after 40 polls"
        )},
        {"type": "text", "content": "TASK_COMPLETE: recovered"},
    ])
    agent = _wire_for_run(_make_stateful_agent(provider))
    # Continuing session: resumed history + persisted thread nonce
    agent._browser_thread_nonce = "d" * 32
    agent._resume_session_id = "sess-0"

    with patch("orchestrator.agent.SessionStore.session_exists", return_value=True), \
         patch("orchestrator.agent.SessionStore.load_session", return_value=_history(2)):
        result = _run(agent.run("second task"))

    assert result["success"] is True
    assert result["summary"] == "recovered"
    assert len(provider.calls) == 2
    # Failed call went to the reused thread (no clear)
    assert provider.calls[0]["clear_chat"] is False
    # Retry went to a FRESH chat seeded with a locally-built handoff
    assert provider.calls[1]["clear_chat"] is True
    assert provider.calls[1]["handoff"]
    assert agent._thread_send_failures == 1


def test_run_second_send_failure_surfaces_need_help():
    failure = {"type": "text", "content": (
        "NEED_HELP: In-app browser execution failed — Send did not register"
    )}
    provider = BrowserProvider(responses=[dict(failure), dict(failure)])
    agent = _wire_for_run(_make_stateful_agent(provider))
    agent._browser_thread_nonce = "e" * 32
    agent.memory.load_from_messages(_history(2))
    # Continuing session (history present via resume path is stubbed, so force
    # the decision the way run() would see it)
    agent._resume_session_id = "sess-1"

    with patch("orchestrator.agent.SessionStore.session_exists", return_value=True), \
         patch("orchestrator.agent.SessionStore.load_session", return_value=_history(2)):
        result = _run(agent.run("doomed task"))

    assert result["success"] is False
    assert "NEED_HELP" in result["summary"]
    assert len(provider.calls) == 2
    assert agent._thread_send_failures == 2


def test_run_continuing_task_wraps_task_in_thread_markers():
    provider = BrowserProvider(responses=[
        {"type": "text", "content": "TASK_COMPLETE: ok"},
    ])
    agent = _wire_for_run(_make_stateful_agent(provider))
    agent._browser_thread_nonce = "f" * 32
    agent._resume_session_id = "sess-2"

    with patch("orchestrator.agent.SessionStore.session_exists", return_value=True), \
         patch("orchestrator.agent.SessionStore.load_session", return_value=_history(2)):
        result = _run(agent.run("follow-up task"))

    assert result["success"] is True
    # Thread continued: no clear, provider told the system prompt is in place
    assert provider.calls[0]["clear_chat"] is False
    assert provider.continued is True
    # The delta message wraps the task in the persisted thread markers
    last_message = provider.calls[0]["messages"][-1]["content"]
    assert f"<<<BEGIN_USER_INSTRUCTION_{'f' * 32}>>>" in last_message
    assert "follow-up task" in last_message
    assert f"<<<END_USER_INSTRUCTION_{'f' * 32}>>>" in last_message


# ---------------------------------------------------------------------------
# __init__ plumbing
# ---------------------------------------------------------------------------

def _real_agent(config_extra=None, context_state=None):
    from orchestrator.agent import KimAgent
    from orchestrator.providers.fake import FakeProvider

    config = {
        "max_iterations": 5,
        "context_budget_tokens": 100_000,
    }
    config.update(config_extra or {})
    store = MagicMock()
    store.load_context_state.return_value = context_state or {}
    return KimAgent(
        config=config,
        session=MagicMock(),
        provider=FakeProvider(),
        session_store=store,
    )


def test_init_defaults_stateful_off():
    agent = _real_agent()
    assert agent._browser_stateful is False
    assert agent._browser_compact_at_ratio == 0.80
    assert agent._browser_max_thread_turns == 40
    assert agent._browser_thread_turns == 0
    assert agent._browser_thread_nonce == ""


def test_init_reads_config_and_persisted_state():
    agent = _real_agent(
        config_extra={
            "browser_provider": {
                "stateful_threads": True,
                "compact_at_ratio": 0.7,
                "max_thread_turns": 25,
            }
        },
        context_state={
            "browser_thread_turns": 9,
            "browser_thread_nonce": "9" * 32,
        },
    )
    assert agent._browser_stateful is True
    assert agent._browser_compact_at_ratio == 0.7
    assert agent._browser_max_thread_turns == 25
    assert agent._browser_thread_turns == 9
    assert agent._browser_thread_nonce == "9" * 32

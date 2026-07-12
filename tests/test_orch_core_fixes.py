"""Regression tests for the orchestrator-core fixes (branch fix/orch-core).

Covers:
- 4.1 stateful-thread context meter accumulates per-turn deltas so the
  ratio-based browser-thread rollover can actually fire.
- 4.3 local compaction summaries keep a content-bearing snippet of tool-result
  bodies instead of only the "[Tool result: …]" header line.
- 4.4 compaction and context_meter share ONE token estimator.
- 4.5 reset_after_compact snapshots the post-compaction context size instead
  of persisting a fictional 0.
- Error-classification honesty: only genuine rate limits emit the typed
  rate_limited event; network/timeout/server errors get honest labels.
"""

import asyncio

import pytest

from orchestrator import compaction
from orchestrator.compaction import _estimate_message_tokens, _summarize_messages
from orchestrator.context_meter import (
    ContextMeter,
    estimate_content_tokens,
    estimate_request_tokens,
)
from tests.conftest import make_test_agent


# ---------------------------------------------------------------------------
# 4.1 — accumulate mode for stateful browser threads
# ---------------------------------------------------------------------------

class TestContextMeterAccumulate:
    def test_add_input_default_keeps_last_request_semantics(self):
        """Stateless providers: cumulative_input tracks the LAST request size."""
        meter = ContextMeter(budget=10_000)
        meter.add_input(4_000, source="api", estimated=False)
        snap = meter.add_input(3_000, source="api", estimated=False)
        assert snap.cumulative_input == 3_000  # replaced, not summed

    def test_add_input_accumulate_sums_deltas_and_outputs(self):
        """Stateful browser threads: deltas + replies both fill the thread."""
        meter = ContextMeter(budget=10_000)
        meter.add_input(1_000, source="browser", estimated=True, output_tokens=200, accumulate=True)
        snap = meter.add_input(
            500, source="browser", estimated=True, output_tokens=100, accumulate=True
        )
        assert snap.cumulative_input == 1_000 + 200 + 500 + 100

    def test_observe_usage_passes_accumulate_through(self):
        meter = ContextMeter(budget=10_000)
        meter.observe_usage({"input": 700, "output": 50, "estimated": True}, accumulate=True)
        snap = meter.observe_usage(
            {"input": 300, "output": 10, "estimated": True}, accumulate=True
        )
        assert snap is not None
        assert snap.cumulative_input == 700 + 50 + 300 + 10

    def test_ratio_rollover_fires_after_accumulated_deltas(self):
        """The 4.1 end-to-end regression: many small deltas must eventually
        cross compact_at_ratio even though each individual delta is tiny."""

        class BrowserProvider:  # name is what _is_browser_provider checks
            pass

        agent = make_test_agent(provider=BrowserProvider())
        agent._log = lambda level, msg: None
        agent._context_meter = ContextMeter(budget=1_000)
        agent._browser_stateful = True
        agent._browser_compact_at_ratio = 0.80
        agent._browser_max_thread_turns = 999
        agent._browser_thread_turns = 1
        agent._browser_thread_nonce = "nonce"
        agent.memory.add_user("hi")

        usage = {"input": 300, "output": 50, "estimated": True}
        agent._track_context_usage(usage, fallback_source="BrowserProvider", accumulate=True)
        assert agent._should_rollover_browser_thread() is False  # 350 / 1000

        agent._track_context_usage(usage, fallback_source="BrowserProvider", accumulate=True)
        agent._track_context_usage(usage, fallback_source="BrowserProvider", accumulate=True)
        # 1050 / 1000 >= 0.80 — with the old assignment semantics this would
        # still read 300/1000 and never fire.
        assert agent._should_rollover_browser_thread() is True


# ---------------------------------------------------------------------------
# 4.3 — tool-result bodies survive local summarization
# ---------------------------------------------------------------------------

class TestSummaryKeepsToolResultBodies:
    def _tool_call(self, tool="web_text"):
        return {
            "role": "assistant",
            "content": f'{{"type": "tool_call", "tool": "{tool}", "args": {{}}}}',
        }

    def test_body_content_is_kept(self):
        messages = [
            {"role": "user", "content": "What is the capital of Freedonia?"},
            self._tool_call(),
            {
                "role": "user",
                "content": "[Tool result: web_text]\nThe capital of Freedonia is Zembla.\nPopulation 1M.",
            },
        ]
        summary = _summarize_messages(messages)
        # Old behavior kept only the first line "[Tool result: web_text]".
        assert "Zembla" in summary

    def test_long_bodies_are_truncated(self):
        body = "[Tool result: read_file]\n" + ("x" * 5_000)
        messages = [self._tool_call("read_file"), {"role": "user", "content": body}]
        summary = _summarize_messages(messages)
        assert "…[truncated]" in summary
        # The full 5k-char body must not be embedded verbatim.
        assert "x" * (compaction.TOOL_RESULT_SNIPPET_CHARS + 10) not in summary


# ---------------------------------------------------------------------------
# 4.4 — one tokenizer
# ---------------------------------------------------------------------------

class TestUnifiedTokenizer:
    def test_compaction_delegates_to_context_meter(self):
        # Word-heavy text is exactly where the old len//4+1 heuristic and the
        # meter's max(chars/4, words*1.3) diverged by >30%.
        text = " ".join(["go"] * 400)
        msg = {"role": "user", "content": text}
        assert _estimate_message_tokens(msg) == estimate_content_tokens(text)

    def test_multimodal_content_matches_meter(self):
        content = [
            {"type": "text", "text": "hello world " * 50},
            {"type": "image", "source": {"data": "…"}},
        ]
        msg = {"role": "user", "content": content}
        assert _estimate_message_tokens(msg) == estimate_content_tokens(content)


# ---------------------------------------------------------------------------
# 4.5 — reset_after_compact snapshots the post-compaction size
# ---------------------------------------------------------------------------

class TestResetAfterCompact:
    def test_reset_seeds_new_cumulative_input(self):
        meter = ContextMeter(budget=100_000, cumulative_input=50_000)
        snap = meter.reset_after_compact(
            compacted_at="2026-01-01T00:00:00Z", new_cumulative_input=1_200
        )
        assert meter.cumulative_input == 1_200
        assert snap.cumulative_input == 1_200
        assert snap.estimated is True  # it is an estimate, and labelled as one
        assert snap.last_compact_at == "2026-01-01T00:00:00Z"

    def test_reset_default_is_zero_and_not_estimated(self):
        meter = ContextMeter(budget=100_000, cumulative_input=50_000)
        snap = meter.reset_after_compact(compacted_at="2026-01-01T00:00:00Z")
        assert meter.cumulative_input == 0
        assert snap.estimated is False

    def test_arm_fresh_browser_thread_seeds_meter_from_memory(self):
        """After a rollover the meter reflects the summary+tail the fresh
        chat will re-send — not a fictional 0."""

        class BrowserProvider:
            pass

        agent = make_test_agent(provider=BrowserProvider())
        agent._log = lambda level, msg: None
        agent._context_meter = ContextMeter(budget=100_000, cumulative_input=90_000)
        agent._browser_stateful = True
        agent._browser_thread_turns = 7
        agent._browser_thread_nonce = "nonce"
        agent._pending_handoff = None
        agent.memory.add_user("a compacted summary tail " * 40)

        agent._arm_fresh_browser_thread("handoff text")

        expected = estimate_request_tokens(agent.memory.get_messages())
        assert expected > 0
        assert agent._context_meter.cumulative_input == expected
        assert agent._browser_thread_turns == 0
        assert agent._clear_chat_on_next_call is True


# ---------------------------------------------------------------------------
# Error-classification honesty in _call_with_retry
# ---------------------------------------------------------------------------

class _FlakyProvider:
    """Fails N times with the given exception, then succeeds."""

    def __init__(self, exc: Exception, fail_times: int = 1):
        self._exc = exc
        self._fails_left = fail_times

    async def complete(self, messages, tools, system, **kwargs):
        if self._fails_left > 0:
            self._fails_left -= 1
            raise self._exc
        return {"type": "text", "content": "ok", "usage": {}}


def _run_retry(agent):
    return asyncio.run(agent._call_with_retry(messages=[], tools=[], system="s"))


class TestHonestRetryLabels:
    def _agent(self, exc, monkeypatch):
        agent = make_test_agent(provider=_FlakyProvider(exc))
        agent._max_retries = 3
        agent._retry_base_delay = 0.0
        agent._retry_max_delay = 0.001
        logs: list[str] = []
        agent._log = lambda level, msg: logs.append(msg)
        rate_limited_calls: list[tuple] = []
        monkeypatch.setattr(
            "orchestrator.agent.emit_rate_limited",
            lambda *args: rate_limited_calls.append(args),
        )
        return agent, logs, rate_limited_calls

    def test_network_error_is_not_labelled_rate_limited(self, monkeypatch):
        agent, logs, rate_limited = self._agent(ConnectionError("connection reset"), monkeypatch)
        response = _run_retry(agent)
        assert response["content"] == "ok"
        assert rate_limited == []  # the dishonest banner must NOT fire
        status_lines = [line for line in logs if line.startswith("[STATUS]")]
        assert any("Connection problem" in line for line in status_lines)
        assert not any("Rate-limited" in line for line in logs)

    def test_timeout_error_gets_timeout_label(self, monkeypatch):
        agent, logs, rate_limited = self._agent(TimeoutError("read timeout"), monkeypatch)
        _run_retry(agent)
        assert rate_limited == []
        assert any("Provider timed out" in line for line in logs)

    def test_server_error_gets_server_label(self, monkeypatch):
        agent, logs, rate_limited = self._agent(RuntimeError("HTTP 503 overloaded"), monkeypatch)
        _run_retry(agent)
        assert rate_limited == []
        assert any("Provider server error" in line for line in logs)

    def test_real_rate_limit_still_emits_typed_event(self, monkeypatch):
        agent, logs, rate_limited = self._agent(RuntimeError("429 rate limit exceeded"), monkeypatch)
        _run_retry(agent)
        assert len(rate_limited) == 1
        delay, attempt, max_retries = rate_limited[0]
        assert attempt == 1 and max_retries == 3

    def test_retry_notice_label_mapping(self):
        from orchestrator.agent import _retry_notice_label

        assert "Connection problem" in _retry_notice_label("network")
        assert "timed out" in _retry_notice_label("timeout")
        assert "server error" in _retry_notice_label("server_error")
        assert "unknown" in _retry_notice_label("unknown")

    def test_non_retryable_error_still_raises(self, monkeypatch):
        agent, logs, rate_limited = self._agent(PermissionError("invalid api key"), monkeypatch)
        with pytest.raises(PermissionError):
            _run_retry(agent)
        assert rate_limited == []


class TestApiProviderCompactionDurable:
    def test_api_provider_compacts_and_writes_to_jsonl(self, tmp_path):
        from orchestrator.agent_states import AgentTermination
        
        class FakeProvider:
            pass
            
        from orchestrator.session_store import SessionStore
        agent = make_test_agent(provider=FakeProvider())
        agent._log = lambda level, msg: None
        agent._context_meter = ContextMeter(budget=10_000)
        agent._session_store = SessionStore(base_dir=tmp_path, session_id="test-session")
        agent._resume_session_id = "test-session"
        
        # Seed the session with messages
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "user", "content": "do something"},
        ]
        # Write to JSONL
        for m in msgs:
            agent._session_store.append_message(m)
            
        # Run agent with /compact task
        result = asyncio.run(agent.run("/compact"))
        assert result["success"] is True, result
        assert result["termination"] == "task_complete"
        
        # Load the session back and verify it is compacted!
        from orchestrator.session_store import SessionStore
        reloaded = SessionStore.load_session("test-session", base_dir=tmp_path)
        assert len(reloaded) > 0
        # The reloaded messages should start with the compact_summary role!
        assert reloaded[0]["role"] == "compact_summary"

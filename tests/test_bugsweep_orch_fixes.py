"""Behavioral tests for the orchestrator/ bug-sweep fixes (BUGSWEEP_orch.md).

One focused test per non-trivial fix, grouped by the finding id it covers.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from orchestrator.providers.base import (
    ProviderEnvironmentError,
    classify_provider_error,
)


# ── H1: env errors are not retryable "network" ───────────────────────────────

def test_h1_provider_environment_error_is_not_retryable_network():
    err = ProviderEnvironmentError("Ollama is installed but not running.")
    classified = classify_provider_error(err)
    assert classified.code == "environment"
    assert classified.retryable is False


def test_h1_plain_oserror_is_still_retryable_network():
    # A genuine transient connection failure stays retryable.
    classified = classify_provider_error(ConnectionError("connection refused"))
    assert classified.code == "network"
    assert classified.retryable is True


# ── L4: 429 must be digit-bounded, not a bare substring ──────────────────────

def test_l4_429_substring_does_not_false_match():
    # "error 14290" contains "429" but is not a 429 rate-limit.
    classified = classify_provider_error(RuntimeError("upstream error 14290"))
    assert classified.code != "rate_limit"


def test_l4_real_429_is_rate_limited():
    classified = classify_provider_error(RuntimeError("HTTP 429 Too Many Requests"))
    assert classified.code == "rate_limit"


# ── M10: a quota message containing "api key" is rate_limit, not auth ─────────

def test_m10_gemini_quota_message_is_not_classified_auth():
    # The message mentions "API key" — the auth markers must not win over the
    # provider's explicit ProviderError classification.
    from orchestrator.providers.base import ProviderError

    err = ProviderError(
        "rate_limit",
        "Your Google Gemini free-tier quota has been exceeded. Use an API key.",
        retryable=False,
    )
    classified = classify_provider_error(err)
    assert classified.code == "rate_limit"


# ── H2: assistant narration accompanying a tool call is preserved ────────────

def test_h2_claude_tool_call_carries_preceding_text():
    from orchestrator.providers.claude import AnthropicProvider

    class _Block:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class _Resp:
        usage = None
        content = [
            _Block(type="text", text="PLAN: do the thing"),
            _Block(type="tool_use", name="read_file", input={"path": "a"}),
        ]

    provider = AnthropicProvider.__new__(AnthropicProvider)
    parsed = provider._parse_response(_Resp())
    assert parsed["type"] == "tool_call"
    assert parsed["tool"] == "read_file"
    assert "PLAN: do the thing" in parsed["content"]


def test_h2_openai_tool_call_carries_narration():
    from orchestrator.providers.openai_provider import OpenAIProvider

    class _Fn:
        name = "read_file"
        arguments = '{"path": "a"}'

    class _TC:
        function = _Fn()

    class _Msg:
        content = "STEP 1: reading"
        tool_calls = [_TC()]

    class _Choice:
        message = _Msg()

    class _Resp:
        usage = None
        choices = [_Choice()]

    provider = OpenAIProvider.__new__(OpenAIProvider)
    parsed = provider._parse_response(_Resp())
    assert parsed["type"] == "tool_call"
    assert parsed["content"] == "STEP 1: reading"


def test_h2_gemini_tool_call_carries_narration():
    from orchestrator.providers import gemini

    provider = gemini.GeminiProvider({"gemini_auth_mode": "oauth", "oauth_access_token": "t"})
    parsed = provider._parse_rest_response({
        "candidates": [{"content": {"parts": [
            {"text": "PLAN: list files"},
            {"functionCall": {"name": "bash", "args": {"cmd": "ls"}}},
        ]}}],
    })
    assert parsed["type"] == "tool_call"
    assert parsed["content"] == "PLAN: list files"


# ── M2: Gemini blocked prompt / finishReason surfaces instead of empty ───────

def test_m2_gemini_block_reason_surfaces():
    from orchestrator.providers import gemini

    provider = gemini.GeminiProvider({"gemini_auth_mode": "oauth", "oauth_access_token": "t"})
    parsed = provider._parse_rest_response({"promptFeedback": {"blockReason": "SAFETY"}})
    assert parsed["type"] == "text"
    assert "SAFETY" in parsed["content"]
    assert parsed["content"].startswith("NEED_HELP")


def test_m2_gemini_max_tokens_finish_reason_surfaces():
    from orchestrator.providers import gemini

    provider = gemini.GeminiProvider({"gemini_auth_mode": "oauth", "oauth_access_token": "t"})
    parsed = provider._parse_rest_response({
        "candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": []}}],
    })
    assert parsed["type"] == "text"
    assert "MAX_TOKENS" in parsed["content"]


def test_m2_gemini_normal_stop_stays_plain_text():
    from orchestrator.providers import gemini

    provider = gemini.GeminiProvider({"gemini_auth_mode": "oauth", "oauth_access_token": "t"})
    parsed = provider._parse_rest_response({
        "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "hello"}]}}],
    })
    assert parsed == {
        "type": "text",
        "content": "hello",
        "usage": {"input": 0, "output": 0, "cache_read_tokens": 0},
    }


# ── M9: malformed tool args coerced to {} (never a raw non-dict) ─────────────

def test_m9_ollama_normalize_tool_arguments_coerces_invalid_json():
    from orchestrator.providers.ollama import _normalize_tool_arguments

    assert _normalize_tool_arguments("not json") == {}
    assert _normalize_tool_arguments('["a", "b"]') == {}
    assert _normalize_tool_arguments('{"x": 1}') == {"x": 1}


# ── M1: strip_data_uris validates the mime type ──────────────────────────────

def test_m1_strip_data_uris_ignores_bogus_data_prefix():
    from orchestrator.providers.browser.prompt_builder import strip_data_uris

    # "metadata:" contains "data:" but is not a data URI, and the later
    # ";base64," belongs to a real image far away — the whitespace-containing
    # span must NOT be accepted as a mime type.
    text = "metadata: captured 2026 and the image image/png;base64,QUJD ends here"
    out: list = []
    result = strip_data_uris(text, out)
    # The prose before the real data URI is preserved (not swallowed).
    assert "metadata: captured 2026" in result


def test_m1_strip_data_uris_extracts_real_uri():
    from orchestrator.providers.browser.prompt_builder import strip_data_uris

    text = "here: data:image/png;base64,QUJD done"
    out: list = []
    result = strip_data_uris(text, out)
    assert len(out) == 1
    assert out[0]["mime_type"] == "image/png"
    assert "[Screenshot attached]" in result


# ── L6: strip_transport_markers anchors on the LAST hash occurrence ──────────

def test_l6_strip_transport_markers_uses_last_hash():
    from orchestrator.providers.browser.response_parser import strip_transport_markers

    h = "KIM_HASH_abc"
    # The echoed prompt contains the hash in its instruction; the real answer
    # follows the model's own trailing hash.
    text = f"(instruction: always append {h}) The real answer is 42. {h}"
    result = strip_transport_markers(text, h)
    assert "The real answer is 42." in result


# ── L3: try_parse_tool_json coerces non-dict args to {} ──────────────────────

def test_l3_try_parse_tool_json_coerces_null_args():
    from orchestrator.providers.browser.response_parser import try_parse_tool_json

    parsed = try_parse_tool_json('{"tool": "read_file", "args": null}', known_tools={"read_file"})
    assert parsed is not None
    assert parsed["args"] == {}


# ── M4: compaction recognizes list-shaped tool results ───────────────────────

def test_m4_compaction_is_tool_result_handles_list_content():
    from orchestrator.compaction import _is_tool_result

    msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": "[Tool result: take_screenshot]\nok"},
            {"type": "image", "data": "..."},
        ],
    }
    assert _is_tool_result(msg) is True


def test_m4_compaction_is_tool_result_str_still_works():
    from orchestrator.compaction import _is_tool_result

    assert _is_tool_result({"role": "user", "content": "[Tool result: read_file]\nx"}) is True
    assert _is_tool_result({"role": "user", "content": "hello"}) is False


# ── L9: _strip_images tolerates non-dict list items ──────────────────────────

def test_l9_strip_images_tolerates_raw_string_items():
    from orchestrator.memory import _strip_images

    content = ["a bare string", {"type": "image", "data": "x"}, {"type": "text", "text": "keep"}]
    out = _strip_images(content)
    assert isinstance(out, list)
    # image removed; the bare string and text survive
    assert {"type": "image", "data": "x"} not in out


# ── M3 / M5: interaction policy element-id bookkeeping ───────────────────────

def test_m3_web_resolve_registers_returned_element_id():
    from orchestrator.interaction_policy import InteractionPolicy

    policy = InteractionPolicy()
    policy.web_generation = 1  # a web_observe happened
    policy.after_tool("web_resolve", {"intent": "the submit button"},
                      json.dumps({"element_id": "w99", "confidence": 0.9}))
    # A follow-up web_click by that id is now allowed (not hard-blocked).
    decision = policy.before_tool("web_click", {"element_id": "w99"})
    assert decision.allowed is True


def test_m5_failed_observe_ui_resets_generation():
    from orchestrator.interaction_policy import InteractionPolicy

    policy = InteractionPolicy()
    policy.after_tool("observe_ui", {}, "ERROR: accessibility tree unavailable e5: bogus")
    assert policy.ui_generation == 0
    assert policy.known_ui_element_ids == set()
    # click_ui is blocked with the "requires a prior observe_ui" remedy.
    decision = policy.before_tool("click_ui", {"element_id": "e5"})
    assert decision.allowed is False


# ── M8: cron_store.update recomputes next_run_at on schedule change ──────────

def test_m8_cron_update_reschedules_on_schedule_change(tmp_path):
    from orchestrator.cron_store import CronStore

    store = CronStore(store_file=tmp_path / "schedules.json")
    task = store.add("ping", "@hourly")
    original = store.get(task.id).next_run_at

    updated = store.update(task.id, schedule_expr="@every 5m")
    assert updated is not None
    # next_run_at must have moved to reflect the shorter cadence.
    assert updated.next_run_at is not None
    assert updated.next_run_at != original


def test_m8_cron_update_without_schedule_change_keeps_next_run(tmp_path):
    from orchestrator.cron_store import CronStore

    store = CronStore(store_file=tmp_path / "schedules.json")
    task = store.add("ping", "@hourly")
    original = store.get(task.id).next_run_at
    updated = store.update(task.id, task="ping harder")
    assert updated.next_run_at == original


# ── H5: browser provider only commits _sent_system_prompt on success ─────────

def test_h5_commit_sent_system_prompt_skips_need_help():
    from orchestrator.providers.browser.provider import BrowserProvider

    provider = BrowserProvider.__new__(BrowserProvider)
    provider._sent_system_prompt = False
    # A NEED_HELP result means nothing was delivered — must NOT commit.
    provider._commit_sent_system_prompt(
        {"type": "text", "content": "NEED_HELP: lost the tab"}, new_sent=True
    )
    assert provider._sent_system_prompt is False
    # A real answer commits.
    provider._commit_sent_system_prompt(
        {"type": "text", "content": "the answer"}, new_sent=True
    )
    assert provider._sent_system_prompt is True


# ── T1 / H3: stale + mismatched approval decisions are discarded ─────────────

def test_t1_pump_discards_mismatched_decision_id():
    from orchestrator.ui_bridge import StdinPump

    async def scenario():
        pump = StdinPump()
        pump._loop = asyncio.get_running_loop()
        # A decision for a DIFFERENT (earlier) request arrives, then the one
        # for the pending request. Only the matching id is returned.
        loop = asyncio.get_running_loop()
        loop.call_later(0.01, pump._dispatch,
                        {"type": "hitl_approve", "id": "OLD", "decision": "accept"})
        loop.call_later(0.02, pump._dispatch,
                        {"type": "hitl_approve", "id": "NEW", "decision": "decline"})
        return await pump.next_approval(timeout=1.0, request_id="NEW")

    data = asyncio.run(scenario())
    assert data["id"] == "NEW"
    assert data["decision"] == "decline"


def test_t1_pump_drops_pre_queued_stale_decision():
    from orchestrator.ui_bridge import StdinPump

    async def scenario():
        pump = StdinPump()
        pump._loop = None  # synchronous dispatch straight into the queue
        # A late Approve for a timed-out prompt is already queued BEFORE the
        # next request starts waiting — it must be dropped, not applied.
        pump._dispatch({"type": "hitl_approve", "id": "OLD", "decision": "accept"})
        loop = asyncio.get_running_loop()
        pump._loop = loop
        loop.call_later(0.02, pump._dispatch,
                        {"type": "hitl_approve", "id": "NEW", "decision": "accept"})
        return await pump.next_approval(timeout=1.0, request_id="NEW")

    data = asyncio.run(scenario())
    assert data["id"] == "NEW"


def test_t1_pump_times_out_when_only_stale_decisions_present():
    from orchestrator.ui_bridge import StdinPump

    async def scenario():
        pump = StdinPump()
        pump._loop = None
        pump._dispatch({"type": "hitl_approve", "id": "OLD", "decision": "accept"})
        with pytest.raises(asyncio.TimeoutError):
            await pump.next_approval(timeout=0.1, request_id="NEW")

    asyncio.run(scenario())


# ── H4: app-server transport discards a mismatched decision id ───────────────

def test_h4_collect_decision_discards_mismatched_then_matches():
    from orchestrator.codex_appserver_transport import AppServerTurnRunner
    from codex_engine.app_server import ServerRequest

    # A scripted reader: first a stale decision for a different id, then the
    # decision addressed to this request.
    scripted = [("accept", "OTHER"), ("decline", None)]

    async def reader(_timeout):
        return scripted.pop(0) if scripted else None

    runner = AppServerTurnRunner(
        task="t", cwd="/p", model=None, config={}, proxy_port=1,
        bearer_token="b", thread_state={}, binary_path="/bin/codex",
        client=object(), decision_reader=reader, install_signal_handler=False,
    )
    runner._interactive = True
    req = ServerRequest(id=5, method="item/commandExecution/requestApproval",
                        params={"command": "rm -rf x"})
    decision = asyncio.run(runner._collect_decision(req))
    # The stale ("accept", "OTHER") is discarded; the id-less decline applies.
    assert decision == "decline"

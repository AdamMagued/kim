"""V-3 — Provider response-shape conformance matrix (Team B's V-3 finish).

Asserts every provider that produces a canonical response honors the SAME base
contract the agent loop consumes, so no provider silently drifts from the
de-facto richest shape documented in docs/ops/findings/team-b.md:

    text        -> {"type":"text", "content": str, "stop_reason", "usage"}
    tool_call   -> {"type":"tool_call", "tool", "args", "content"(narration),
                    "stop_reason", "usage"}
    multi-call  -> {"type":"tool_call", "tool":"batch",
                    "args":{"calls":[{tool,args}, ...]}}

Plus the honesty contract: a length/max-tokens truncated text reply is annotated
(finalize_text_content) rather than presented as complete.

No network: the API providers are exercised through their pure parse layer
(_parse_response / _parse_rest_response); Ollama is driven through complete()
with its stream/usage/daemon layer stubbed.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest


# ── Builders: return the PARSED canonical response dict per (provider, kind) ──

def _claude(kind):
    from orchestrator.providers.claude import AnthropicProvider
    p = object.__new__(AnthropicProvider)
    usage = SimpleNamespace(
        input_tokens=10, output_tokens=5,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )

    def resp(blocks, stop_reason):
        return SimpleNamespace(content=blocks, stop_reason=stop_reason, usage=usage)

    text_block = SimpleNamespace(type="text", text="here is the answer")
    if kind == "text":
        return p._parse_response(resp([text_block], "end_turn"))
    if kind == "text_truncated":
        return p._parse_response(resp([SimpleNamespace(type="text", text="half")], "max_tokens"))
    tool = SimpleNamespace(type="tool_use", name="read_file", input={"path": "a"})
    if kind == "tool_call":
        return p._parse_response(resp([text_block, tool], "tool_use"))
    if kind == "batch":
        tool2 = SimpleNamespace(type="tool_use", name="list_dir", input={"path": "."})
        return p._parse_response(resp([tool, tool2], "tool_use"))
    raise ValueError(kind)


def _openai_like(cls_path, kind):
    module, cls_name = cls_path
    mod = __import__(module, fromlist=[cls_name])
    p = object.__new__(getattr(mod, cls_name))
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, prompt_tokens_details=None)

    def resp(content, tool_calls, finish_reason):
        msg = SimpleNamespace(content=content, tool_calls=tool_calls)
        choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
        return SimpleNamespace(choices=[choice], usage=usage)

    def tc(name, args_json):
        return SimpleNamespace(function=SimpleNamespace(name=name, arguments=args_json))

    if kind == "text":
        return p._parse_response(resp("here is the answer", None, "stop"))
    if kind == "text_truncated":
        return p._parse_response(resp("half", None, "length"))
    if kind == "tool_call":
        return p._parse_response(resp("narration", [tc("read_file", '{"path":"a"}')], "tool_calls"))
    if kind == "batch":
        return p._parse_response(resp(
            "narration",
            [tc("read_file", '{"path":"a"}'), tc("list_dir", '{"path":"."}')],
            "tool_calls",
        ))
    raise ValueError(kind)


def _gemini(kind):
    from orchestrator.providers.gemini import GeminiProvider
    p = object.__new__(GeminiProvider)
    usage = {"promptTokenCount": 10, "candidatesTokenCount": 5}

    def resp(parts, finish_reason):
        return {
            "usageMetadata": usage,
            "candidates": [{"content": {"parts": parts}, "finishReason": finish_reason}],
        }

    if kind == "text":
        return p._parse_rest_response(resp([{"text": "here is the answer"}], "STOP"))
    if kind == "text_truncated":
        return p._parse_rest_response(resp([{"text": "half"}], "MAX_TOKENS"))
    if kind == "tool_call":
        return p._parse_rest_response(resp(
            [{"text": "narration"}, {"functionCall": {"name": "read_file", "args": {"path": "a"}}}],
            "STOP",
        ))
    if kind == "batch":
        return p._parse_rest_response(resp(
            [
                {"functionCall": {"name": "read_file", "args": {"path": "a"}}},
                {"functionCall": {"name": "list_dir", "args": {"path": "."}}},
            ],
            "STOP",
        ))
    raise ValueError(kind)


def _ollama(kind):
    from orchestrator.providers.ollama import OllamaProvider
    provider = OllamaProvider({"ollama": {"mode": "cloud", "cloud_model": "m:cloud"}})

    if kind == "text":
        final, content, tcs = {"done_reason": "stop", "model": "m"}, "here is the answer", []
    elif kind == "text_truncated":
        final, content, tcs = {"done_reason": "length", "model": "m"}, "half", []
    elif kind == "tool_call":
        final = {"done_reason": "stop", "model": "m"}
        content = "narration"
        tcs = [{"function": {"name": "read_file", "arguments": {"path": "a"}}}]
    elif kind == "batch":
        final = {"done_reason": "stop", "model": "m"}
        content = "narration"
        tcs = [
            {"function": {"name": "read_file", "arguments": {"path": "a"}}},
            {"function": {"name": "list_dir", "arguments": {"path": "."}}},
        ]
    else:
        raise ValueError(kind)

    async def _noop():
        return None

    async def _fake_stream(_payload):
        return final, content, tcs

    async def _fake_usage(_f, _m):
        return {"provider": "ollama", "source": "ollama", "mode": "cloud"}

    async def _drive():
        with patch.object(provider, "_ensure_daemon_running", _noop), \
             patch.object(provider, "_stream_chat", _fake_stream), \
             patch.object(provider, "_usage_from_final", _fake_usage):
            return await provider.complete([{"role": "user", "content": "hi"}], [], "sys")

    return asyncio.run(_drive())


BUILDERS = {
    "claude": _claude,
    "openai": lambda kind: _openai_like(("orchestrator.providers.openai_provider", "OpenAIProvider"), kind),
    "deepseek": lambda kind: _openai_like(("orchestrator.providers.deepseek", "DeepSeekProvider"), kind),
    "openai_oauth": lambda kind: _openai_like(("orchestrator.providers.openai_oauth", "OpenAIOAuthProvider"), kind),
    "gemini": _gemini,
    "ollama": _ollama,
}

ALL_PROVIDERS = list(BUILDERS)


# ── Contract assertions, parametrized over every provider ────────────────────

@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_text_shape(provider):
    r = BUILDERS[provider]("text")
    assert r["type"] == "text"
    assert isinstance(r["content"], str) and r["content"]
    assert "stop_reason" in r, f"{provider} text must carry stop_reason"
    assert "usage" in r, f"{provider} text must carry usage"


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_truncated_text_is_annotated(provider):
    r = BUILDERS[provider]("text_truncated")
    assert r["type"] == "text"
    assert "truncated" in r["content"].lower(), (
        f"{provider} must annotate a length-truncated reply (finalize_text_content)"
    )


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_single_tool_call_shape(provider):
    r = BUILDERS[provider]("tool_call")
    assert r["type"] == "tool_call"
    assert r["tool"] == "read_file"
    assert isinstance(r["args"], dict) and r["args"] == {"path": "a"}
    assert "content" in r, f"{provider} tool_call must carry narration content"
    assert "usage" in r


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_multi_tool_call_becomes_batch(provider):
    r = BUILDERS[provider]("batch")
    assert r["type"] == "tool_call"
    assert r["tool"] == "batch", f"{provider} must wrap parallel calls as a batch"
    calls = r["args"]["calls"]
    assert isinstance(calls, list) and len(calls) == 2
    names = [c["tool"] for c in calls]
    assert names == ["read_file", "list_dir"]
    for c in calls:
        assert isinstance(c.get("args"), dict)

"""
Regression tests for batch-wrapping shape of tool-call responses.

Covers:
1. Claude provider: multiple tool_use blocks → canonical batch wrapper shape
2. OpenAI provider: multiple parallel tool calls → same batch wrapper shape
3. Single tool call: returned directly, NOT wrapped in a batch

No network required — all responses are stubbed with SimpleNamespace / MagicMock.
"""
from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock


# ── Stub heavy runtime deps (matches convention from test_provider_contract.py) ──

def _ensure_stubs():
    for mod in ("mss", "pynput", "pynput.mouse", "pynput.keyboard",
                "pyautogui", "PIL", "PIL.Image", "sounddevice", "soundfile",
                "kokoro", "pygetwindow"):
        if mod not in sys.modules:
            stub = types.ModuleType(mod)
            sys.modules[mod] = stub


_ensure_stubs()


# ── Helpers: build fake Anthropic response objects ────────────────────────────

def _make_tool_use_block(name: str, input_dict: dict):
    """Mimic an Anthropic tool_use content block."""
    block = SimpleNamespace(
        type="tool_use",
        name=name,
        input=input_dict,
    )
    return block


def _make_text_block(text: str):
    block = SimpleNamespace(type="text", text=text)
    return block


def _make_anthropic_response(blocks: list, input_tokens: int = 10, output_tokens: int = 5):
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    return SimpleNamespace(content=blocks, usage=usage)


# ── Helpers: build fake OpenAI response objects ───────────────────────────────

def _make_oai_tool_call(call_id: str, name: str, arguments: dict):
    fn = SimpleNamespace(name=name, arguments=json.dumps(arguments))
    return SimpleNamespace(id=call_id, function=fn)


def _make_oai_response(tool_calls: list | None, content: str = ""):
    msg = SimpleNamespace(tool_calls=tool_calls or [], content=content)
    choice = SimpleNamespace(message=msg)
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=5,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
    )
    return SimpleNamespace(choices=[choice], usage=usage)


# ── Claude provider batch tests ───────────────────────────────────────────────

class TestClaudeBatchShape:
    def _provider(self):
        from orchestrator.providers.claude import AnthropicProvider
        return object.__new__(AnthropicProvider)

    def test_multiple_tool_calls_wrapped_as_batch(self):
        """Claude: >1 tool_use blocks → batch wrapper with every sub-call present."""
        p = self._provider()
        blocks = [
            _make_tool_use_block("run_command", {"cmd": "ls /tmp"}),
            _make_tool_use_block("read_file", {"path": "/etc/hosts"}),
        ]
        response = _make_anthropic_response(blocks)
        result = p._parse_response(response)

        assert result["type"] == "tool_call"
        assert result["tool"] == "batch"
        assert "args" in result
        calls = result["args"]["calls"]
        assert len(calls) == 2
        # Both sub-calls must be present (no extras discarded)
        names = [c["tool"] for c in calls]
        assert "run_command" in names
        assert "read_file" in names
        # Each sub-call carries its args
        rc = next(c for c in calls if c["tool"] == "run_command")
        assert rc["args"] == {"cmd": "ls /tmp"}

    def test_multiple_tool_calls_batch_has_usage(self):
        """Claude batch result carries a usage dict."""
        p = self._provider()
        blocks = [
            _make_tool_use_block("tool_a", {"x": 1}),
            _make_tool_use_block("tool_b", {"y": 2}),
        ]
        response = _make_anthropic_response(blocks, input_tokens=42, output_tokens=7)
        result = p._parse_response(response)

        assert "usage" in result
        assert result["usage"]["input"] == 42
        assert result["usage"]["output"] == 7

    def test_single_tool_call_not_wrapped(self):
        """Claude: exactly 1 tool_use block → direct tool_call, NOT batch."""
        p = self._provider()
        blocks = [_make_tool_use_block("take_screenshot", {})]
        response = _make_anthropic_response(blocks)
        result = p._parse_response(response)

        assert result["type"] == "tool_call"
        assert result["tool"] == "take_screenshot"
        assert result["tool"] != "batch"
        assert result["args"] == {}

    def test_three_tool_calls_all_preserved_in_batch(self):
        """Claude: 3 tool_use blocks — none are discarded."""
        p = self._provider()
        blocks = [
            _make_tool_use_block("tool_a", {"a": 1}),
            _make_tool_use_block("tool_b", {"b": 2}),
            _make_tool_use_block("tool_c", {"c": 3}),
        ]
        response = _make_anthropic_response(blocks)
        result = p._parse_response(response)

        assert result["tool"] == "batch"
        calls = result["args"]["calls"]
        assert len(calls) == 3
        assert {c["tool"] for c in calls} == {"tool_a", "tool_b", "tool_c"}


# ── OpenAI provider batch tests ───────────────────────────────────────────────

class TestOpenAIBatchShape:
    def _provider(self):
        from orchestrator.providers.openai_provider import OpenAIProvider
        return object.__new__(OpenAIProvider)

    def test_openai_batch_shape_matches(self):
        """OpenAI: >1 parallel tool calls → same batch wrapper shape as Claude."""
        p = self._provider()
        tool_calls = [
            _make_oai_tool_call("call_1", "run_command", {"cmd": "pwd"}),
            _make_oai_tool_call("call_2", "read_file", {"path": "/tmp/x.txt"}),
        ]
        response = _make_oai_response(tool_calls)
        result = p._parse_response(response)

        assert result["type"] == "tool_call"
        assert result["tool"] == "batch"
        assert "args" in result
        calls = result["args"]["calls"]
        assert len(calls) == 2
        names = [c["tool"] for c in calls]
        assert "run_command" in names
        assert "read_file" in names
        rc = next(c for c in calls if c["tool"] == "run_command")
        assert rc["args"] == {"cmd": "pwd"}

    def test_openai_batch_has_usage(self):
        """OpenAI batch result carries a usage dict."""
        p = self._provider()
        tool_calls = [
            _make_oai_tool_call("c1", "a", {"k": 1}),
            _make_oai_tool_call("c2", "b", {"k": 2}),
        ]
        response = _make_oai_response(tool_calls)
        result = p._parse_response(response)

        assert "usage" in result
        assert result["usage"]["input"] == 10
        assert result["usage"]["output"] == 5

    def test_openai_single_tool_call_not_wrapped(self):
        """OpenAI: exactly 1 tool call → direct tool_call, NOT batch."""
        p = self._provider()
        tool_calls = [_make_oai_tool_call("c1", "list_dir", {"path": "/"})]
        response = _make_oai_response(tool_calls)
        result = p._parse_response(response)

        assert result["type"] == "tool_call"
        assert result["tool"] == "list_dir"
        assert result["tool"] != "batch"
        assert result["args"] == {"path": "/"}

    def test_openai_three_tool_calls_all_preserved_in_batch(self):
        """OpenAI: 3 parallel tool calls — none are discarded."""
        p = self._provider()
        tool_calls = [
            _make_oai_tool_call("c1", "tool_a", {"a": 1}),
            _make_oai_tool_call("c2", "tool_b", {"b": 2}),
            _make_oai_tool_call("c3", "tool_c", {"c": 3}),
        ]
        response = _make_oai_response(tool_calls)
        result = p._parse_response(response)

        assert result["tool"] == "batch"
        calls = result["args"]["calls"]
        assert len(calls) == 3
        assert {c["tool"] for c in calls} == {"tool_a", "tool_b", "tool_c"}


# ── Cross-provider shape consistency ─────────────────────────────────────────

class TestBatchShapeConsistency:
    """The batch wrapper shape must be identical across Claude and OpenAI."""

    def test_batch_top_level_keys_match(self):
        from orchestrator.providers.claude import AnthropicProvider
        from orchestrator.providers.openai_provider import OpenAIProvider

        claude_p = object.__new__(AnthropicProvider)
        oai_p = object.__new__(OpenAIProvider)

        claude_resp = _make_anthropic_response([
            _make_tool_use_block("tool_x", {"v": 1}),
            _make_tool_use_block("tool_y", {"v": 2}),
        ])
        oai_resp = _make_oai_response([
            _make_oai_tool_call("c1", "tool_x", {"v": 1}),
            _make_oai_tool_call("c2", "tool_y", {"v": 2}),
        ])

        claude_result = claude_p._parse_response(claude_resp)
        oai_result = oai_p._parse_response(oai_resp)

        # type and tool fields must match
        assert claude_result["type"] == oai_result["type"] == "tool_call"
        assert claude_result["tool"] == oai_result["tool"] == "batch"
        # args.calls structure must match
        assert set(claude_result["args"].keys()) == set(oai_result["args"].keys())
        assert len(claude_result["args"]["calls"]) == len(oai_result["args"]["calls"])

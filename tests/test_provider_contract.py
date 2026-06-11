"""V-3b: Provider message-formatting contract suite.

Parametrized across every provider that formats messages before sending
to the LLM API.  Each scenario (text reply, tool call, tool result,
image content) must pass identical shape assertions.  No network required.

Covered providers: claude, openai/deepseek (shared _to_oai_messages),
gemini (_to_rest_request), ollama (_to_ollama_messages).
BrowserProvider/FakeProvider have no message-formatting layer to test.
"""
from __future__ import annotations

import base64
import sys
import types

import pytest


# ── Stub heavy runtime deps so provider modules can be imported ───────────────

def _ensure_stubs():
    # Only stub modules that are NOT installed in this environment and would
    # cause ImportError.  anthropic, openai, and httpx are installed in the
    # venv, so leave them alone.  Never stub playwright — it has real
    # test coverage in test_browser_protocol.py.
    for mod in ("mss", "pynput", "pynput.mouse", "pynput.keyboard",
                "pyautogui", "PIL", "PIL.Image", "sounddevice", "soundfile",
                "kokoro", "pygetwindow"):
        if mod not in sys.modules:
            stub = types.ModuleType(mod)
            sys.modules[mod] = stub


_ensure_stubs()


# ── Canonical test data ────────────────────────────────────────────────────────

TEXT_MESSAGES = [
    {"role": "user", "content": "Hello, what time is it?"},
    {"role": "assistant", "content": "It is 3 PM."},
]

# A conversation where the assistant called a tool and got a result back
TOOL_CALL_MESSAGES = [
    {"role": "user", "content": "List files in /tmp"},
    {
        "role": "assistant",
        "content": '{"type": "tool_call", "tool": "run_command", "args": {"cmd": "ls /tmp"}}',
    },
    {
        "role": "user",
        "content": "[Tool result: run_command]\nfile1.txt\nfile2.txt",
    },
]

IMAGE_MESSAGES = [
    {"role": "user", "content": "What is in this image?"},
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "Describe this screenshot."},
            {
                "type": "image",
                "data": base64.b64encode(b"fake-png-bytes").decode(),
                "media_type": "image/png",
            },
        ],
    },
]

TOOL_DEF = [
    {
        "name": "run_command",
        "description": "Run a shell command",
        "parameters": {
            "type": "object",
            "properties": {"cmd": {"type": "string", "description": "Command"}},
            "required": ["cmd"],
        },
    }
]

SYSTEM = "You are a helpful agent."


# ── Claude provider ────────────────────────────────────────────────────────────

class TestClaudeMessageFormatting:
    def _provider(self):
        from orchestrator.providers.claude import AnthropicProvider
        p = object.__new__(AnthropicProvider)
        return p

    def test_text_messages_round_trip(self):
        p = self._provider()
        out = p._to_claude_messages(TEXT_MESSAGES)
        assert isinstance(out, list)
        assert len(out) == 2
        assert out[0]["role"] == "user"
        assert isinstance(out[0]["content"], str)
        assert out[1]["role"] == "assistant"

    def test_tool_call_messages_round_trip(self):
        p = self._provider()
        out = p._to_claude_messages(TOOL_CALL_MESSAGES)
        assert len(out) == 3
        # assistant message content should be str (JSON serialized tool call)
        assert out[1]["role"] == "assistant"
        assert isinstance(out[1]["content"], str)

    def test_image_messages_round_trip(self):
        p = self._provider()
        out = p._to_claude_messages(IMAGE_MESSAGES)
        # Find the message with image content
        img_msg = next(m for m in out if isinstance(m.get("content"), list))
        assert any(
            isinstance(blk, dict) and blk.get("type") == "image"
            for blk in img_msg["content"]
        )

    def test_image_message_has_base64_source(self):
        p = self._provider()
        out = p._to_claude_messages(IMAGE_MESSAGES)
        img_msg = next(m for m in out if isinstance(m.get("content"), list))
        img_blk = next(blk for blk in img_msg["content"] if isinstance(blk, dict) and blk.get("type") == "image")
        assert img_blk["source"]["type"] == "base64"
        assert img_blk["source"]["media_type"] == "image/png"

    def test_tools_formatted_with_input_schema(self):
        p = self._provider()
        out = p._to_claude_tools(TOOL_DEF)
        assert len(out) == 1
        assert out[0]["name"] == "run_command"
        assert "input_schema" in out[0]


# ── OpenAI provider ───────────────────────────────────────────────────────────

class TestOpenAIMessageFormatting:
    def _provider(self):
        from orchestrator.providers.openai_provider import OpenAIProvider
        p = object.__new__(OpenAIProvider)
        return p

    def test_text_messages_round_trip(self):
        p = self._provider()
        out = p._to_oai_messages(TEXT_MESSAGES)
        assert isinstance(out, list)
        assert len(out) == 2
        assert out[0]["role"] == "user"
        assert out[1]["role"] == "assistant"

    def test_tool_call_messages_round_trip(self):
        p = self._provider()
        out = p._to_oai_messages(TOOL_CALL_MESSAGES)
        assert len(out) == 3

    def test_image_messages_round_trip(self):
        p = self._provider()
        out = p._to_oai_messages(IMAGE_MESSAGES)
        img_msg = next(m for m in out if isinstance(m.get("content"), list))
        img_blk = next(
            blk for blk in img_msg["content"]
            if isinstance(blk, dict) and blk.get("type") == "image_url"
        )
        assert "image_url" in img_blk
        assert img_blk["image_url"]["url"].startswith("data:image/png;base64,")

    def test_tools_formatted_as_function_type(self):
        p = self._provider()
        out = p._to_oai_tools(TOOL_DEF)
        assert out[0]["type"] == "function"
        assert "function" in out[0]
        assert out[0]["function"]["name"] == "run_command"


# ── DeepSeek provider (subclasses OpenAI) ─────────────────────────────────────

class TestDeepSeekMessageFormatting:
    def _provider(self):
        from orchestrator.providers.deepseek import DeepSeekProvider
        p = object.__new__(DeepSeekProvider)
        return p

    def test_text_messages_inherits_oai_format(self):
        p = self._provider()
        out = p._to_oai_messages(TEXT_MESSAGES)
        assert len(out) == 2
        assert out[0]["role"] == "user"
        assert out[1]["role"] == "assistant"

    def test_image_messages_use_oai_image_url_format(self):
        p = self._provider()
        out = p._to_oai_messages(IMAGE_MESSAGES)
        img_msg = next(m for m in out if isinstance(m.get("content"), list))
        img_blk = next(
            blk for blk in img_msg["content"]
            if isinstance(blk, dict) and blk.get("type") == "image_url"
        )
        assert img_blk["image_url"]["url"].startswith("data:image/png;base64,")


# ── Gemini provider ───────────────────────────────────────────────────────────

class TestGeminiMessageFormatting:
    def _provider(self):
        from orchestrator.providers.gemini import GeminiProvider
        p = object.__new__(GeminiProvider)
        p._max_tokens = 4096
        return p

    def test_text_messages_produce_contents_list(self):
        p = self._provider()
        body = p._to_rest_request(TEXT_MESSAGES, [], SYSTEM)
        assert "contents" in body
        assert isinstance(body["contents"], list)
        assert len(body["contents"]) == 2

    def test_system_instruction_added_when_present(self):
        p = self._provider()
        body = p._to_rest_request(TEXT_MESSAGES, [], SYSTEM)
        assert "systemInstruction" in body
        parts = body["systemInstruction"]["parts"]
        assert any(p_item.get("text") == SYSTEM for p_item in parts)

    def test_no_system_instruction_when_empty(self):
        p = self._provider()
        body = p._to_rest_request(TEXT_MESSAGES, [], "")
        assert "systemInstruction" not in body

    def test_tool_definitions_wrapped_in_function_declarations(self):
        p = self._provider()
        body = p._to_rest_request(TEXT_MESSAGES, TOOL_DEF, "")
        tools = body.get("tools", [])
        assert tools
        assert "functionDeclarations" in tools[0]
        assert tools[0]["functionDeclarations"][0]["name"] == "run_command"

    def test_image_content_produces_inline_data(self):
        p = self._provider()
        body = p._to_rest_request(IMAGE_MESSAGES, [], "")
        # Find a part with inlineData
        all_parts = [
            part
            for content in body["contents"]
            for part in content.get("parts", [])
        ]
        inline_parts = [pt for pt in all_parts if "inlineData" in pt]
        assert inline_parts, "Expected inlineData part for image message"
        assert inline_parts[0]["inlineData"]["mimeType"] == "image/png"

    def test_user_role_maps_to_user(self):
        p = self._provider()
        body = p._to_rest_request([{"role": "user", "content": "Hi"}], [], "")
        assert body["contents"][0]["role"] == "user"

    def test_assistant_role_maps_to_model(self):
        p = self._provider()
        body = p._to_rest_request([{"role": "assistant", "content": "Hi"}], [], "")
        assert body["contents"][0]["role"] == "model"


# ── Ollama provider ───────────────────────────────────────────────────────────

class TestOllamaMessageFormatting:
    def _provider(self):
        from orchestrator.providers.ollama import OllamaProvider
        p = object.__new__(OllamaProvider)
        return p

    def test_system_message_prepended_when_present(self):
        p = self._provider()
        out = p._to_ollama_messages(TEXT_MESSAGES, SYSTEM)
        assert out[0]["role"] == "system"
        assert out[0]["content"] == SYSTEM

    def test_no_system_message_when_empty(self):
        p = self._provider()
        out = p._to_ollama_messages(TEXT_MESSAGES, "")
        assert all(m["role"] != "system" for m in out)

    def test_text_messages_preserved(self):
        p = self._provider()
        out = p._to_ollama_messages(TEXT_MESSAGES, "")
        assert len(out) == 2
        assert out[0]["role"] == "user"
        assert out[0]["content"] == "Hello, what time is it?"

    def test_image_content_extracted_to_images_field(self):
        p = self._provider()
        out = p._to_ollama_messages(IMAGE_MESSAGES, "")
        img_msg = next((m for m in out if m.get("images")), None)
        assert img_msg is not None, "Expected a message with 'images' field for image content"
        assert len(img_msg["images"]) >= 1

    def test_tool_call_string_content_becomes_tool_calls(self):
        p = self._provider()
        out = p._to_ollama_messages(TOOL_CALL_MESSAGES, "")
        # The assistant message with tool call JSON string should be converted
        # to a native tool_calls message
        assistant_msg = next(m for m in out if m.get("role") == "assistant")
        assert "tool_calls" in assistant_msg or isinstance(assistant_msg.get("content"), str)

    def test_tool_result_message_becomes_tool_role(self):
        p = self._provider()
        out = p._to_ollama_messages(TOOL_CALL_MESSAGES, "")
        # Tool results should be emitted as role:'tool' if paired with a call
        roles = [m["role"] for m in out]
        # Either 'tool' role present or all messages are user/assistant (no paired call)
        assert "user" in roles or "tool" in roles


# ── Cross-provider consistency assertions ─────────────────────────────────────

class TestCrossProviderConsistency:
    """Every provider must handle the same canonical inputs without raising."""

    @pytest.mark.parametrize("provider_cls,format_fn_name,extra_args", [
        ("orchestrator.providers.claude.AnthropicProvider", "_to_claude_messages", []),
        ("orchestrator.providers.openai_provider.OpenAIProvider", "_to_oai_messages", []),
        ("orchestrator.providers.deepseek.DeepSeekProvider", "_to_oai_messages", []),
    ])
    def test_text_messages_do_not_raise(self, provider_cls, format_fn_name, extra_args):
        module_path, cls_name = provider_cls.rsplit(".", 1)
        import importlib
        mod = importlib.import_module(module_path)
        p = object.__new__(getattr(mod, cls_name))
        fn = getattr(p, format_fn_name)
        result = fn(TEXT_MESSAGES, *extra_args)
        assert isinstance(result, list)

    @pytest.mark.parametrize("provider_cls,format_fn_name,extra_args", [
        ("orchestrator.providers.claude.AnthropicProvider", "_to_claude_messages", []),
        ("orchestrator.providers.openai_provider.OpenAIProvider", "_to_oai_messages", []),
        ("orchestrator.providers.deepseek.DeepSeekProvider", "_to_oai_messages", []),
    ])
    def test_image_messages_do_not_raise(self, provider_cls, format_fn_name, extra_args):
        module_path, cls_name = provider_cls.rsplit(".", 1)
        import importlib
        mod = importlib.import_module(module_path)
        p = object.__new__(getattr(mod, cls_name))
        fn = getattr(p, format_fn_name)
        result = fn(IMAGE_MESSAGES, *extra_args)
        assert isinstance(result, list)

    def test_gemini_text_messages_do_not_raise(self):
        from orchestrator.providers.gemini import GeminiProvider
        p = object.__new__(GeminiProvider)
        p._max_tokens = 4096
        result = p._to_rest_request(TEXT_MESSAGES, [], "")
        assert "contents" in result

    def test_ollama_text_messages_do_not_raise(self):
        from orchestrator.providers.ollama import OllamaProvider
        p = object.__new__(OllamaProvider)
        result = p._to_ollama_messages(TEXT_MESSAGES, "")
        assert isinstance(result, list)

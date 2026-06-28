import unittest
from unittest.mock import patch

from orchestrator.providers.ollama import (
    DEFAULT_OLLAMA_CLOUD_MODEL,
    OllamaProvider,
    _accumulate_tool_call_delta,
    _normalize_image_data,
    _normalize_tool_arguments,
    _parse_num_ctx,
    _parse_ollama_ps_context,
    _tool_result_message,
)


class OllamaProviderTests(unittest.TestCase):
    def test_parse_num_ctx_from_parameters_block(self):
        raw = """
        temperature 0.2
        num_ctx 65536
        top_p 0.9
        """
        self.assertEqual(_parse_num_ctx(raw), 65536)

    def test_parse_num_ctx_from_modelfile_style_text(self):
        raw = "PARAMETER num_ctx 32768\nPARAMETER temperature 0.1"
        self.assertEqual(_parse_num_ctx(raw), 32768)

    def test_parse_ollama_ps_context_for_target_model(self):
        stdout = """NAME                    ID              SIZE      PROCESSOR    UNTIL              CONTEXT
gpt-oss:120b-cloud      abc123          73 GB     100% GPU     4 minutes from now 65536
llama3.2:latest         def456          2.0 GB    100% GPU     4 minutes from now 8192
"""
        self.assertEqual(_parse_ollama_ps_context(stdout, "gpt-oss:120b-cloud"), 65536)
        self.assertEqual(_parse_ollama_ps_context(stdout, "llama3.2:latest"), 8192)

    def test_normalize_tool_arguments_accepts_dict_and_json_string(self):
        self.assertEqual(_normalize_tool_arguments({"path": "a.txt"}), {"path": "a.txt"})
        self.assertEqual(_normalize_tool_arguments('{"path":"a.txt"}'), {"path": "a.txt"})
        self.assertEqual(_normalize_tool_arguments("not-json"), {})

    def test_provider_prefers_env_configuration(self):
        with patch.dict(
            "os.environ",
            {
                "KIM_OLLAMA_BASE_URL": "http://127.0.0.1:11434",
                "KIM_OLLAMA_MODE": "cloud",
                "KIM_OLLAMA_CLOUD_MODEL": DEFAULT_OLLAMA_CLOUD_MODEL,
            },
            clear=False,
        ):
            provider = OllamaProvider({"ollama": {"base_url": "http://localhost:11434", "mode": "local"}})
        self.assertEqual(provider._base_url, "http://127.0.0.1:11434")
        self.assertEqual(provider._mode, "cloud")
        self.assertEqual(provider._cloud_model, DEFAULT_OLLAMA_CLOUD_MODEL)

    def test_image_content_uses_ollama_images_field_not_text_json(self):
        provider = OllamaProvider({"ollama": {"mode": "cloud"}})
        converted = provider._to_ollama_messages(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Screenshot captured."},
                        {"type": "image", "data": "abc123", "media_type": "image/png"},
                    ],
                }
            ],
            "",
        )
        self.assertEqual(converted, [{"role": "user", "content": "Screenshot captured.", "images": ["abc123"]}])
        self.assertNotIn("abc123", converted[0]["content"])

    def test_image_content_strips_data_url_prefix(self):
        self.assertEqual(
            _normalize_image_data("data:image/png;base64,abc123"),
            "abc123",
        )

    def test_tool_call_transcript_uses_ollama_native_roles(self):
        provider = OllamaProvider({"ollama": {"mode": "cloud"}})
        converted = provider._to_ollama_messages(
            [
                {
                    "role": "assistant",
                    "content": '{"type":"tool_call","tool":"observe_ui","args":{"target":"screen"},"usage":{"input":99}}',
                },
                {
                    "role": "user",
                    "content": "[Tool result: observe_ui]\n{\"ok\":true}",
                },
            ],
            "",
        )
        self.assertEqual(converted[0]["role"], "assistant")
        self.assertEqual(converted[0]["tool_calls"][0]["function"]["name"], "observe_ui")
        self.assertNotIn("usage", converted[0]["content"])
        # The tool result must reference the SAME id as the tool call it answers.
        call_id = converted[0]["tool_calls"][0]["id"]
        self.assertEqual(
            converted[1],
            {"role": "tool", "tool_call_id": call_id, "content": '{"ok":true}'},
        )

    def test_screenshot_tool_result_stays_image_message(self):
        provider = OllamaProvider({"ollama": {"mode": "cloud"}})
        converted = provider._to_ollama_messages(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "[Tool result: take_screenshot]\nScreenshot captured."},
                        {"type": "image", "data": "abc123", "media_type": "image/png"},
                    ],
                }
            ],
            "",
        )
        self.assertEqual(converted[0]["role"], "user")
        self.assertEqual(converted[0]["images"], ["abc123"])

    # ── Additional normalization edge case tests ─────────────────────────────

    def test_normalize_tool_arguments_handles_empty_dict(self):
        self.assertEqual(_normalize_tool_arguments({}), {})

    def test_normalize_tool_arguments_handles_none(self):
        self.assertEqual(_normalize_tool_arguments(None), {})

    def test_normalize_tool_arguments_handles_empty_string(self):
        self.assertEqual(_normalize_tool_arguments(""), {})

    def test_normalize_tool_arguments_handles_nested_json(self):
        nested = '{"path": "file.py", "content": "line1\\nline2"}'
        result = _normalize_tool_arguments(nested)
        self.assertEqual(result["path"], "file.py")
        self.assertIn("line1", result["content"])

    def test_normalize_image_data_strips_jpeg_prefix(self):
        self.assertEqual(
            _normalize_image_data("data:image/jpeg;base64,xyz789"),
            "xyz789",
        )

    def test_normalize_image_data_returns_plain_data_unchanged(self):
        self.assertEqual(_normalize_image_data("abc123"), "abc123")

    def test_parse_num_ctx_returns_none_for_missing(self):
        raw = "temperature 0.2\ntop_p 0.9\n"
        self.assertIsNone(_parse_num_ctx(raw))

    def test_parse_ollama_ps_context_returns_none_for_missing_model(self):
        stdout = """NAME                    ID              SIZE      PROCESSOR    UNTIL              CONTEXT
gpt-oss:120b-cloud      abc123          73 GB     100% GPU     4 minutes from now 65536
"""
        self.assertIsNone(_parse_ollama_ps_context(stdout, "nonexistent:model"))

    def test_plain_text_message_converts_unchanged(self):
        provider = OllamaProvider({"ollama": {"mode": "cloud"}})
        converted = provider._to_ollama_messages(
            [{"role": "user", "content": "Hello, world!"}],
            "",
        )
        self.assertEqual(len(converted), 1)
        self.assertEqual(converted[0]["role"], "user")
        self.assertEqual(converted[0]["content"], "Hello, world!")

    def test_multiple_images_all_extracted(self):
        provider = OllamaProvider({"ollama": {"mode": "cloud"}})
        converted = provider._to_ollama_messages(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Two screenshots"},
                        {"type": "image", "data": "img1", "media_type": "image/png"},
                        {"type": "image", "data": "img2", "media_type": "image/png"},
                    ],
                }
            ],
            "",
        )
        self.assertEqual(converted[0]["images"], ["img1", "img2"])


class ToolResultMessageContractTests(unittest.TestCase):
    """Contract tests for _tool_result_message schema correctness.

    The OpenAI-compatible Ollama API requires role='tool' messages to carry
    'tool_call_id', not 'tool_name'.  Using 'tool_name' is a non-standard field
    that well-behaved Ollama models and cloud-compatible endpoints silently ignore
    or reject — the fix uses the spec-correct key name.
    """

    def test_uses_tool_call_id_key_not_tool_name(self):
        """Spec requires 'tool_call_id', not 'tool_name', in role='tool' messages."""
        result = _tool_result_message("user", "[Tool result: shell_run]\nsome output")
        self.assertIsNotNone(result)
        self.assertIn("tool_call_id", result)
        self.assertNotIn("tool_name", result)

    def test_tool_call_id_value_is_tool_name(self):
        """Without a stored call ID, the tool name is used as the identifier."""
        result = _tool_result_message("user", "[Tool result: read_file]\ncontents")
        self.assertEqual(result["tool_call_id"], "read_file")

    def test_body_extracted_correctly(self):
        result = _tool_result_message("user", "[Tool result: shell_run]\nline1\nline2")
        self.assertEqual(result["content"], "line1\nline2")

    def test_role_is_tool(self):
        result = _tool_result_message("user", "[Tool result: git_status]\nmodified: x.py")
        self.assertEqual(result["role"], "tool")

    def test_non_user_role_returns_none(self):
        self.assertIsNone(_tool_result_message("assistant", "[Tool result: click]\nok"))

    def test_non_matching_text_returns_none(self):
        self.assertIsNone(_tool_result_message("user", "plain message without tool result prefix"))

    def test_empty_body_is_preserved(self):
        result = _tool_result_message("user", "[Tool result: take_screenshot]\n")
        self.assertIsNotNone(result)
        self.assertEqual(result["content"], "")


class AccumulateToolCallDeltaTests(unittest.TestCase):
    """Regression tests for _accumulate_tool_call_delta streaming accumulator."""

    def test_wholeblock_dict_arguments_stored(self):
        """A whole-block dict 'arguments' is stored as-is on first occurrence."""
        acc: dict = {}
        delta = {"function": {"name": "read_file", "arguments": {"path": "a.txt"}}}
        _accumulate_tool_call_delta(acc, delta)
        self.assertEqual(acc["function"]["arguments"], {"path": "a.txt"})

    def test_wholeblock_dict_arguments_not_overwritten_by_second_chunk(self):
        """A second whole-block dict chunk must NOT overwrite the already-stored dict."""
        acc: dict = {}
        _accumulate_tool_call_delta(acc, {"function": {"name": "read_file", "arguments": {"path": "a.txt"}}})
        _accumulate_tool_call_delta(acc, {"function": {"arguments": {"path": "other.txt"}}})
        # First chunk wins for dict arguments.
        self.assertEqual(acc["function"]["arguments"], {"path": "a.txt"})

    def test_delta_string_fragments_concatenated(self):
        """Multiple string 'arguments' deltas are concatenated in arrival order."""
        acc: dict = {}
        _accumulate_tool_call_delta(acc, {"function": {"name": "shell_run", "arguments": '{"cmd":'}})
        _accumulate_tool_call_delta(acc, {"function": {"arguments": '"ls"}'}})
        self.assertEqual(acc["function"]["arguments"], '{"cmd":"ls"}')

    def test_name_and_index_handling(self):
        """Function name is captured from the delta; different 'index' values go to separate slots."""
        tool_calls: list[dict] = []

        # First tool call at index 0.
        delta0 = {"index": 0, "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}}
        idx = delta0.get("index", 0)
        while len(tool_calls) <= idx:
            tool_calls.append({})
        _accumulate_tool_call_delta(tool_calls[idx], delta0)

        # Second tool call at index 1.
        delta1 = {"index": 1, "function": {"name": "shell_run", "arguments": '{"cmd":"ls"}'}}
        idx = delta1.get("index", 0)
        while len(tool_calls) <= idx:
            tool_calls.append({})
        _accumulate_tool_call_delta(tool_calls[idx], delta1)

        self.assertEqual(len(tool_calls), 2)
        self.assertEqual(tool_calls[0]["function"]["name"], "read_file")
        self.assertEqual(tool_calls[0]["function"]["arguments"], '{"path":"a.txt"}')
        self.assertEqual(tool_calls[1]["function"]["name"], "shell_run")
        self.assertEqual(tool_calls[1]["function"]["arguments"], '{"cmd":"ls"}')

    def test_top_level_id_type_preserved(self):
        """id and type from the first chunk that carries them are not overwritten by later chunks."""
        acc: dict = {}
        _accumulate_tool_call_delta(
            acc,
            {"id": "call_42", "type": "function", "function": {"name": "foo", "arguments": ""}},
        )
        # Second chunk also has id/type — they must be ignored.
        _accumulate_tool_call_delta(
            acc,
            {"id": "call_99", "type": "other", "function": {"arguments": '{"x":1}'}},
        )
        self.assertEqual(acc["id"], "call_42")
        self.assertEqual(acc["type"], "function")


if __name__ == "__main__":
    unittest.main()


import asyncio
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
    _resolve_tool_call_index,
    _tool_result_message,
)


def _ps_table(rows: list[list[str]]) -> str:
    """Render a column-aligned `ollama ps` table exactly like the CLI does.

    Real header order (verified against `ollama ps`): CONTEXT precedes UNTIL,
    and UNTIL ("4 minutes from now") is the LAST column — so a naive last-token
    parse grabs "now" (issue #30). Fixed column widths guarantee the CONTEXT
    header and its values share a start offset, which is what the parser relies
    on.
    """
    headers = ["NAME", "ID", "SIZE", "PROCESSOR", "CONTEXT", "UNTIL"]
    widths = [22, 12, 9, 12, 10, 20]

    def fmt(cells: list[str]) -> str:
        return "".join(str(c).ljust(w) for c, w in zip(cells, widths))

    return "\n".join([fmt(headers)] + [fmt(r) for r in rows]) + "\n"


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
        # Real column order: ... CONTEXT UNTIL, with UNTIL ("4 minutes from
        # now") last. The old cols[-1] parse returned "now" and thus None (#30).
        stdout = _ps_table([
            ["gpt-oss:120b-cloud", "abc123", "73 GB", "100% GPU", "65536", "4 minutes from now"],
            ["llama3.2:latest", "def456", "2.0 GB", "100% GPU", "8192", "4 minutes from now"],
        ])
        self.assertEqual(_parse_ollama_ps_context(stdout, "gpt-oss:120b-cloud"), 65536)
        self.assertEqual(_parse_ollama_ps_context(stdout, "llama3.2:latest"), 8192)

    def test_parse_ollama_ps_context_ignores_until_column_text(self):
        # Regression for #30: the UNTIL text must never be mistaken for CONTEXT.
        stdout = _ps_table([
            ["gpt-oss:120b-cloud", "abc123", "73 GB", "100% GPU", "131072", "Stopping..."],
        ])
        self.assertEqual(_parse_ollama_ps_context(stdout, "gpt-oss:120b-cloud"), 131072)

    def test_parse_ollama_ps_context_none_when_no_context_column(self):
        # Older `ollama ps` without a CONTEXT column: return None (fall back to
        # /api/show) rather than grabbing a wrong value.
        stdout = (
            "NAME                  ID          SIZE     PROCESSOR   UNTIL              \n"
            "gpt-oss:120b-cloud    abc123      73 GB    100% GPU    4 minutes from now \n"
        )
        self.assertIsNone(_parse_ollama_ps_context(stdout, "gpt-oss:120b-cloud"))

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
        stdout = _ps_table([
            ["gpt-oss:120b-cloud", "abc123", "73 GB", "100% GPU", "65536", "4 minutes from now"],
        ])
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

    def test_text_only_fallback_is_honest_and_uses_window_context(self):
        provider = OllamaProvider({"ollama": {"mode": "cloud"}})
        cleaned = provider._strip_images_from_messages(
            [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "[Fallback context — the open windows are:]\nEditor"},
                    {"type": "image", "data": "abc123", "media_type": "image/png"},
                ],
            }]
        )

        text = " ".join(item["text"] for item in cleaned[0]["content"])
        self.assertIn("I couldn't grab a screenshot, but you have these windows open:", text)
        self.assertIn("open-window fallback", text)
        self.assertNotIn("observe_ui", text)
        self.assertNotIn("run_command", text)


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


class ResolveToolCallIndexTests(unittest.TestCase):
    """#38: two tool calls arriving in one Ollama message must not collapse."""

    def test_explicit_top_level_index_wins(self):
        self.assertEqual(_resolve_tool_call_index({"index": 3}, 0), 3)

    def test_explicit_function_index_wins(self):
        self.assertEqual(_resolve_tool_call_index({"function": {"index": 2}}, 0), 2)

    def test_falls_back_to_array_position_not_zero(self):
        # Ollama native /api/chat omits the index; two whole calls in one
        # message must land in slots 0 and 1, not both in 0.
        self.assertEqual(_resolve_tool_call_index({"function": {"name": "a"}}, 0), 0)
        self.assertEqual(_resolve_tool_call_index({"function": {"name": "b"}}, 1), 1)

    def test_bool_is_not_treated_as_index(self):
        # True is an int subclass; it must not be read as index 1.
        self.assertEqual(_resolve_tool_call_index({"index": True}, 5), 5)

    def test_two_indexless_calls_in_one_message_go_to_separate_slots(self):
        deltas = [
            {"function": {"name": "read_file", "arguments": {"path": "a.txt"}}},
            {"function": {"name": "shell_run", "arguments": {"cmd": "ls"}}},
        ]
        tool_calls: list[dict] = []
        for pos, delta in enumerate(deltas):
            idx = _resolve_tool_call_index(delta, pos)
            while len(tool_calls) <= idx:
                tool_calls.append({})
            _accumulate_tool_call_delta(tool_calls[idx], delta)
        self.assertEqual(len(tool_calls), 2)
        self.assertEqual(tool_calls[0]["function"]["name"], "read_file")
        self.assertEqual(tool_calls[1]["function"]["name"], "shell_run")


class InterleavedToolResultPairingTests(unittest.TestCase):
    """#40: interleaved tool calls each keep their own id in _to_ollama_messages."""

    def test_two_pending_calls_pair_to_their_own_ids(self):
        provider = OllamaProvider({"ollama": {"mode": "cloud"}})
        messages = [
            {"role": "assistant", "content": '{"type":"tool_call","tool":"read_file","args":{"path":"a"}}'},
            {"role": "assistant", "content": '{"type":"tool_call","tool":"shell_run","args":{"cmd":"ls"}}'},
            {"role": "user", "content": "[Tool result: read_file]\nfile contents"},
            {"role": "user", "content": "[Tool result: shell_run]\nlisting"},
        ]
        out = provider._to_ollama_messages(messages, "")
        tool_msgs = [m for m in out if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 2)
        # read_file was call_0, shell_run was call_1; each result pairs to its own id.
        by_content = {m["content"]: m["tool_call_id"] for m in tool_msgs}
        assistant_ids = [m["tool_calls"][0]["id"] for m in out if m.get("role") == "assistant"]
        self.assertEqual(by_content["file contents"], assistant_ids[0])
        self.assertEqual(by_content["listing"], assistant_ids[1])

    def test_same_tool_twice_pairs_fifo(self):
        provider = OllamaProvider({"ollama": {"mode": "cloud"}})
        messages = [
            {"role": "assistant", "content": '{"type":"tool_call","tool":"read_file","args":{"path":"a"}}'},
            {"role": "assistant", "content": '{"type":"tool_call","tool":"read_file","args":{"path":"b"}}'},
            {"role": "user", "content": "[Tool result: read_file]\nAAA"},
            {"role": "user", "content": "[Tool result: read_file]\nBBB"},
        ]
        out = provider._to_ollama_messages(messages, "")
        assistant_ids = [m["tool_calls"][0]["id"] for m in out if m.get("role") == "assistant"]
        tool_msgs = [m for m in out if m.get("role") == "tool"]
        # FIFO: first result → first call's id, second → second call's id.
        self.assertEqual(tool_msgs[0]["tool_call_id"], assistant_ids[0])
        self.assertEqual(tool_msgs[1]["tool_call_id"], assistant_ids[1])
        self.assertNotEqual(assistant_ids[0], assistant_ids[1])


async def _run_complete(provider, *, final_obj, content, tool_calls, messages=None, tools=None):
    """Drive OllamaProvider.complete() with the network layer stubbed out."""
    async def _noop_daemon():
        return None

    async def _fake_stream(_payload):
        return final_obj, content, tool_calls

    async def _fake_usage(_final, _model):
        return {"provider": "ollama", "source": "ollama", "mode": "cloud"}

    with patch.object(provider, "_ensure_daemon_running", _noop_daemon), \
         patch.object(provider, "_stream_chat", _fake_stream), \
         patch.object(provider, "_usage_from_final", _fake_usage):
        return await provider.complete(messages or [{"role": "user", "content": "hi"}], tools or [], "sys")


class OllamaDoneReasonTests(unittest.TestCase):
    """F-B-3: Ollama must surface done_reason and finalize truncated text."""

    def _provider(self):
        return OllamaProvider({"ollama": {"mode": "cloud", "cloud_model": "m:cloud"}})

    def test_length_stop_annotates_truncated_text(self):
        provider = self._provider()
        result = asyncio.run(_run_complete(
            provider,
            final_obj={"done_reason": "length", "model": "m:cloud"},
            content="half an answer",
            tool_calls=[],
        ))
        self.assertEqual(result["type"], "text")
        self.assertEqual(result["stop_reason"], "length")
        self.assertIn("truncated", result["content"].lower())
        self.assertTrue(result["content"].startswith("half an answer"))

    def test_normal_stop_passes_text_through_with_stop_reason(self):
        provider = self._provider()
        result = asyncio.run(_run_complete(
            provider,
            final_obj={"done_reason": "stop", "model": "m:cloud"},
            content="complete answer",
            tool_calls=[],
        ))
        self.assertEqual(result["content"], "complete answer")
        self.assertEqual(result["stop_reason"], "stop")

    def test_tool_call_carries_stop_reason(self):
        provider = self._provider()
        result = asyncio.run(_run_complete(
            provider,
            final_obj={"done_reason": "stop", "model": "m:cloud"},
            content="",
            tool_calls=[{"function": {"name": "read_file", "arguments": {"path": "a"}}}],
        ))
        self.assertEqual(result["type"], "tool_call")
        self.assertEqual(result["tool"], "read_file")
        self.assertEqual(result["stop_reason"], "stop")


class OllamaTimeoutClassificationTests(unittest.TestCase):
    """F-B-5: httpx transport timeouts must become retryable builtin TimeoutError."""

    def test_stream_chat_reraises_httpx_timeout_as_timeouterror(self):
        import httpx
        from orchestrator.providers.base import classify_provider_error

        provider = OllamaProvider({"ollama": {"mode": "cloud", "cloud_model": "m:cloud"}})

        async def _boom(_payload):
            raise httpx.ReadTimeout("timed out")

        async def _drive():
            with patch.object(provider, "_stream_chat_inner", _boom):
                await provider._stream_chat({"model": "m:cloud"})

        with self.assertRaises(TimeoutError) as ctx:
            asyncio.run(_drive())
        # And the agent's retry boundary classifies it as retryable.
        classified = classify_provider_error(ctx.exception)
        self.assertEqual(classified.code, "timeout")
        self.assertTrue(classified.retryable)


if __name__ == "__main__":
    unittest.main()


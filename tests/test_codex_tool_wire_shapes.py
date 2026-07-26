"""Wire-shape contracts for the tool calls the proxy hands back to codex.

Every expectation here was pinned empirically against the real codex 0.144.3
binary by answering its /v1/responses request with a fabricated tool call and
observing whether the router accepted it:

    function_call  "exec"          -> ERROR "unsupported call: exec"
    function_call  "exec_command"  -> ran the command
    function_call  "apply_patch"   -> ERROR "Fatal error: tool apply_patch
                                      invoked with incompatible payload"
    custom_tool_call "apply_patch" -> patch applied, file created
    function_call  "update_plan"   -> plan accepted

The apply_patch row is the one that mattered: `_make_responses_tool_reply`
used to emit *every* call as a function_call, so the single tool codex's own
system prompt tells the model to prefer for edits ("Use `apply_patch` for
local file edits") was guaranteed to fail.
"""
import unittest

from codex_engine.engine import (
    _custom_tool_names,
    _freeform_tool_input,
    _has_routable_tools,
    _make_responses_tool_reply,
    _provider_response_to_responses_api,
    _reply_has_tool_calls,
)

# Trimmed from a real captured codex request (model=gpt-5.5). Shapes verbatim.
CODEX_TOOLS = [
    {
        "type": "function",
        "name": "exec_command",
        "description": "Runs a command in a PTY, returning output or a session ID.",
        "parameters": {
            "type": "object",
            "properties": {"cmd": {"type": "string"}},
            "required": ["cmd"],
        },
    },
    {
        "type": "custom",
        "name": "apply_patch",
        "description": "Use the `apply_patch` tool to edit files. This is a FREEFORM tool.",
        "format": {"type": "grammar", "syntax": "lark", "definition": "start: ..."},
    },
    {"type": "web_search"},
]

PATCH = "*** Begin Patch\n*** Add File: a.txt\n+hi\n*** End Patch\n"


def _items(reply):
    return reply["output"]


class CustomToolShapeTests(unittest.TestCase):
    def test_apply_patch_is_emitted_as_custom_tool_call(self):
        reply = _make_responses_tool_reply(
            "resp_1", "", [{"name": "apply_patch", "input": {"patch": PATCH}}],
            CODEX_TOOLS,
        )
        item = _items(reply)[0]
        self.assertEqual(item["type"], "custom_tool_call")
        self.assertEqual(item["name"], "apply_patch")
        # Raw patch text, NOT a JSON blob — the lark grammar cannot parse JSON.
        self.assertEqual(item["input"], PATCH)
        self.assertNotIn("arguments", item)

    def test_function_tool_still_uses_function_call(self):
        reply = _make_responses_tool_reply(
            "resp_1", "", [{"name": "exec_command", "input": {"cmd": "ls"}}],
            CODEX_TOOLS,
        )
        item = _items(reply)[0]
        self.assertEqual(item["type"], "function_call")
        self.assertEqual(item["arguments"], '{"cmd": "ls"}')

    def test_unknown_tool_defaults_to_function_call(self):
        reply = _make_responses_tool_reply(
            "resp_1", "", [{"name": "mystery", "input": {"a": 1}}], CODEX_TOOLS
        )
        self.assertEqual(_items(reply)[0]["type"], "function_call")

    def test_without_a_request_tool_list_nothing_becomes_custom(self):
        # Back-compat: the 3-arg call shape used across the existing suite.
        reply = _make_responses_tool_reply(
            "resp_1", "", [{"name": "apply_patch", "input": {"patch": PATCH}}]
        )
        self.assertEqual(_items(reply)[0]["type"], "function_call")

    def test_custom_tool_names_reads_the_type_field(self):
        self.assertEqual(_custom_tool_names(CODEX_TOOLS), {"apply_patch"})
        self.assertEqual(_custom_tool_names(None), set())
        self.assertEqual(_custom_tool_names([]), set())

    def test_text_and_call_both_survive(self):
        reply = _make_responses_tool_reply(
            "resp_1", "editing", [{"name": "apply_patch", "input": PATCH}], CODEX_TOOLS
        )
        types = [i["type"] for i in _items(reply)]
        self.assertEqual(types, ["message", "custom_tool_call"])


class FreeformInputFlatteningTests(unittest.TestCase):
    def test_plain_string_passes_through(self):
        self.assertEqual(_freeform_tool_input(PATCH), PATCH)

    def test_named_wrapper_keys_are_unwrapped(self):
        for key in ("input", "patch", "text", "content", "body", "cmd"):
            self.assertEqual(_freeform_tool_input({key: PATCH}), PATCH, key)

    def test_input_wins_over_other_keys(self):
        self.assertEqual(
            _freeform_tool_input({"text": "no", "input": PATCH}), PATCH
        )

    def test_single_unrecognised_string_value_is_unwrapped(self):
        self.assertEqual(_freeform_tool_input({"weird_key": PATCH}), PATCH)

    def test_ambiguous_dict_falls_back_to_json(self):
        out = _freeform_tool_input({"a": "x", "b": "y"})
        self.assertIn('"a"', out)
        self.assertIn('"b"', out)


class RoutableToolDetectionTests(unittest.TestCase):
    """gpt-5.6-* are code_mode_only and send no tools at all."""

    def test_real_codex_tool_list_is_routable(self):
        self.assertTrue(_has_routable_tools(CODEX_TOOLS))

    def test_code_mode_sends_nothing_to_route(self):
        self.assertFalse(_has_routable_tools([]))
        self.assertFalse(_has_routable_tools(None))
        self.assertFalse(_has_routable_tools("tools"))

    def test_reply_has_tool_calls_spots_both_shapes(self):
        for kind in ("function_call", "custom_tool_call"):
            self.assertTrue(_reply_has_tool_calls({"output": [{"type": kind}]}), kind)

    def test_reply_has_tool_calls_ignores_plain_text(self):
        reply = {"output": [{"type": "message", "role": "assistant", "content": []}]}
        self.assertFalse(_reply_has_tool_calls(reply))
        self.assertFalse(_reply_has_tool_calls({"output": []}))
        self.assertFalse(_reply_has_tool_calls(None))


class NamelessToolEntryTests(unittest.TestCase):
    """Codex sends {"type": "web_search"} / {"type": "tool_search"} with no name.

    A hard `t["name"]` on those raised `KeyError: 'name'`, which surfaced to
    codex as `502 Bad Gateway: LLM call failed: 'name'` on every single relay.
    It stayed invisible while the configured model was code-mode-only (zero
    tools advertised) and appeared the instant a real tool list arrived.
    """

    def test_format_prompt_survives_nameless_tools(self):
        from orchestrator.providers.browser.prompt_builder import format_prompt

        prompt, _attachments, _hash, _sent = format_prompt(
            [{"role": "user", "content": "hi"}],
            CODEX_TOOLS,
            "sys",
            sent_system_prompt=False,
            max_inject_chars=10000,
            use_webview_bridge=True,
        )
        # Named tools are described, and the nameless one degrades to its type
        # rather than crashing or vanishing silently.
        self.assertIn("exec_command", prompt)
        self.assertIn("web_search", prompt)

    def test_known_tools_allowlist_skips_nameless_entries(self):
        known = {t["name"] for t in CODEX_TOOLS if "name" in t}
        self.assertEqual(known, {"exec_command", "apply_patch"})


class EndToEndConversionTests(unittest.TestCase):
    def test_contract_reply_naming_apply_patch_converts_to_custom_call(self):
        content = (
            '{"text": "editing", "tool_calls": '
            '[{"name": "apply_patch", "input": {"patch": '
            '"*** Begin Patch\\n*** Add File: a.txt\\n+hi\\n*** End Patch\\n"}}]}'
        )
        reply = _provider_response_to_responses_api(
            {"type": "text", "content": content}, 1, request_tools=CODEX_TOOLS
        )
        calls = [i for i in _items(reply) if i["type"] == "custom_tool_call"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "apply_patch")
        self.assertTrue(calls[0]["input"].startswith("*** Begin Patch"))

    def test_exec_alias_still_normalizes_onto_exec_command(self):
        # The salvage ladder emits the sentinel name "exec"; codex only routes
        # "exec_command" (verified: "unsupported call: exec").
        content = '{"text": "", "tool_calls": [{"name": "exec", "input": {"cmd": "ls"}}]}'
        reply = _provider_response_to_responses_api(
            {"type": "text", "content": content}, 1, request_tools=CODEX_TOOLS
        )
        call = [i for i in _items(reply) if i["type"] == "function_call"][0]
        self.assertEqual(call["name"], "exec_command")


if __name__ == "__main__":
    unittest.main()

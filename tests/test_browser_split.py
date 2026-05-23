"""
Tests for the browser provider split modules.

Validates that the extracted modules (response_parser, prompt_builder,
site_configs, bridge_client) work correctly in isolation — no playwright
or network required.
"""

import unittest


class TestStripTransportMarkers(unittest.TestCase):
    def setUp(self):
        from orchestrator.providers.browser.response_parser import strip_transport_markers
        self.strip = strip_transport_markers

    def test_removes_completion_hash(self):
        text = 'Hello world[END_OF_RESPONSE_abc12345]'
        result = self.strip(text, '[END_OF_RESPONSE_abc12345]')
        assert result == 'Hello world'

    def test_removes_generic_end_markers(self):
        text = 'Response text [END_OF_RESPONSE] trailing'
        result = self.strip(text, '')
        assert 'END_OF_RESPONSE' not in result

    def test_keeps_last_fragment_on_multiple_markers(self):
        text = 'old[END_OF_RESPONSE_aaa]new content[END_OF_RESPONSE_bbb]'
        result = self.strip(text, '[END_OF_RESPONSE_bbb]')
        assert 'new content' in result

    def test_empty_input(self):
        assert self.strip('', 'hash') == ''

    def test_no_markers(self):
        assert self.strip('plain text', '') == 'plain text'


class TestTryParseToolJson(unittest.TestCase):
    def setUp(self):
        from orchestrator.providers.browser.response_parser import try_parse_tool_json
        self.parse = try_parse_tool_json

    def test_valid_tool_call(self):
        result = self.parse('{"tool": "click", "args": {"x": 100, "y": 200}}')
        assert result == {"type": "tool_call", "tool": "click", "args": {"x": 100, "y": 200}}

    def test_arguments_alias(self):
        result = self.parse('{"tool": "read_file", "arguments": {"path": "a.txt"}}')
        assert result["args"] == {"path": "a.txt"}

    def test_no_tool_key(self):
        result = self.parse('{"name": "click", "x": 100}')
        assert result is None

    def test_invalid_json(self):
        result = self.parse('not json at all')
        assert result is None

    def test_empty_args(self):
        result = self.parse('{"tool": "take_screenshot"}')
        assert result == {"type": "tool_call", "tool": "take_screenshot", "args": {}}


class TestScanForJsonMatch(unittest.TestCase):
    def setUp(self):
        from orchestrator.providers.browser.response_parser import scan_for_json_match
        self.scan = scan_for_json_match

    def test_finds_tool_json_in_prose(self):
        text = 'I will click now. {"tool": "click", "args": {"x": 10}} Done.'
        result = self.scan(text)
        assert result is not None
        parsed, start, end = result
        assert parsed["tool"] == "click"
        assert text[start:end] == '{"tool": "click", "args": {"x": 10}}'

    def test_no_tool_json(self):
        assert self.scan('no json here') is None

    def test_nested_braces(self):
        text = '{"tool": "write_file", "args": {"path": "a.py", "content": "x = {1: 2}"}}'
        result = self.scan(text)
        assert result is not None
        assert result[0]["tool"] == "write_file"

    def test_non_tool_json_skipped(self):
        text = '{"name": "foo"} then {"tool": "click", "args": {}}'
        result = self.scan(text)
        assert result is not None
        assert result[0]["tool"] == "click"


class TestParseResponse(unittest.TestCase):
    def setUp(self):
        from orchestrator.providers.browser.response_parser import parse_response
        self.parse = parse_response

    def test_task_complete(self):
        result = self.parse('TASK_COMPLETE: opened notepad', '')
        assert result["type"] == "text"
        assert "TASK_COMPLETE:" in result["content"]

    def test_need_help(self):
        result = self.parse('NEED_HELP: cannot find button', '')
        assert result["type"] == "text"
        assert "NEED_HELP:" in result["content"]

    def test_fenced_json(self):
        text = 'Let me click.\n```json\n{"tool": "click", "args": {"x": 5}}\n```'
        result = self.parse(text, '')
        assert result["type"] == "tool_call"
        assert result["tool"] == "click"

    def test_bare_json(self):
        result = self.parse('{"tool": "take_screenshot", "args": {}}', '')
        assert result["type"] == "tool_call"
        assert result["tool"] == "take_screenshot"

    def test_plain_text_fallback(self):
        result = self.parse('Just some commentary.', '')
        assert result["type"] == "text"
        assert result["content"] == 'Just some commentary.'

    def test_strips_completion_hash(self):
        hash_val = '[END_OF_RESPONSE_deadbeef]'
        result = self.parse(f'TASK_COMPLETE: done{hash_val}', hash_val)
        assert hash_val not in result["content"]

    def test_surrounding_content_preserved(self):
        text = 'STEP 1: clicking\n{"tool": "click", "args": {"x": 1}}\nDONE 1: clicked'
        result = self.parse(text, '')
        assert result["type"] == "tool_call"
        assert "content" in result
        assert "STEP 1" in result["content"]


class TestFormatPrompt(unittest.TestCase):
    def setUp(self):
        from orchestrator.providers.browser.prompt_builder import format_prompt
        self.format = format_prompt

    def test_first_message_includes_system(self):
        messages = [{"role": "user", "content": "open notepad"}]
        tools = [{"name": "click", "description": "click", "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}}}]
        prompt, attachments, hash_, new_sent = self.format(
            messages, tools, "You are Kim.",
            sent_system_prompt=False, max_inject_chars=50000, use_webview_bridge=False,
        )
        assert "[SYSTEM]" in prompt
        assert "You are Kim." in prompt
        assert new_sent is True
        assert hash_.startswith("[END_OF_RESPONSE_")

    def test_subsequent_message_no_system(self):
        messages = [{"role": "user", "content": "next task"}]
        prompt, _, hash_, new_sent = self.format(
            messages, [], "",
            sent_system_prompt=True, max_inject_chars=50000, use_webview_bridge=False,
        )
        assert "[SYSTEM]" not in prompt
        assert "next task" in prompt
        assert new_sent is True

    def test_returns_four_elements(self):
        result = self.format(
            [{"role": "user", "content": "hi"}], [], "",
            sent_system_prompt=False, max_inject_chars=50000, use_webview_bridge=False,
        )
        assert len(result) == 4

    def test_data_uri_extracted(self):
        msg = "look at data:image/png;base64,iVBORw0KGgo= this"
        messages = [{"role": "user", "content": msg}]
        prompt, attachments, _, _ = self.format(
            messages, [], "",
            sent_system_prompt=True, max_inject_chars=50000, use_webview_bridge=False,
        )
        assert len(attachments) == 1
        assert attachments[0]["mime_type"] == "image/png"
        assert "iVBORw0KGgo=" not in prompt


class TestBuildHistoryRecap(unittest.TestCase):
    def setUp(self):
        from orchestrator.providers.browser.prompt_builder import build_history_recap
        self.recap = build_history_recap

    def test_empty_messages(self):
        assert self.recap([]) == ""

    def test_basic_recap(self):
        msgs = [
            {"role": "user", "content": "Task: open calc"},
            {"role": "assistant", "content": "TASK_COMPLETE: opened calc"},
        ]
        result = self.recap(msgs)
        assert "User: open calc" in result
        assert "Kim: TASK_COMPLETE" in result

    def test_tool_results_skipped(self):
        msgs = [
            {"role": "user", "content": "[Tool result: success]"},
        ]
        assert self.recap(msgs) == ""

    def test_truncation(self):
        msgs = [{"role": "user", "content": "x" * 5000}]
        result = self.recap(msgs, max_recap=100)
        assert len(result) <= 110  # max_recap + prefix


class TestSiteConfigs(unittest.TestCase):
    def test_all_required_sites(self):
        from orchestrator.providers.browser.site_configs import SITE_CONFIGS
        for name in ("claude", "chatgpt", "gemini", "deepseek", "grok"):
            assert name in SITE_CONFIGS, f"Missing site: {name}"

    def test_site_has_required_keys(self):
        from orchestrator.providers.browser.site_configs import SITE_CONFIGS
        required = {"url_pattern", "input_selectors", "send_selectors", "response_selectors", "stop_selectors"}
        for name, cfg in SITE_CONFIGS.items():
            for key in required:
                assert key in cfg, f"{name} missing {key}"

    def test_to_list(self):
        from orchestrator.providers.browser.site_configs import to_list
        assert to_list("a") == ["a"]
        assert to_list(["a", "b"]) == ["a", "b"]
        assert to_list("") == []
        assert to_list(None) == []


class TestReExportShim(unittest.TestCase):
    def test_site_configs_importable_from_shim(self):
        from orchestrator.providers.browser_provider import SITE_CONFIGS
        assert "claude" in SITE_CONFIGS

    def test_browser_provider_lazy_import(self):
        import orchestrator.providers.browser_provider as mod
        assert "BrowserProvider" in mod.__all__

    def test_unknown_attr_raises(self):
        import orchestrator.providers.browser_provider as mod
        with self.assertRaises(AttributeError):
            _ = mod.NonExistentThing


if __name__ == "__main__":
    unittest.main()

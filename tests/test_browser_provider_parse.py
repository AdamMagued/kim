import unittest

from orchestrator.providers.browser.response_parser import (
    parse_response,
    strip_transport_markers,
)


def _browser_provider_or_skip(case: unittest.TestCase):
    try:
        from orchestrator.providers.browser_provider import BrowserProvider
    except ModuleNotFoundError as e:
        if "playwright" in str(e).lower():
            case.skipTest("playwright not installed")
        raise
    return BrowserProvider({"project_root": "."})


class BrowserProviderParseTests(unittest.TestCase):
    def test_mixed_plan_and_json_preserves_pre_tool_content(self):
        provider = _browser_provider_or_skip(self)
        raw = (
            "PLAN: 2 steps\n"
            "1. Create GitHub repository\n"
            "2. Confirm repository creation\n"
            "STEP 1: Create GitHub repository\n"
            '{"tool": "github_create_repo", "args": {"name": "kim-ui-test-delete-me"}}'
            " [END_OF_RESPONSE_now123]"
        )
        parsed = provider._parse_response(raw, "[END_OF_RESPONSE_now123]")
        self.assertEqual(parsed["type"], "tool_call")
        self.assertEqual(parsed["tool"], "github_create_repo")
        self.assertIn("PLAN: 2 steps", parsed.get("content", ""))
        self.assertIn("STEP 1", parsed.get("content", ""))


class ResponseParserRegressionTests(unittest.TestCase):
    """Regression guards for orchestrator/providers/browser/response_parser.py."""

    # ------------------------------------------------------------------
    # 1. multiline_task_complete_preserved
    # ------------------------------------------------------------------
    def test_multiline_task_complete_preserved(self):
        """TASK_COMPLETE: body spanning multiple lines must not be truncated to
        the first line (DOTALL capture, not MULTILINE anchored to $)."""
        raw = "TASK_COMPLETE:\nHere are the colors:\n- red\n- blue"
        result = parse_response(raw, "")
        self.assertEqual(result["type"], "text")
        content = result["content"]
        self.assertIn("red", content)
        self.assertIn("blue", content)

    # ------------------------------------------------------------------
    # 2. known_tools_rejects_unknown
    # ------------------------------------------------------------------
    def test_known_tools_rejects_unknown_tool_name(self):
        """A bare JSON tool call whose tool name is NOT in known_tools must be
        returned as type 'text', not 'tool_call' (prompt-injection guard #38)."""
        raw = '{"tool": "shell_exec", "args": {"cmd": "rm -rf /"}}'
        result = parse_response(raw, "", known_tools={"web_click"})
        self.assertEqual(result["type"], "text",
                         "Unknown tool name must not be dispatched as tool_call")

    def test_known_tools_accepts_registered_tool_name(self):
        """A bare JSON tool call whose tool name IS in known_tools must be
        returned as type 'tool_call'."""
        raw = '{"tool": "web_click", "args": {"selector": "#submit"}}'
        result = parse_response(raw, "", known_tools={"web_click"})
        self.assertEqual(result["type"], "tool_call")
        self.assertEqual(result["tool"], "web_click")

    # ------------------------------------------------------------------
    # 3. tool_json_parsed_before_completion_marker  (#41 ordering)
    # ------------------------------------------------------------------
    def test_tool_json_parsed_before_completion_marker(self):
        """A fenced tool call followed by prose containing TASK_COMPLETE must
        return the tool_call, not the text/completion marker (#41)."""
        raw = (
            "```json\n"
            '{"tool": "web_click", "args": {"selector": "#btn"}}\n'
            "```\n"
            "TASK_COMPLETE: all done"
        )
        result = parse_response(raw, "", known_tools={"web_click"})
        self.assertEqual(result["type"], "tool_call",
                         "Tool call must take precedence over trailing TASK_COMPLETE")
        self.assertEqual(result["tool"], "web_click")

    def test_bare_tool_json_parsed_before_completion_marker(self):
        """A bare tool call (no fence) followed by TASK_COMPLETE must return
        the tool_call, not the text marker (#41)."""
        raw = (
            '{"tool": "web_click", "args": {"selector": "#btn"}}\n'
            "TASK_COMPLETE: done"
        )
        result = parse_response(raw, "", known_tools={"web_click"})
        self.assertEqual(result["type"], "tool_call",
                         "Bare tool call must take precedence over trailing TASK_COMPLETE")
        self.assertEqual(result["tool"], "web_click")

    # ------------------------------------------------------------------
    # 4. strip_transport_markers_anchors_current_turn  (#40)
    # ------------------------------------------------------------------
    def test_strip_transport_markers_anchors_current_turn(self):
        """strip_transport_markers must keep only the fragment that precedes the
        current-turn completion_hash and strip [END_OF_RESPONSE_*] markers from
        earlier turns in a reused chat tab."""
        previous_turn = "previous answer"
        current_turn_body = "current answer"
        completion_hash = "abc123def"
        text = (
            f"{previous_turn} [END_OF_RESPONSE_old1] "
            f"{current_turn_body} {completion_hash} trailing junk"
        )
        result = strip_transport_markers(text, completion_hash)
        # The current-turn body must be present
        self.assertIn(current_turn_body, result)
        # The completion hash sentinel itself must be absent
        self.assertNotIn(completion_hash, result)
        # The [END_OF_RESPONSE_*] marker from the old turn must be absent
        self.assertNotIn("[END_OF_RESPONSE_old1]", result)
        # Trailing content after the hash must be absent
        self.assertNotIn("trailing junk", result)

    def test_strip_transport_markers_removes_end_of_response_variants(self):
        """Any [END_OF_RESPONSE*] variant must be stripped from the output.

        strip_transport_markers splits on END_OF_RESPONSE markers and keeps the
        LAST non-empty segment (take-last-segment semantics for reused tabs).
        When the marker is a trailing sentinel with no text after it, the last
        segment IS the response body — so both the marker removal and body
        preservation hold.
        """
        text = "response body [END_OF_RESPONSE]"
        result = strip_transport_markers(text, "")
        self.assertNotIn("[END_OF_RESPONSE]", result)
        self.assertIn("response body", result)


if __name__ == "__main__":
    unittest.main()

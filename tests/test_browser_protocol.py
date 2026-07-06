import os
import unittest

from codex_engine.engine import (
    _chatgpt_terminal_system_prompt,
    _codex_browser_system_prompt,
    _provider_response_to_chat_completions,
    _provider_response_to_responses_api,
    _system_prompt_for,
)


def _browser_provider_or_skip(case: unittest.TestCase):
    """BrowserProvider imports playwright at module load — lazy import for CI."""
    try:
        from orchestrator.providers.browser_provider import BrowserProvider
    except ModuleNotFoundError as e:
        if "playwright" in str(e).lower():
            case.skipTest("playwright not installed")
        raise
    return BrowserProvider({"project_root": "."})


class BrowserProtocolTests(unittest.TestCase):
    def test_codex_prompt_has_no_static_plain_sentinel_contract(self):
        prompt = _codex_browser_system_prompt()
        self.assertNotIn("raw JSON followed by the [END_OF_RESPONSE] marker", prompt)
        self.assertNotIn("\n[END_OF_RESPONSE]\n", prompt)
        self.assertIn("omit tool_calls entirely", prompt)

    def test_empty_tool_calls_is_tolerated_as_final_responses_text(self):
        parsed = _provider_response_to_responses_api(
            {"type": "text", "content": '{"text":"done","tool_calls":[]}'},
            relay_num=1,
        )
        # A bare "done" is normalized to a clean "Done." for display — what
        # matters is it ends the turn as a text answer, not another relay.
        self.assertEqual(parsed["output"][0]["content"][0]["text"], "Done.")
        self.assertEqual(parsed["output"][0]["type"], "message")

    def test_tool_calls_are_mapped_to_responses_api(self):
        parsed = _provider_response_to_responses_api(
            {
                "type": "text",
                "content": '{"text":"use shell","tool_calls":[{"name":"shell","input":{"cmd":"ls"}}]}',
            },
            relay_num=1,
        )
        self.assertEqual(parsed["output"][1]["type"], "function_call")
        self.assertEqual(parsed["output"][1]["name"], "shell")

    def test_tool_calls_are_mapped_to_chat_completions(self):
        parsed = _provider_response_to_chat_completions(
            {
                "type": "text",
                "content": '{"text":"use shell","tool_calls":[{"name":"shell","input":{"cmd":"ls"}}]}',
            },
            relay_num=1,
        )
        message = parsed["choices"][0]["message"]
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "shell")

    def test_browser_provider_strips_current_hash_and_old_fragments(self):
        provider = _browser_provider_or_skip(self)
        text = 'old [END_OF_RESPONSE_old1] {"tool":"x","args":{}} [END_OF_RESPONSE_now123] trailing'
        self.assertEqual(
            provider._strip_transport_markers(text, "[END_OF_RESPONSE_now123]"),
            '{"tool":"x","args":{}}',
        )

    def test_codex_prompt_selects_codex_layout_not_chat_layout(self):
        """The prompt_builder detects the codex bridge by phrase-matching the
        system prompt ("codex bridge json mode"). If that coupling breaks, the
        chat-mode layout is used instead, which appends an empty
        [AVAILABLE TOOLS] block + TASK_COMPLETE instructions — honesty-tuned
        models then refuse: 'there are no tools available'."""
        provider = _browser_provider_or_skip(self)
        prompt, _attachments, _hash = provider._format_prompt(
            messages=[{"role": "user", "content": "make pong"}],
            tools=[],
            system=_codex_browser_system_prompt(),
        )
        self.assertNotIn("[AVAILABLE TOOLS]", prompt)
        self.assertNotIn("TASK_COMPLETE", prompt)
        self.assertNotIn("NEED_HELP", prompt)

    def test_system_prompt_selector_routes_chatgpt_to_terminal_mode(self):
        # ChatGPT → terminal-helper prompt; everyone else → JSON prompt.
        self.assertEqual(
            _system_prompt_for("browser:chatgpt"), _chatgpt_terminal_system_prompt()
        )
        self.assertEqual(
            _system_prompt_for("browser:gemini"), _codex_browser_system_prompt()
        )
        self.assertEqual(_system_prompt_for(""), _codex_browser_system_prompt())

    def test_terminal_prompt_asks_for_one_bash_command_and_done(self):
        prompt = _chatgpt_terminal_system_prompt()
        self.assertIn("ONE shell command", prompt)
        self.assertIn("```bash", prompt)
        self.assertIn("DONE", prompt)
        self.assertIn("printf", prompt)  # heredoc-avoidance guidance

    def test_terminal_prompt_requires_narration_line(self):
        # The narration line is the visible "thinking" in the Kim chat —
        # _surface_relay_reasoning shows the prose before the first fence.
        prompt = _chatgpt_terminal_system_prompt()
        self.assertIn("narration line", prompt)
        self.assertIn("codex bridge terminal", prompt)  # load-bearing phrase

    def test_terminal_prompt_selects_codex_layout_not_chat_layout(self):
        # Same load-bearing coupling as the JSON prompt: the terminal prompt
        # must also pick the codex layout, not the chat-mode one.
        provider = _browser_provider_or_skip(self)
        prompt, _attachments, _hash = provider._format_prompt(
            messages=[{"role": "user", "content": "make pong"}],
            tools=[],
            system=_chatgpt_terminal_system_prompt(),
        )
        self.assertNotIn("[AVAILABLE TOOLS]", prompt)
        self.assertNotIn("TASK_COMPLETE", prompt)

    def test_formatted_codex_prompt_has_only_dynamic_marker_instruction(self):
        provider = _browser_provider_or_skip(self)
        prompt, _attachments, completion_hash = provider._format_prompt(
            messages=[{"role": "user", "content": "say hi"}],
            tools=[],
            system=_codex_browser_system_prompt(),
        )
        self.assertIn(completion_hash, prompt)
        self.assertIn("append the exact string", prompt)
        self.assertNotIn("append [END_OF_RESPONSE]", prompt)
        self.assertNotRegex(prompt, r"\[END_OF_RESPONSE\](?!_)")

    def test_restored_browser_thread_uses_lighter_recap(self):
        previous = os.environ.get("KIM_BROWSER_RESTORE_STATUS")
        os.environ["KIM_BROWSER_RESTORE_STATUS"] = "stored_thread"
        try:
            provider = _browser_provider_or_skip(self)
            long_prior = "A" * 1500
            prompt, _attachments, _completion_hash = provider._format_prompt(
                messages=[
                    {"role": "user", "content": long_prior},
                    {"role": "assistant", "content": "Earlier answer"},
                    {"role": "user", "content": "continue"},
                ],
                tools=[],
                system="You are Kim.",
            )
            self.assertIn("[BRIEF PRIOR CONTEXT", prompt)
            self.assertNotIn("[PRIOR CONVERSATION", prompt)
            self.assertLess(prompt.count("A"), 300)
        finally:
            if previous is None:
                os.environ.pop("KIM_BROWSER_RESTORE_STATUS", None)
            else:
                os.environ["KIM_BROWSER_RESTORE_STATUS"] = previous

    def test_non_restored_browser_thread_keeps_full_recap_header(self):
        previous = os.environ.get("KIM_BROWSER_RESTORE_STATUS")
        os.environ.pop("KIM_BROWSER_RESTORE_STATUS", None)
        try:
            provider = _browser_provider_or_skip(self)
            prompt, _attachments, _completion_hash = provider._format_prompt(
                messages=[
                    {"role": "user", "content": "old task"},
                    {"role": "assistant", "content": "old answer"},
                    {"role": "user", "content": "continue"},
                ],
                tools=[],
                system="You are Kim.",
            )
            self.assertIn("[PRIOR CONVERSATION", prompt)
            self.assertNotIn("[BRIEF PRIOR CONTEXT", prompt)
        finally:
            if previous is None:
                os.environ.pop("KIM_BROWSER_RESTORE_STATUS", None)
            else:
                os.environ["KIM_BROWSER_RESTORE_STATUS"] = previous

    def test_browser_usage_estimates_non_zero_output_tokens(self):
        provider = _browser_provider_or_skip(self)
        result = provider._attach_usage(
            {"type": "text", "content": "Hello from Gemini."},
            {"input": 12, "estimated": True, "source": "browser_prompt"},
        )
        self.assertGreater(result["usage"]["output"], 0)
        self.assertEqual(result["usage"]["source"], "browser_prompt")


if __name__ == "__main__":
    unittest.main()

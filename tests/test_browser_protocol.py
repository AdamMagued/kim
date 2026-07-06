import os
import unittest

from codex_engine.engine import (
    _codex_browser_system_prompt,
    _provider_response_to_chat_completions,
    _provider_response_to_responses_api,
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
        self.assertEqual(parsed["output"][0]["content"][0]["text"], "done")

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

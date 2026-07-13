"""
The browser prompt's [INSTRUCTIONS] block carries a one-line auto-lint hint
(codex-parity item 5): after editing a lintable code file, run lint_file.
"""
from __future__ import annotations

import unittest

from orchestrator.providers.browser.prompt_builder import format_prompt

_LINT_HINT = (
    "After editing a lintable code file, run lint_file on it to catch "
    "errors before moving on."
)


class LintHintTests(unittest.TestCase):
    def _prompt(self, **overrides):
        kwargs = dict(
            sent_system_prompt=False,
            max_inject_chars=200000,
            use_webview_bridge=True,
            preferred_site="gemini",
        )
        kwargs.update(overrides)
        prompt, _attachments, _marker, _sent = format_prompt(
            [{"role": "user", "content": "do the task"}],
            [{"name": "lint_file", "parameters": {"properties": {}}}],
            "You are Kim.",
            **kwargs,
        )
        return prompt

    def test_lint_hint_present_in_instructions_block(self):
        prompt = self._prompt()
        self.assertIn("[INSTRUCTIONS]", prompt)
        self.assertIn(_LINT_HINT, prompt)
        # The hint lives inside the INSTRUCTIONS block, not appended at tail.
        self.assertLess(
            prompt.index("[INSTRUCTIONS]"), prompt.index(_LINT_HINT)
        )

    def test_hint_is_single_line_and_brace_free(self):
        # Budget + f-string invariants: one line, no braces.
        self.assertNotIn("{", _LINT_HINT)
        self.assertNotIn("}", _LINT_HINT)
        self.assertNotIn("\n", _LINT_HINT)


if __name__ == "__main__":
    unittest.main()

import unittest

from mcp_server.tools.claw_bridge import (
    _claw_browser_system_prompt,
    _latest_bridge_response_fragment,
    _provider_response_to_bridge,
)


class BrowserProtocolTests(unittest.TestCase):
    def test_claw_prompt_has_no_static_plain_sentinel_contract(self):
        prompt = _claw_browser_system_prompt()
        self.assertNotIn('raw JSON followed by the [END_OF_RESPONSE] marker', prompt)
        self.assertNotIn('\n[END_OF_RESPONSE]\n', prompt)
        self.assertIn('omit the tool_calls key entirely', prompt)

    def test_latest_fragment_splits_dynamic_and_legacy_markers(self):
        raw = '{"text":"old"}[END_OF_RESPONSE_abcd1234] {"text":"new"}[END_OF_RESPONSE]'
        self.assertEqual(_latest_bridge_response_fragment(raw), '{"text":"new"}')

    def test_empty_tool_calls_is_tolerated_as_final_text(self):
        parsed = _provider_response_to_bridge({
            'type': 'text',
            'content': '{"text":"done","tool_calls":[]}[END_OF_RESPONSE_abc123]'
        })
        self.assertEqual(parsed.get('text'), 'done')
        self.assertEqual(parsed.get('tool_calls'), [])

    def test_browser_provider_strips_current_hash_and_old_fragments(self):
        # Import here: top-level import pulls playwright; CI / minimal venvs may skip.
        try:
            from orchestrator.providers.browser_provider import BrowserProvider
        except ModuleNotFoundError as e:
            if 'playwright' in str(e).lower():
                self.skipTest('playwright not installed')
            raise
        provider = BrowserProvider({'project_root': '.'})
        text = 'old [END_OF_RESPONSE_old1] {"tool":"x","args":{}} [END_OF_RESPONSE_now123] trailing'
        self.assertEqual(
            provider._strip_transport_markers(text, '[END_OF_RESPONSE_now123]'),
            '{"tool":"x","args":{}}'
        )


if __name__ == '__main__':
    unittest.main()

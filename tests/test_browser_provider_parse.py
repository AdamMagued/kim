import unittest


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


if __name__ == "__main__":
    unittest.main()

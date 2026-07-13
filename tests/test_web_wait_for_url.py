import unittest
from unittest.mock import patch

from mcp_server.tools import web


class FakePage:
    def __init__(self, url):
        self.url = url

    async def wait_for_url(self, predicate, timeout):
        if not predicate(self.url):
            raise TimeoutError("url did not match")


class WebWaitForUrlTests(unittest.IsolatedAsyncioTestCase):
    async def test_url_contains_succeeds(self):
        async def fake_page():
            return FakePage("https://github.com/example/kim-test-repo")

        with patch.object(web.browser, "_page", side_effect=fake_page):
            result = await web.handle_web_wait_for_url({"url_contains": "/kim-test-repo", "timeout_ms": 5})

        self.assertIn("URL contains", result)

    async def test_url_regex_succeeds(self):
        async def fake_page():
            return FakePage("https://github.com/example/kim-test-repo")

        with patch.object(web.browser, "_page", side_effect=fake_page):
            result = await web.handle_web_wait_for_url({"url_regex": r"github\.com/.+/kim-test-repo$", "timeout_ms": 5})

        self.assertIn("URL matched regex", result)

    async def test_missing_matcher_errors(self):
        result = await web.handle_web_wait_for_url({})
        self.assertTrue(result.startswith("ERROR:"))


if __name__ == "__main__":
    unittest.main()

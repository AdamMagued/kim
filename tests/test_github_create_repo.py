import json
import unittest
from unittest.mock import patch

from mcp_server.tools import github


def _el(
    element_id,
    *,
    tag="input",
    role="textbox",
    label="",
    text="",
    value="",
    type_="text",
    disabled=False,
    required=False,
    checked=False,
    form_id="repo-form",
    container_id="main",
    in_viewport=True,
):
    return {
        "id": element_id,
        "tag": tag,
        "role": role,
        "label": label,
        "text": text,
        "aria_label": "",
        "placeholder": "",
        "name": "",
        "value": value,
        "title": "",
        "nearby_text": "",
        "type": type_,
        "disabled": disabled,
        "required": required,
        "visible": True,
        "hidden": False,
        "in_viewport": in_viewport,
        "checked": checked,
        "form_id": form_id,
        "container_id": container_id,
        "bbox": [10, 20, 200, 30],
        "selector": f"#{element_id}",
    }


class FakePage:
    def __init__(self, repo_name):
        self.url = "https://github.com/new"
        self.repo_name = repo_name

    async def wait_for_url(self, predicate, timeout):
        self.url = f"https://github.com/example/{self.repo_name}"
        assert predicate(self.url)

    async def wait_for_load_state(self, state, timeout):
        return None

    async def evaluate(self, script):
        return f"Quick setup {self.repo_name}"


class GithubCreateRepoTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_fallback_uses_resolver_flow(self):
        state = {"repo_name": "", "clicked_create": False}
        page = FakePage("kim-test-repo")

        async def fake_open(args):
            return "Opened: https://github.com/new\nTitle: Create a new repository"

        async def fake_observe(args):
            elements = [
                _el("w1", label="Repository name", value=state["repo_name"], required=True),
                _el("w2", tag="input", role="radio", label="Private", type_="radio", checked=True),
                _el("w3", tag="input", role="radio", label="Public", type_="radio", checked=False),
                _el(
                    "w4",
                    tag="button",
                    role="button",
                    label="Create repository",
                    text="Create repository",
                    type_="button",
                    disabled=not bool(state["repo_name"]),
                ),
            ]
            github.web._remember_observation(
                {"url": "https://github.com/new", "title": "Create a new repository", "elements": elements}
            )
            return "WEB_OBSERVATION"

        async def fake_fill(args):
            self.assertEqual(args["element_id"], "w1")
            state["repo_name"] = args["text"]
            return f"Filled {args['element_id']} with {len(args['text'])} chars"

        async def fake_click(args):
            self.assertEqual(args["element_id"], "w4")
            state["clicked_create"] = True
            return "Clicked w4"

        async def fake_page():
            return page

        with (
            patch.object(github.shutil, "which", return_value=None),
            patch.object(github.web, "handle_web_open", side_effect=fake_open),
            patch.object(github.web, "handle_web_observe", side_effect=fake_observe),
            patch.object(github.web, "handle_web_fill", side_effect=fake_fill),
            patch.object(github.web, "handle_web_click", side_effect=fake_click),
            patch.object(github.web, "_page", side_effect=fake_page),
        ):
            raw = await github.handle_github_create_repo({"name": "kim-test-repo"})

        result = json.loads(raw)
        self.assertTrue(result["success"])
        self.assertEqual(result["code"], "SUCCESS_CREATED")
        self.assertEqual(result["method"], "browser")
        self.assertTrue(state["clicked_create"])
        self.assertEqual(result["url"], "https://github.com/example/kim-test-repo")

    async def test_private_visibility_unresolved_fails_compactly(self):
        state = {"repo_name": ""}

        async def fake_open(args):
            return "Opened: https://github.com/new\nTitle: Create a new repository"

        async def fake_observe(args):
            elements = [
                _el("w1", label="Repository name", value=state["repo_name"], required=True),
                _el("w2", tag="input", role="radio", label="Public", type_="radio", checked=True),
                _el("w3", tag="button", role="button", label="Create repository", text="Create repository", type_="button"),
            ]
            github.web._remember_observation({"url": "https://github.com/new", "title": "New", "elements": elements})
            return "WEB_OBSERVATION"

        async def fake_fill(args):
            state["repo_name"] = args["text"]
            return "Filled w1 with 13 chars"

        async def fake_page():
            return FakePage("kim-test-repo")

        with (
            patch.object(github.shutil, "which", return_value=None),
            patch.object(github.web, "handle_web_open", side_effect=fake_open),
            patch.object(github.web, "handle_web_observe", side_effect=fake_observe),
            patch.object(github.web, "handle_web_fill", side_effect=fake_fill),
            patch.object(github.web, "_page", side_effect=fake_page),
        ):
            raw = await github.handle_github_create_repo({"name": "kim-test-repo"})

        result = json.loads(raw)
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "FAIL_VISIBILITY_NOT_CONFIRMED")
        self.assertNotIn("debug", result)

    async def test_disabled_create_button_returns_typed_failure(self):
        state = {"repo_name": ""}

        async def fake_open(args):
            return "Opened: https://github.com/new\nTitle: Create a new repository"

        async def fake_observe(args):
            elements = [
                _el("w1", label="Repository name", value=state["repo_name"], required=True),
                _el("w2", tag="input", role="radio", label="Private", type_="radio", checked=True),
                _el(
                    "w3",
                    tag="button",
                    role="button",
                    label="Create repository",
                    text="Create repository",
                    type_="button",
                    disabled=True,
                ),
            ]
            github.web._remember_observation({"url": "https://github.com/new", "title": "New", "elements": elements})
            return "WEB_OBSERVATION"

        async def fake_fill(args):
            state["repo_name"] = args["text"]
            return "Filled w1 with 13 chars"

        async def fake_page():
            return FakePage("kim-test-repo")

        with (
            patch.object(github.shutil, "which", return_value=None),
            patch.object(github.web, "handle_web_open", side_effect=fake_open),
            patch.object(github.web, "handle_web_observe", side_effect=fake_observe),
            patch.object(github.web, "handle_web_fill", side_effect=fake_fill),
            patch.object(github.web, "_page", side_effect=fake_page),
        ):
            raw = await github.handle_github_create_repo({"name": "kim-test-repo", "debug": True})

        result = json.loads(raw)
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "FAIL_SUBMIT_DISABLED")
        self.assertIn("debug", result)


if __name__ == "__main__":
    unittest.main()

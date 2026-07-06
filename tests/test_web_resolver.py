import unittest

from mcp_server.tools import web


def _el(
    element_id,
    *,
    tag="input",
    role="textbox",
    label="",
    text="",
    aria_label="",
    placeholder="",
    name="",
    value="",
    title="",
    nearby_text="",
    type_="text",
    disabled=False,
    required=False,
    visible=True,
    checked=False,
    in_viewport=True,
    form_id="",
    container_id="",
    hidden=False,
):
    return {
        "id": element_id,
        "tag": tag,
        "role": role,
        "label": label,
        "text": text,
        "aria_label": aria_label,
        "placeholder": placeholder,
        "name": name,
        "value": value,
        "title": title,
        "nearby_text": nearby_text,
        "type": type_,
        "disabled": disabled,
        "required": required,
        "visible": visible,
        "hidden": hidden,
        "in_viewport": in_viewport,
        "checked": checked,
        "form_id": form_id,
        "container_id": container_id,
        "bbox": [10, 20, 200, 30] if visible else [0, 0, 0, 0],
        "selector": f"#{element_id}",
    }


class WebResolverTests(unittest.TestCase):
    def setUp(self):
        web._element_map.clear()
        web._element_data_map.clear()
        web.observation._last_observation = None
        web.observation._last_form_diagnostics = {}
        web.observation._observe_generation = 0

    def _remember(self, elements):
        result = {"url": "https://example.test", "title": "Example", "elements": elements}
        web._remember_observation(result)

    def test_exact_label_match(self):
        self._remember([
            _el("w1", label="Owner"),
            _el("w2", label="Repository name", required=True),
        ])
        resolved = web._resolve_element("repository name textbox", preferred_roles=["textbox"])
        self.assertEqual(resolved["element_id"], "w2")
        self.assertGreater(resolved["confidence"], 0.5)

    def test_placeholder_match(self):
        self._remember([
            _el("w1", label="", placeholder="my-awesome-project"),
            _el("w2", label="Search"),
        ])
        resolved = web._resolve_element("my awesome project textbox", preferred_roles=["textbox"])
        self.assertEqual(resolved["element_id"], "w1")

    def test_aria_label_match(self):
        self._remember([
            _el("w1", aria_label="Email recipient"),
            _el("w2", aria_label="Email subject"),
        ])
        resolved = web._resolve_element("email recipient field", preferred_roles=["textbox"])
        self.assertEqual(resolved["element_id"], "w1")

    def test_role_preference_breaks_text_tie(self):
        self._remember([
            _el("w1", tag="a", role="link", label="Create repository", text="Create repository", type_=""),
            _el("w2", tag="button", role="button", label="Create repository", text="Create repository", type_="button"),
        ])
        resolved = web._resolve_element("create repository button", preferred_roles=["button"])
        self.assertEqual(resolved["element_id"], "w2")

    def test_disabled_and_hidden_penalties(self):
        self._remember([
            _el("w1", label="Submit", tag="button", role="button", type_="button", disabled=True),
            _el("w2", label="Submit", tag="button", role="button", type_="button"),
            _el("w3", label="Submit", tag="button", role="button", type_="button", visible=False),
        ])
        resolved = web._resolve_element("submit button", preferred_roles=["button"])
        self.assertEqual(resolved["element_id"], "w2")

    def test_multiple_candidates_returns_debug_candidates(self):
        self._remember([
            _el("w1", label="Email subject"),
            _el("w2", label="Email recipient"),
            _el("w3", label="Email body"),
        ])
        resolved = web._resolve_element("email recipient field", preferred_roles=["textbox"])
        self.assertEqual(resolved["element_id"], "w2")
        self.assertGreaterEqual(len(resolved["candidates"]), 2)
        self.assertIn("reason", resolved["candidates"][0])

    def test_strict_scoped_submit_rejects_top_nav_create_new(self):
        self._remember([
            _el("w1", label="Repository name", form_id="repo-form", container_id="main"),
            _el(
                "w2",
                tag="button",
                role="button",
                label="Create new...",
                text="Create new...",
                type_="button",
                form_id="",
                container_id="header nav",
            ),
            _el(
                "w3",
                tag="button",
                role="button",
                label="Create repository",
                text="Create repository",
                type_="button",
                form_id="repo-form",
                container_id="main",
            ),
        ])
        resolved = web._resolve_element(
            "create repository button",
            preferred_roles=["button"],
            mode="strict",
            scope={"same_form_as": "w1"},
        )
        self.assertTrue(resolved["ok"])
        self.assertEqual(resolved["element_id"], "w3")

    def test_offscreen_visible_element_is_resolvable(self):
        self._remember([
            _el("w1", tag="button", role="button", label="Create repository", text="Create repository", in_viewport=False),
        ])
        resolved = web._resolve_element("create repository button", preferred_roles=["button"], mode="strict")
        self.assertTrue(resolved["ok"])
        self.assertEqual(resolved["element_id"], "w1")
        self.assertFalse(resolved["candidates"][0]["in_viewport"])


class WebFormDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        web._element_map.clear()
        web._element_data_map.clear()
        web.observation._last_form_diagnostics = {}
        web.observation._observe_generation = 0

    def test_required_textbox_detected_correctly(self):
        elements = [_el("w1", label="Repository name", required=True, value="")]
        _, diagnostics = web._remember_observation({"url": "https://github.com/new", "title": "New", "elements": elements})
        self.assertEqual(len(diagnostics["required_fields"]), 1)
        self.assertEqual(len(diagnostics["empty_required_fields"]), 1)
        self.assertIn("Repository name textbox is required and empty.", diagnostics["messages"])

    def test_disabled_submit_due_to_empty_required_field(self):
        elements = [
            _el("w1", label="Repository name", required=True, value=""),
            _el("w2", tag="button", role="button", type_="button", label="Create repository", text="Create repository", disabled=True),
        ]
        _, diagnostics = web._remember_observation({"url": "https://github.com/new", "title": "New", "elements": elements})
        self.assertEqual(len(diagnostics["disabled_submit_buttons"]), 1)
        self.assertIn(
            "Create repository button is disabled, likely because Repository name is empty.",
            diagnostics["messages"],
        )


if __name__ == "__main__":
    unittest.main()

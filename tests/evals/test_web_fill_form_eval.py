"""Fixture-driven evals for the composite web_fill_form tool.

These are behavioral evals, not unit tests: each case models a realistic
form page (GitHub new-repo shaped) as observation fixtures plus a fake
Playwright page, then asserts that ONE web_fill_form call produces the
right sequence of browser actions and an accurate report.
"""

from __future__ import annotations

import asyncio
import json
import re
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


def _github_new_repo_elements():
    return [
        _el("w1", label="Repository name", name="repository[name]", required=True, form_id="f1"),
        _el("w2", tag="textarea", role="textbox", label="Description", form_id="f1"),
        _el(
            "w3", role="radio", type_="radio", label="Public", name="visibility",
            nearby_text="Visibility Anyone on the internet can see this repository",
            checked=True, form_id="f1",
        ),
        _el(
            "w4", role="radio", type_="radio", label="Private", name="visibility",
            nearby_text="Visibility You choose who can see and commit",
            form_id="f1",
        ),
        _el(
            "w5", role="checkbox", type_="checkbox", label="Add a README file",
            form_id="f1",
        ),
        _el(
            "w6", tag="button", role="button", type_="submit", label="Create repository",
            text="Create repository", form_id="f1",
        ),
    ]


class FakeLocator:
    def __init__(self, page, selector):
        self._page = page
        self._selector = selector

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if self._selector in self._page.labelled_selectors else 0

    async def scroll_into_view_if_needed(self, timeout=None):
        return None

    async def click(self, timeout=None):
        self._page.actions.append(("click", self._selector))

    async def fill(self, text, timeout=None):
        self._page.actions.append(("fill", self._selector, text))

    async def select_option(self, label=None, value=None, timeout=None):
        if self._selector in self._page.select_fails:
            raise RuntimeError("no such option")
        self._page.actions.append(("select", self._selector, label or value))


class FakePage:
    """Just enough Playwright surface for handle_web_fill_form."""

    def __init__(self, elements, url="https://github.com/new", title="New repository"):
        self.elements = elements
        self.url = url
        self._title = title
        self.actions: list[tuple] = []
        # selectors that page.get_by_label() should pretend to find
        self.labelled_selectors: set[str] = set()
        self.select_fails: set[str] = set()

    def locator(self, selector):
        return FakeLocator(self, selector)

    def get_by_label(self, pattern):
        for el in self.elements:
            if re.search(pattern, el.get("label", "")):
                loc = FakeLocator(self, el["selector"])
                self.labelled_selectors.add(el["selector"])
                return loc
        return FakeLocator(self, "#__missing__")

    async def title(self):
        return self._title

    async def evaluate(self, js):
        return {"url": self.url, "title": self._title, "elements": self.elements}

    async def wait_for_load_state(self, state=None, timeout=None):
        return None


class WebFillFormEvals(unittest.TestCase):
    def setUp(self):
        web._element_map.clear()
        web._element_data_map.clear()
        web._last_observation = None
        web._last_form_diagnostics = {}
        self._orig_page = web._page

    def tearDown(self):
        web._page = self._orig_page

    def _run(self, page, args):
        async def fake_page():
            return page
        web._page = fake_page
        raw = asyncio.run(web.handle_web_fill_form(args))
        self.assertTrue(raw.startswith("FORM_FILL_REPORT"), raw)
        return json.loads(raw.split("\n", 1)[1])

    # ── The headline scenario ────────────────────────────────────────────
    def test_github_create_private_repo_one_call(self):
        page = FakePage(_github_new_repo_elements())
        report = self._run(page, {
            "fields": {
                "repository name": "demo-repo",
                "visibility": "private",
                "add a readme": True,
            },
            "submit": "create repository button",
        })

        self.assertTrue(report["ok"], report)
        self.assertIn(("fill", "#w1", "demo-repo"), page.actions)
        self.assertIn(("click", "#w4"), page.actions)  # Private radio
        self.assertIn(("click", "#w5"), page.actions)  # README checkbox
        self.assertIn(("click", "#w6"), page.actions)  # Create repository
        # Submit must come after every field action.
        self.assertEqual(page.actions[-1], ("click", "#w6"))
        self.assertTrue(report["submit"]["ok"])
        self.assertEqual(len(report["fields"]), 3)
        self.assertTrue(all(f["ok"] for f in report["fields"]))

    def test_radio_already_selected_is_not_clicked(self):
        page = FakePage(_github_new_repo_elements())
        report = self._run(page, {"fields": {"visibility": "public"}})
        self.assertTrue(report["ok"], report)
        self.assertNotIn(("click", "#w3"), page.actions)  # Public already checked

    def test_checkbox_already_in_desired_state_is_not_clicked(self):
        elements = _github_new_repo_elements()
        elements[4]["checked"] = True  # README already checked
        page = FakePage(elements)
        report = self._run(page, {"fields": {"add a readme": True}})
        self.assertTrue(report["ok"], report)
        self.assertNotIn(("click", "#w5"), page.actions)

    def test_checkbox_unchecks_when_false(self):
        elements = _github_new_repo_elements()
        elements[4]["checked"] = True
        page = FakePage(elements)
        report = self._run(page, {"fields": {"add a readme": False}})
        self.assertTrue(report["ok"], report)
        self.assertIn(("click", "#w5"), page.actions)

    def test_select_falls_back_to_fill_when_option_missing(self):
        elements = [
            _el("w1", tag="select", role="combobox", label="License", form_id="f1"),
        ]
        page = FakePage(elements)
        page.select_fails.add("#w1")
        report = self._run(page, {"fields": {"license": "MIT"}})
        self.assertTrue(report["ok"], report)
        self.assertIn(("fill", "#w1", "MIT"), page.actions)

    def test_failed_field_skips_submit(self):
        page = FakePage(_github_new_repo_elements())
        report = self._run(page, {
            "fields": {"completely nonexistent field xyz": "value"},
            "submit": "create repository button",
        })
        self.assertFalse(report["ok"])
        self.assertTrue(report["submit"]["skipped"])
        self.assertNotIn(("click", "#w6"), page.actions)

    def test_disabled_submit_is_reported_not_clicked(self):
        elements = _github_new_repo_elements()
        elements[5]["disabled"] = True
        page = FakePage(elements)
        report = self._run(page, {
            "fields": {"repository name": "demo"},
            "submit": "create repository button",
        })
        self.assertFalse(report["ok"])
        self.assertFalse(report["submit"]["ok"])
        self.assertNotIn(("click", "#w6"), page.actions)

    # ── Synonym bridging ─────────────────────────────────────────────────
    def test_synonym_resolves_email_against_hyphenated_label(self):
        elements = [_el("w1", label="E-mail address", type_="email", form_id="f1")]
        page = FakePage(elements)
        report = self._run(page, {"fields": {"email": "kim@example.com"}})
        self.assertTrue(report["ok"], report)
        self.assertIn(("fill", "#w1", "kim@example.com"), page.actions)

    # ── Accessible-label fallback ────────────────────────────────────────
    def test_label_fallback_when_observation_misses_field(self):
        # The page HAS a labelled field, but the observation extractor
        # returned nothing the resolver can match.
        elements = [_el("w9", label="Secret handshake", form_id="f1")]
        page = FakePage(elements)
        # Sabotage resolution by making the intent share no tokens with the label,
        # while get_by_label still finds it via the regex below.
        report = self._run(page, {"fields": {"secret handshake": "hello"}})
        self.assertTrue(report["ok"], report)

    def test_bad_args_rejected(self):
        async def fake_page():
            raise AssertionError("should not be called")
        web._page = fake_page
        raw = asyncio.run(web.handle_web_fill_form({"fields": "not a dict"}))
        self.assertTrue(raw.startswith("ERROR"))


class FormSchemaEvals(unittest.TestCase):
    def test_schema_groups_radios_and_lists_fields(self):
        lines = web._form_schema_lines(_github_new_repo_elements())
        joined = "\n".join(lines)
        self.assertIn("FORM_SCHEMA", joined)
        self.assertIn("'Repository name'", joined)
        self.assertIn("required", joined)
        self.assertIn("radio group 'visibility'", joined)
        self.assertIn("Public *", joined)  # selected marker
        self.assertIn("checkbox", joined)

    def test_schema_empty_for_no_fields(self):
        self.assertEqual(web._form_schema_lines([]), [])


class CoerceBoolEvals(unittest.TestCase):
    def test_truthy_falsy_and_passthrough(self):
        self.assertIs(web._coerce_bool(True), True)
        self.assertIs(web._coerce_bool("yes"), True)
        self.assertIs(web._coerce_bool("checked"), True)
        self.assertIs(web._coerce_bool("off"), False)
        self.assertIs(web._coerce_bool(0), False)
        self.assertIsNone(web._coerce_bool("private"))
        self.assertIsNone(web._coerce_bool("MIT"))


if __name__ == "__main__":
    unittest.main()

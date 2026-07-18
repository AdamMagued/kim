"""K-EFFORT: reasoning-effort control for browser LLM providers.

Covers env/config resolution (resolve_effort) and the click-selection logic
(apply_effort) against a lightweight fake page — no real browser, no
playwright import required.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from orchestrator.providers.browser.reasoning_effort import apply_effort, resolve_effort


# ---------------------------------------------------------------------------
# resolve_effort
# ---------------------------------------------------------------------------

class TestResolveEffort(unittest.TestCase):
    def test_none_when_unset(self):
        self.assertIsNone(resolve_effort({}))

    def test_env_var_wins(self, ):
        import os
        os.environ["KIM_BROWSER_EFFORT"] = "high"
        try:
            self.assertEqual(resolve_effort({"browser_provider": {"effort": "low"}}), "high")
        finally:
            del os.environ["KIM_BROWSER_EFFORT"]

    def test_config_fallback(self):
        self.assertEqual(resolve_effort({"browser_provider": {"effort": "medium"}}), "medium")

    def test_invalid_value_is_ignored(self):
        self.assertIsNone(resolve_effort({"browser_provider": {"effort": "ultra"}}))

    def test_case_insensitive(self):
        self.assertEqual(resolve_effort({"browser_provider": {"effort": "HIGH"}}), "high")


# ---------------------------------------------------------------------------
# apply_effort — fake page harness
# ---------------------------------------------------------------------------

class _FakeElement:
    def __init__(self, text: str, visible: bool = True):
        self._text = text
        self._visible = visible
        self.clicked = False

    async def is_visible(self):
        return self._visible

    async def inner_text(self):
        return self._text

    async def click(self):
        self.clicked = True


class _FakeLocator:
    def __init__(self, elements: list[_FakeElement]):
        self._elements = elements

    async def count(self):
        return len(self._elements)

    def nth(self, i):
        return self._elements[i]

    @property
    def first(self):
        return self._elements[0]


class _FakeKeyboard:
    def __init__(self):
        self.pressed: list[str] = []

    async def press(self, key):
        self.pressed.append(key)


class _FakePage:
    """Composer with a trigger button and (once "opened") a menu."""

    def __init__(self, trigger_visible: bool, menu_items: list[_FakeElement]):
        self.keyboard = _FakeKeyboard()
        self._trigger = _FakeElement("Model selector", visible=trigger_visible)
        self._menu_items = menu_items
        self.opened = False

    def locator(self, sel: str):
        if "menuitem" in sel or "option" in sel or "switch" in sel:
            return _FakeLocator(self._menu_items if self.opened else [])
        return _FakeLocator([self._trigger])

    async def wait_for_timeout(self, _ms):
        self.opened = True


class TestApplyEffort(unittest.IsolatedAsyncioTestCase):
    async def test_noop_when_effort_is_none(self):
        page = _FakePage(True, [])
        applied = await apply_effort(page, "chatgpt", None)
        self.assertFalse(applied)

    async def test_noop_for_unknown_site(self):
        page = _FakePage(True, [])
        applied = await apply_effort(page, "deepseek", "high")
        self.assertFalse(applied)

    async def test_clicks_matching_item_for_chatgpt_high(self):
        items = [
            _FakeElement("Instant"),
            _FakeElement("Thinking"),
            _FakeElement("Pro"),
        ]
        page = _FakePage(True, items)
        applied = await apply_effort(page, "chatgpt", "high")
        self.assertTrue(applied)
        self.assertTrue(page._trigger.clicked)
        self.assertTrue(items[2].clicked)  # "pro" is high's first search term
        self.assertFalse(items[1].clicked)

    async def test_clicks_matching_item_for_gemini_medium(self):
        items = [
            _FakeElement("3.5 Flash"),
            _FakeElement("3.5 Thinking"),
            _FakeElement("3.1 Pro"),
        ]
        page = _FakePage(True, items)
        applied = await apply_effort(page, "gemini", "medium")
        self.assertTrue(applied)
        self.assertTrue(items[1].clicked)

    async def test_noop_and_logs_when_trigger_not_found(self):
        page = _FakePage(False, [])  # trigger never visible
        applied = await apply_effort(page, "chatgpt", "high")
        self.assertFalse(applied)
        self.assertFalse(page._trigger.clicked)

    async def test_noop_and_closes_menu_when_no_item_matches(self):
        items = [_FakeElement("Some Unrelated Label")]
        page = _FakePage(True, items)
        applied = await apply_effort(page, "chatgpt", "high")
        self.assertFalse(applied)
        self.assertIn("Escape", page.keyboard.pressed)

    async def test_never_raises_when_locator_blows_up(self):
        page = MagicMock()
        page.locator.side_effect = RuntimeError("DOM gone")
        applied = await apply_effort(page, "chatgpt", "high")
        self.assertFalse(applied)


if __name__ == "__main__":
    unittest.main()

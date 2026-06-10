"""
Tests for mcp_server/tool_tiers.py -- capability tier filtering.

Structure:
    ParseEnabledTiersTests  -- parse_enabled_tiers() contract
    FilterToolsTests        -- filter_tools() with a mock tier_dispatch
    AliasExpansionTests     -- compound alias expansion (core/ui/browser)
    GetActiveToolNamesTests -- get_active_tool_names() env-var integration
    StaticRegistryTests     -- source scan: TIER_DISPATCH wired in tool_registry.py
    StaticServerTests       -- source scan: server.py imports and calls tier filter
    RegistryCoverageTests   -- live import (skipped if mcp unavailable):
                               every registered tool belongs to exactly one tier

All pure-stdlib tests run in Python 3.9 (no mcp dependency).
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server.tool_tiers import (
    TIER_ALIASES,
    filter_tools,
    get_active_tool_names,
    parse_enabled_tiers,
)

_REGISTRY_PY = Path(__file__).resolve().parent.parent / "mcp_server" / "tool_registry.py"
_SERVER_PY = Path(__file__).resolve().parent.parent / "mcp_server" / "server.py"
_TIERS_PY = Path(__file__).resolve().parent.parent / "mcp_server" / "tool_tiers.py"

# Minimal mock tier_dispatch used by unit tests.
_MOCK = {
    "file": {"read_file": None, "write_file": None, "list_dir": None},
    "git":  {"git_status": None, "git_commit": None},
    "web":  {"web_open": None, "web_click": None},
}


# ---------------------------------------------------------------------------
# parse_enabled_tiers
# ---------------------------------------------------------------------------

class ParseEnabledTiersTests(unittest.TestCase):

    def test_none_returns_none(self):
        self.assertIsNone(parse_enabled_tiers(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(parse_enabled_tiers(""))

    def test_whitespace_only_returns_none(self):
        self.assertIsNone(parse_enabled_tiers("   "))

    def test_single_tier(self):
        result = parse_enabled_tiers("git")
        self.assertEqual(result, frozenset({"git"}))

    def test_multiple_tiers(self):
        result = parse_enabled_tiers("git,search,file")
        self.assertEqual(result, frozenset({"git", "search", "file"}))

    def test_normalizes_to_lowercase(self):
        result = parse_enabled_tiers("GIT,SEARCH")
        self.assertEqual(result, frozenset({"git", "search"}))

    def test_strips_whitespace_around_names(self):
        result = parse_enabled_tiers("  git , search  ")
        self.assertEqual(result, frozenset({"git", "search"}))

    def test_unknown_tier_names_pass_through_without_error(self):
        # Validation is filter_tools' responsibility, not parse_enabled_tiers'.
        result = parse_enabled_tiers("git,totally_unknown")
        self.assertIn("git", result)
        self.assertIn("totally_unknown", result)

    def test_returns_frozenset(self):
        result = parse_enabled_tiers("git")
        self.assertIsInstance(result, frozenset)


# ---------------------------------------------------------------------------
# filter_tools
# ---------------------------------------------------------------------------

class FilterToolsTests(unittest.TestCase):

    def test_none_enabled_tiers_returns_none(self):
        # None means "all tools" -- callers skip filtering entirely.
        self.assertIsNone(filter_tools(_MOCK, None))

    def test_single_tier_returns_only_that_tiers_tools(self):
        result = filter_tools(_MOCK, frozenset({"git"}))
        self.assertEqual(result, frozenset({"git_status", "git_commit"}))

    def test_multiple_tiers_returns_union(self):
        result = filter_tools(_MOCK, frozenset({"file", "git"}))
        self.assertIn("read_file", result)
        self.assertIn("git_status", result)
        self.assertNotIn("web_open", result)

    def test_unknown_tier_is_ignored_and_logged(self):
        import logging
        with self.assertLogs("mcp_server.tool_tiers", level=logging.WARNING):
            result = filter_tools(_MOCK, frozenset({"nonexistent_tier"}))
        self.assertEqual(result, frozenset())

    def test_unknown_tier_does_not_expose_unintended_tools(self):
        result = filter_tools(_MOCK, frozenset({"bogus", "git"}))
        # bogus contributes nothing; git tools still present
        self.assertIn("git_status", result)
        self.assertNotIn(None, result)

    def test_empty_tier_set_returns_empty_frozenset(self):
        result = filter_tools(_MOCK, frozenset())
        self.assertEqual(result, frozenset())

    def test_returns_frozenset(self):
        result = filter_tools(_MOCK, frozenset({"git"}))
        self.assertIsInstance(result, frozenset)

    def test_all_tools_covered_when_all_tiers_specified(self):
        all_tiers = frozenset(_MOCK.keys())
        result = filter_tools(_MOCK, all_tiers)
        expected = frozenset(name for d in _MOCK.values() for name in d)
        self.assertEqual(result, expected)


# ---------------------------------------------------------------------------
# Compound alias expansion
# ---------------------------------------------------------------------------

class AliasExpansionTests(unittest.TestCase):

    # Build a tier_dispatch that mirrors the real granular tier names so alias
    # expansion can be exercised without importing mcp.
    _ALIAS_DISPATCH = {
        "file":     {"read_file": None},
        "shell":    {"run_command": None},
        "search":   {"search_in_files": None},
        "screen":   {"take_screenshot": None},
        "mouse":    {"click": None},
        "keyboard": {"type_text": None},
        "windows":  {"get_windows": None},
        "web":      {"web_open": None},
    }

    def test_core_alias_expands_to_file_shell_search(self):
        result = filter_tools(self._ALIAS_DISPATCH, frozenset({"core"}))
        self.assertIn("read_file", result)
        self.assertIn("run_command", result)
        self.assertIn("search_in_files", result)
        self.assertNotIn("take_screenshot", result)

    def test_ui_alias_expands_to_screen_mouse_keyboard_windows(self):
        result = filter_tools(self._ALIAS_DISPATCH, frozenset({"ui"}))
        self.assertIn("take_screenshot", result)
        self.assertIn("click", result)
        self.assertIn("type_text", result)
        self.assertIn("get_windows", result)
        self.assertNotIn("read_file", result)

    def test_browser_alias_expands_to_web(self):
        result = filter_tools(self._ALIAS_DISPATCH, frozenset({"browser"}))
        self.assertIn("web_open", result)
        self.assertNotIn("click", result)

    def test_alias_combined_with_granular_tier(self):
        result = filter_tools(self._ALIAS_DISPATCH, frozenset({"core", "web"}))
        self.assertIn("read_file", result)
        self.assertIn("web_open", result)

    def test_all_alias_names_in_tier_aliases(self):
        for alias in ("core", "ui", "browser"):
            self.assertIn(alias, TIER_ALIASES, f"alias {alias!r} missing from TIER_ALIASES")


# ---------------------------------------------------------------------------
# get_active_tool_names -- env-var integration
# ---------------------------------------------------------------------------

class GetActiveToolNamesTests(unittest.TestCase):

    def test_unset_env_returns_none(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KIM_ENABLED_TOOL_TIERS", None)
            result = get_active_tool_names(_MOCK)
        self.assertIsNone(result)

    def test_empty_env_returns_none(self):
        with patch.dict(os.environ, {"KIM_ENABLED_TOOL_TIERS": ""}):
            result = get_active_tool_names(_MOCK)
        self.assertIsNone(result)

    def test_set_env_returns_frozenset_of_tool_names(self):
        with patch.dict(os.environ, {"KIM_ENABLED_TOOL_TIERS": "git"}):
            result = get_active_tool_names(_MOCK)
        self.assertEqual(result, frozenset({"git_status", "git_commit"}))

    def test_multiple_tiers_in_env(self):
        with patch.dict(os.environ, {"KIM_ENABLED_TOOL_TIERS": "git,file"}):
            result = get_active_tool_names(_MOCK)
        self.assertIn("git_status", result)
        self.assertIn("read_file", result)
        self.assertNotIn("web_open", result)

    def test_env_is_isolated_between_test_cases(self):
        # Verify patch.dict correctly restores env after each test.
        self.assertNotIn("KIM_ENABLED_TOOL_TIERS", os.environ,
                         "env var leaked from a previous test")


# ---------------------------------------------------------------------------
# Static source scan: tool_registry.py
# ---------------------------------------------------------------------------

class StaticRegistryTests(unittest.TestCase):

    def setUp(self):
        self.source = _REGISTRY_PY.read_text(encoding="utf-8")

    def test_tier_dispatch_variable_defined(self):
        self.assertIn("TIER_DISPATCH", self.source)

    def test_all_granular_tier_names_present_in_tier_dispatch(self):
        expected = ("file", "shell", "screen", "web", "mouse",
                    "keyboard", "windows", "git", "code", "search", "memory")
        for tier in expected:
            self.assertIn(f'"{tier}"', self.source,
                          f"Tier {tier!r} not found in tool_registry.py")

    def test_all_dispatch_dicts_wired_into_tier_dispatch(self):
        # Each _*_DISPATCH variable must appear inside the TIER_DISPATCH block.
        # Locate the TIER_DISPATCH definition and scan it.
        idx = self.source.find("TIER_DISPATCH")
        self.assertGreater(idx, 0)
        tier_section = self.source[idx: idx + 1200]
        for var in ("_FILE_DISPATCH", "_SHELL_DISPATCH", "_SCREEN_DISPATCH",
                    "_WEB_DISPATCH", "_MOUSE_DISPATCH", "_KEYBOARD_DISPATCH",
                    "_WINDOW_DISPATCH", "_GIT_DISPATCH", "_CODE_DISPATCH",
                    "_SEARCH_DISPATCH", "_MEMORY_DISPATCH"):
            self.assertIn(var, tier_section,
                          f"{var} not found in TIER_DISPATCH definition")

    def test_all_dispatch_keys_appear_exactly_once_across_tiers(self):
        # Extract every "tool_name": handle_ key from the dispatch dicts.
        # Each must appear in exactly one tier (no duplication across tiers).
        keys = re.findall(r'"([^"]+)":\s*handle_', self.source)
        self.assertEqual(len(keys), len(set(keys)),
                         "A dispatch key appears more than once: "
                         + str([k for k in keys if keys.count(k) > 1]))

    def test_tools_and_dispatch_exports_unchanged(self):
        # TOOLS and DISPATCH must still be exported for backward compat.
        self.assertIn("TOOLS:", self.source)
        self.assertIn("DISPATCH:", self.source)


# ---------------------------------------------------------------------------
# Static source scan: server.py
# ---------------------------------------------------------------------------

class StaticServerTests(unittest.TestCase):

    def setUp(self):
        self.source = _SERVER_PY.read_text(encoding="utf-8")

    def test_server_imports_tier_dispatch(self):
        self.assertIn("TIER_DISPATCH", self.source)

    def test_server_imports_get_active_tool_names(self):
        self.assertIn("get_active_tool_names", self.source)

    def test_server_imports_from_tool_tiers(self):
        self.assertIn("from mcp_server.tool_tiers import", self.source)

    def test_server_calls_get_active_tool_names_with_tier_dispatch(self):
        self.assertIn("get_active_tool_names(TIER_DISPATCH)", self.source)


# ---------------------------------------------------------------------------
# Static source scan: tool_tiers.py
# ---------------------------------------------------------------------------

class StaticTiersModuleTests(unittest.TestCase):

    def setUp(self):
        self.source = _TIERS_PY.read_text(encoding="utf-8")

    def test_env_var_name_referenced(self):
        self.assertIn("KIM_ENABLED_TOOL_TIERS", self.source)

    def test_tier_aliases_defined(self):
        self.assertIn("TIER_ALIASES", self.source)

    def test_all_alias_names_in_source(self):
        for alias in ("core", "ui", "browser"):
            self.assertIn(f'"{alias}"', self.source)

    def test_source_is_ascii(self):
        self.source.encode("ascii")  # raises UnicodeEncodeError if non-ASCII


# ---------------------------------------------------------------------------
# Registry coverage (skipped when mcp is unavailable in test Python 3.9 env)
# ---------------------------------------------------------------------------

try:
    from mcp_server.tool_registry import DISPATCH as _DISPATCH
    from mcp_server.tool_registry import TIER_DISPATCH as _TIER_DISPATCH
    from mcp_server.tool_registry import TOOLS as _TOOLS_LIST
    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False


@unittest.skipUnless(_MCP_AVAILABLE, "mcp module not available in test env -- skipping live registry checks")
class RegistryCoverageTests(unittest.TestCase):
    """Live-import checks: every registered tool belongs to exactly one tier."""

    def _all_tiered_names(self) -> frozenset:
        names: set = set()
        for d in _TIER_DISPATCH.values():
            names.update(d.keys())
        return frozenset(names)

    def test_every_dispatch_key_covered_by_some_tier(self):
        tiered = self._all_tiered_names()
        untiered = set(_DISPATCH.keys()) - tiered
        self.assertEqual(untiered, set(),
                         f"Tools in DISPATCH but not in any tier: {sorted(untiered)}")

    def test_no_tier_names_a_nonexistent_tool(self):
        tiered = self._all_tiered_names()
        nonexistent = tiered - set(_DISPATCH.keys())
        self.assertEqual(nonexistent, set(),
                         f"Tier names a tool that is not in DISPATCH: {sorted(nonexistent)}")

    def test_tools_list_and_tier_names_in_sync(self):
        tiered = self._all_tiered_names()
        tool_names = {t.name for t in _TOOLS_LIST}
        self.assertEqual(tiered, tool_names,
                         f"Mismatch between TOOLS list and tier coverage. "
                         f"Only in TOOLS: {sorted(tool_names - tiered)}. "
                         f"Only in tiers: {sorted(tiered - tool_names)}")

    def test_no_tool_in_two_tiers(self):
        seen: dict = {}
        for tier_name, d in _TIER_DISPATCH.items():
            for tool_name in d.keys():
                if tool_name in seen:
                    self.fail(
                        f"Tool {tool_name!r} appears in both tier {seen[tool_name]!r} "
                        f"and tier {tier_name!r}"
                    )
                seen[tool_name] = tier_name


if __name__ == "__main__":
    unittest.main()

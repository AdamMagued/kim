"""
Poka-yoke invariant tests — checks that encode the "you just have to know" rules.

Split across two modules:
  - test_invariants.py (this file): tool-registry parity, CSS order, Code-tab constraint
    (no sys.modules stubs needed; the mcp package is installed)
  - test_prompt_render.py: f-string prompt rendering (needs agent stubs)
"""
from __future__ import annotations

import re
from pathlib import Path


# ---------------------------------------------------------------------------
# 1. Tool-registry parity: TOOLS list ↔ DISPATCH dict
# ---------------------------------------------------------------------------
# The real mcp package must be importable for these tests to run.  If a
# module-level stub replaced mcp.types (from another test file), the import
# will fail.  Use skipUnless — identical pattern to test_tool_tiers.py.

try:
    from mcp_server.tool_registry import TOOLS as _TOOLS, DISPATCH as _DISPATCH
    _REGISTRY_AVAILABLE = True
except ImportError:
    _REGISTRY_AVAILABLE = False

import unittest


@unittest.skipUnless(_REGISTRY_AVAILABLE, "mcp_server.tool_registry unavailable (mcp not installed or stubbed)")
class TestToolRegistryParity(unittest.TestCase):
    """Every schema entry must have a dispatch handler and vice versa."""

    def test_every_tool_has_dispatch_entry(self):
        tool_names = {t.name for t in _TOOLS}
        dispatch_names = set(_DISPATCH.keys())
        missing = tool_names - dispatch_names
        self.assertFalse(missing, f"Tools with schema but no dispatch handler: {missing}")

    def test_every_dispatch_has_tool_schema(self):
        tool_names = {t.name for t in _TOOLS}
        dispatch_names = set(_DISPATCH.keys())
        orphans = dispatch_names - tool_names
        self.assertFalse(orphans, f"Dispatch entries with no schema definition: {orphans}")

    def test_all_dispatch_handlers_are_callable(self):
        non_callable = {name for name, fn in _DISPATCH.items() if not callable(fn)}
        self.assertFalse(non_callable, f"Dispatch entries that are not callable: {non_callable}")

    def test_tool_count_matches_architecture_doc(self):
        """Regression: the Architecture doc says 31 tools. Alert if count drifts below baseline."""
        self.assertGreaterEqual(len(_TOOLS), 31, f"Tool count dropped below baseline: {len(_TOOLS)} < 31")


# ---------------------------------------------------------------------------
# 2. Code-tab constraint: never OpenAI auth, never gpt-5.5
# ---------------------------------------------------------------------------

class TestCodeTabConstraint:
    """The Code tab must never use OpenAI auth or gpt-5.5."""

    def test_code_tab_never_produces_gpt55(self):
        src = Path(__file__).parent.parent / "desktop/src-tauri/src/subprocess.rs"
        content = src.read_text()
        assert "gpt-5.5" not in content, (
            "gpt-5.5 found in subprocess.rs — Code tab constraint violated"
        )

    def test_code_tab_never_uses_openai_auth_as_default(self):
        src = Path(__file__).parent.parent / "desktop/src-tauri/src/subprocess.rs"
        content = src.read_text()
        default_match = re.search(
            r'unwrap_or_else\(\|\|\s*["\'](\w+)["\']\.to_string\(\)\)',
            content,
        )
        if default_match:
            assert default_match.group(1) != "openai", (
                "Code tab default provider changed to 'openai' — constraint violated"
            )

    def test_cron_store_excludes_openai_gpt55(self):
        src = Path(__file__).parent.parent / "orchestrator/cron_store.py"
        content = src.read_text()
        assert "gpt-5.5" in content, (
            "cron_store.py no longer mentions gpt-5.5 exclusion — docstring may have been removed"
        )


# ---------------------------------------------------------------------------
# 3. CSS import order check
# ---------------------------------------------------------------------------

class TestCSSImportOrder:
    """desktop/src/index.css must maintain its declared load-bearing import order."""

    EXPECTED_ORDER = [
        "tokens.css",
        "animations.css",
        "shell.css",
        "sidebar.css",
        "chat.css",
        "tool-cards.css",
        "theme-toggle.css",
        "settings.css",
        "onboarding.css",
        "greeting.css",
        "loaders.css",
        "settings-shader.css",
        "typing-animations.css",
        "revamp.css",
        "relay.css",
    ]

    def _read_imports(self) -> list[str]:
        css = Path(__file__).parent.parent / "desktop/src/index.css"
        lines = css.read_text().splitlines()
        return [
            line.strip()
            for line in lines
            if line.strip().startswith("@import './styles/")
        ]

    def test_import_order_is_preserved(self):
        imports = self._read_imports()
        imported_files = [
            re.search(r"'./styles/([^']+)'", line).group(1)
            for line in imports
            if re.search(r"'./styles/([^']+)'", line)
        ]
        found = [f for f in imported_files if f in self.EXPECTED_ORDER]
        expected_subset = [f for f in self.EXPECTED_ORDER if f in imported_files]
        assert found == expected_subset, (
            f"CSS import order changed!\n"
            f"Expected order: {expected_subset}\n"
            f"Actual order:   {found}\n"
            "Reordering desktop/src/index.css breaks the cascade — see CLAUDE.md"
        )

    def test_all_expected_css_files_present(self):
        imports = self._read_imports()
        import_text = "\n".join(imports)
        for f in self.EXPECTED_ORDER:
            assert f in import_text, f"Expected CSS file missing from index.css: {f}"


# ---------------------------------------------------------------------------
# 4. II-J relay feature-flag: pane hidden but code preserved
# ---------------------------------------------------------------------------

class TestRelayFeatureFlag:
    """Relay pane is feature-flagged off; code must still exist (not deleted)."""

    def test_relay_flag_is_false(self):
        src = Path(__file__).parent.parent / "desktop/src/components/kim-ui/RevampSettings.tsx"
        content = src.read_text()
        assert "RELAY_ENABLED = false" in content, (
            "RELAY_ENABLED must be false in RevampSettings.tsx (II-J)"
        )

    def test_relay_code_preserved(self):
        pane_info = Path(__file__).parent.parent / "desktop/src/components/kim-ui/settings-panes/PaneInfo.tsx"
        content = pane_info.read_text()
        assert "PaneRelay" in content, "PaneRelay code must not be deleted (II-J: flag off, not delete)"

    def test_relay_pane_id_preserved(self):
        src = Path(__file__).parent.parent / "desktop/src/components/kim-ui/RevampSettings.tsx"
        content = src.read_text()
        assert "'relay'" in content, (
            "PaneId 'relay' must still exist in RevampSettings.tsx (code preserved, just flagged off)"
        )


# ---------------------------------------------------------------------------
# 5. II-F OS notifications hook wiring invariants
# ---------------------------------------------------------------------------

class TestOsNotificationsHook:
    """useOsNotifications hook must exist and listen to the right Tauri events."""

    def test_hook_file_exists(self):
        hook = Path(__file__).parent.parent / "desktop/src/hooks/useOsNotifications.ts"
        assert hook.exists(), "useOsNotifications.ts hook file must exist (II-F)"

    def test_hook_listens_run_done(self):
        hook = Path(__file__).parent.parent / "desktop/src/hooks/useOsNotifications.ts"
        content = hook.read_text()
        assert "kim:run-done" in content, "hook must listen to kim:run-done event"

    def test_hook_listens_run_failed(self):
        hook = Path(__file__).parent.parent / "desktop/src/hooks/useOsNotifications.ts"
        content = hook.read_text()
        assert "kim:run-failed" in content, "hook must listen to kim:run-failed event"

    def test_hook_uses_notification_plugin(self):
        hook = Path(__file__).parent.parent / "desktop/src/hooks/useOsNotifications.ts"
        content = hook.read_text()
        assert "plugin-notification" in content, "hook must import from @tauri-apps/plugin-notification"

    def test_hook_wired_in_chatview(self):
        chatview = Path(__file__).parent.parent / "desktop/src/components/ChatView.tsx"
        content = chatview.read_text()
        assert "useOsNotifications" in content, "useOsNotifications must be called in ChatView.tsx"

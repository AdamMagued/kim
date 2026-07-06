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
        """Regression: the documented tool count in ARCHITECTURE.md must equal len(TOOLS).

        Parses the canonical number from the doc (e.g. '**50 OS-control tools**') so
        that any drift — doc updated without touching the registry, or vice versa —
        causes an immediate failure.  Update the doc when adding/removing tools.
        """
        arch_doc = Path(__file__).parent.parent / "ARCHITECTURE.md"
        text = arch_doc.read_text()
        match = re.search(r"\*\*(\d+) OS-control tools\*\*", text)
        self.assertIsNotNone(
            match,
            "Could not find '**N OS-control tools**' in ARCHITECTURE.md — "
            "update the doc to use that exact phrase",
        )
        doc_count = int(match.group(1))
        self.assertEqual(
            doc_count,
            len(_TOOLS),
            f"ARCHITECTURE.md documents {doc_count} tools but tool_registry has "
            f"{len(_TOOLS)}. Update the doc or the registry so they agree.",
        )


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
        # Guard against silent vacuity: if the default-provider expression is
        # refactored to another form this test must fail loudly, not pass.
        assert default_match is not None, (
            "default-provider unwrap_or_else expression not found in subprocess.rs — "
            "update this test's regex to match the new form"
        )
        assert default_match.group(1) != "openai", (
            "Code tab default provider changed to 'openai' — constraint violated"
        )

    def test_scheduled_runner_excludes_openai_gpt55(self):
        """The REAL enforcement lives in scheduled_runner.is_allowed_provider —
        test it, not the cron_store docstring that merely mentions it."""
        from orchestrator.scheduled_runner import is_allowed_provider

        # Allowed for scheduled execution
        assert is_allowed_provider("ollama")
        assert is_allowed_provider("ollama-cloud")
        assert is_allowed_provider("browser")
        assert is_allowed_provider("browser:chatgpt")
        assert is_allowed_provider(None)  # empty -> defaults to ollama

        # The standing constraint: never openai / gpt-5.5
        assert not is_allowed_provider("openai")
        assert not is_allowed_provider("gpt-5.5")
        assert not is_allowed_provider("claude")


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
        "chat-base.css",
        "chat-welcome.css",
        "chat-activity.css",
        "chat-composer.css",
        "chat-providers.css",
        "chat-session.css",
        "chat-messages.css",
        "tool-cards.css",
        "theme-toggle.css",
        "settings.css",
        "onboarding.css",
        "greeting.css",
        "loaders.css",
        "settings-shader.css",
        "typing-animations.css",
        "revamp.css",
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
# 4. Relay decommissioned (Phase 0 A5/S6 — supersedes the old II-J flag-off)
# ---------------------------------------------------------------------------

class TestRelayDecommissioned:
    """The phone-relay subsystem was removed in Phase 0 (roadmap A5/S6).

    It was never enabled (RELAY_ENABLED=false since inception) and shipped a
    deployable server (relay_server/ + Dockerfile + railway.toml) that was
    pure attack surface. If relay comes back, it must come back deliberately
    through a new design — not by accident. Git history preserves the code.
    """

    def test_relay_server_is_gone(self):
        root = Path(__file__).parent.parent
        assert not (root / "relay_server").exists(), (
            "relay_server/ must stay deleted (decommissioned in Phase 0); "
            "resurrect deliberately or not at all"
        )
        assert not (root / "railway.toml").exists(), "relay deploy config must stay deleted"

    def test_relay_ui_is_gone(self):
        src = Path(__file__).parent.parent / "desktop/src/components/kim-ui/RevampSettings.tsx"
        content = src.read_text()
        assert "'relay'" not in content and "PaneRelay" not in content, (
            "relay settings pane must stay removed (decommissioned in Phase 0)"
        )


class TestVoiceFeatureFlag:
    """Voice stays dormant until its agent runtime is deliberately restored."""

    def test_voice_flag_defaults_to_false(self):
        src = Path(__file__).parent.parent / "mcp_server/config.py"
        content = src.read_text()
        assert '_cfg.get("voice_enabled", False)' in content, (
            "VOICE_ENABLED must default to False while the agent voice runtime is removed"
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

    def test_hook_notifies_on_failure(self):
        # B8: the hook no longer subscribes to a separate kim:run-failed event —
        # it notifies from kim:run-done as the single source of truth and surfaces
        # failures via the success=false branch (avoids the old double-notify).
        hook = Path(__file__).parent.parent / "desktop/src/hooks/useOsNotifications.ts"
        content = hook.read_text()
        assert "kim:run-done" in content, "hook must listen to kim:run-done"
        assert "success" in content, "hook must branch on the success flag to notify failures"

    def test_hook_uses_notification_plugin(self):
        hook = Path(__file__).parent.parent / "desktop/src/hooks/useOsNotifications.ts"
        content = hook.read_text()
        assert "plugin-notification" in content, "hook must import from @tauri-apps/plugin-notification"

    def test_hook_wired_in_chatview(self):
        chatview = Path(__file__).parent.parent / "desktop/src/components/ChatView.tsx"
        content = chatview.read_text()
        assert "useOsNotifications" in content, "useOsNotifications must be called in ChatView.tsx"

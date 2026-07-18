"""F-B-14: BrowserProvider's session dir (chrome_data / CDP registry) must
anchor to the stable Kim install, never to the launching cwd or an ambient
PROJECT_ROOT env var meant for a different purpose ("target project" — see
task_spec.rs's own "GUI: target project; bridge: kim root" comment).

Regression this guards: config.yaml's shipped `project_root: "."`, resolved
against Path.cwd() (the old behavior), silently created a brand-new,
never-logged-in Chrome profile every time Kim was launched from a different
directory or against a different target project — the user had to sign in
to claude.ai/chatgpt/gemini again and again with no indication why.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from orchestrator.providers.browser.session_paths import (
    resolve_session_root,
    stable_install_root,
)


class TestStableInstallRoot:
    def test_falls_back_to_this_package_location_when_no_kim_root_marker(self, tmp_path):
        with patch("orchestrator.providers.browser.session_paths.Path.home", return_value=tmp_path):
            root = stable_install_root()
        # orchestrator/providers/browser/session_paths.py -> kim-pro/
        expected = Path(__file__).resolve().parent.parent
        assert root == expected

    def test_honors_kim_root_marker_when_present_and_real(self, tmp_path):
        install_dir = tmp_path / "my-kim-install"
        install_dir.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        (home / ".kim_root").write_text(str(install_dir), encoding="utf-8")
        with patch("orchestrator.providers.browser.session_paths.Path.home", return_value=home):
            root = stable_install_root()
        assert root == install_dir.resolve()

    def test_ignores_kim_root_marker_pointing_at_nonexistent_dir(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        (home / ".kim_root").write_text(str(tmp_path / "does-not-exist"), encoding="utf-8")
        with patch("orchestrator.providers.browser.session_paths.Path.home", return_value=home):
            root = stable_install_root()
        assert root == Path(__file__).resolve().parent.parent


class TestResolveSessionRoot:
    def test_shipped_dot_default_anchors_to_install_not_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # simulate "launched from a random directory"
        with patch(
            "orchestrator.providers.browser.session_paths.stable_install_root",
            return_value=Path("/stable/kim-install"),
        ):
            root = resolve_session_root({"project_root": "."})
        assert root == Path("/stable/kim-install")
        assert root != tmp_path.resolve()

    def test_missing_project_root_key_anchors_to_install(self):
        with patch(
            "orchestrator.providers.browser.session_paths.stable_install_root",
            return_value=Path("/stable/kim-install"),
        ):
            assert resolve_session_root({}) == Path("/stable/kim-install")

    def test_explicit_absolute_override_is_still_honored(self, tmp_path):
        # Existing test-isolation / power-user mechanism (test_browser_cdp_reap.py
        # relies on exactly this) must keep working unchanged.
        root = resolve_session_root({"project_root": str(tmp_path)})
        assert root == tmp_path.resolve()

    def test_ambient_project_root_env_var_is_not_consulted(self, tmp_path, monkeypatch):
        # The PROJECT_ROOT env var means "target project" elsewhere in Kim
        # (GUI task / MCP sandboxing) — it must NOT steer the browser's login
        # profile, or switching projects would silently reset the login.
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / "some-other-target-project"))
        with patch(
            "orchestrator.providers.browser.session_paths.stable_install_root",
            return_value=Path("/stable/kim-install"),
        ):
            assert resolve_session_root({"project_root": "."}) == Path("/stable/kim-install")

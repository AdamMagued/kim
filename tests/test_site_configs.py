"""
Regression tests for orchestrator/providers/browser/site_configs.py.

CDP_URL and MOD_KEY are computed at module-import time, so each test that
needs a different environment patches the relevant seam and uses
importlib.reload() to re-evaluate the module-level expressions.
"""
from __future__ import annotations

import importlib
import sys
import platform


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _reload_site_configs():
    """Force site_configs to re-execute its module-level statements and return it."""
    import orchestrator.providers.browser.site_configs as sc
    importlib.reload(sc)
    return sc


# ---------------------------------------------------------------------------
# 1. cdp_url_honors_env_port
# ---------------------------------------------------------------------------

def test_cdp_url_honors_env_port(monkeypatch):
    """With KIM_REAL_BROWSER_CDP_PORT set, CDP_URL must use that port."""
    monkeypatch.setenv("KIM_REAL_BROWSER_CDP_PORT", "19222")
    sc = _reload_site_configs()
    assert sc.CDP_URL == "http://localhost:19222", (
        f"Expected CDP_URL='http://localhost:19222', got {sc.CDP_URL!r}"
    )


# ---------------------------------------------------------------------------
# 2. cdp_url_default_9222
# ---------------------------------------------------------------------------

def test_cdp_url_default_9222(monkeypatch):
    """Without the env override, CDP_URL must default to http://localhost:9222."""
    monkeypatch.delenv("KIM_REAL_BROWSER_CDP_PORT", raising=False)
    sc = _reload_site_configs()
    assert sc.CDP_URL == "http://localhost:9222", (
        f"Expected CDP_URL='http://localhost:9222', got {sc.CDP_URL!r}"
    )


# ---------------------------------------------------------------------------
# 3. mod_key_platform
# ---------------------------------------------------------------------------

def test_mod_key_platform_darwin(monkeypatch):
    """MOD_KEY must be 'Meta' when platform.system() returns 'Darwin'."""
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    sc = _reload_site_configs()
    assert sc.MOD_KEY == "Meta", (
        f"Expected MOD_KEY='Meta' on Darwin, got {sc.MOD_KEY!r}"
    )


def test_mod_key_platform_linux(monkeypatch):
    """MOD_KEY must be 'Control' when platform.system() returns a non-Darwin value."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    sc = _reload_site_configs()
    assert sc.MOD_KEY == "Control", (
        f"Expected MOD_KEY='Control' on Linux, got {sc.MOD_KEY!r}"
    )


def test_mod_key_platform_windows(monkeypatch):
    """MOD_KEY must be 'Control' when platform.system() returns 'Windows'."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    sc = _reload_site_configs()
    assert sc.MOD_KEY == "Control", (
        f"Expected MOD_KEY='Control' on Windows, got {sc.MOD_KEY!r}"
    )

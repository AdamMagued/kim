"""Regression tests for MCP registry/dispatch hardening (findings 2.2–2.5)."""
from __future__ import annotations

from mcp_server import tool_registry as reg


# ── 2.4 registry integrity ──

def test_tools_and_dispatch_are_consistent():
    tool_names = {t.name for t in reg.TOOLS}
    assert tool_names == set(reg.DISPATCH), (
        "every advertised Tool must have a handler and vice-versa"
    )


def test_no_duplicate_tool_names_in_tools_list():
    names = [t.name for t in reg.TOOLS]
    assert len(names) == len(set(names)), "duplicate Tool name in TOOLS"


# ── 2.3 open_url belongs to the web tier, not windows ──

def test_open_url_is_in_web_tier_not_windows():
    assert "open_url" in reg.TIER_DISPATCH["web"]
    assert "open_url" not in reg.TIER_DISPATCH["windows"]
    assert "open_url" in reg.DISPATCH  # still globally dispatchable


# ── 2.5 third-party MCP servers get a minimal env, not the full secret-laden one ──

def test_extra_server_env_withholds_secrets():
    from orchestrator.mcp_client import _extra_server_env
    base = {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "ANTHROPIC_API_KEY": "sk-secret",
        "KIM_GOOGLE_ACCESS_TOKEN": "tok",
        "KIM_APPROVAL_SOCK": "/tmp/x.sock",
    }
    env = _extra_server_env(base, None)
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/u"
    assert "ANTHROPIC_API_KEY" not in env
    assert "KIM_GOOGLE_ACCESS_TOKEN" not in env
    assert "KIM_APPROVAL_SOCK" not in env


def test_extra_server_env_applies_declared_block():
    from orchestrator.mcp_client import _extra_server_env
    env = _extra_server_env({"PATH": "/usr/bin"}, {"MY_TOKEN": "abc", "NUM": 5})
    assert env["MY_TOKEN"] == "abc"
    assert env["NUM"] == "5"  # coerced to str


def test_extra_server_env_ignores_non_dict_declared():
    from orchestrator.mcp_client import _extra_server_env
    env = _extra_server_env({"PATH": "/usr/bin"}, "not-a-dict")
    assert env == {"PATH": "/usr/bin"}

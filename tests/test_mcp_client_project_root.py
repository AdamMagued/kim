"""F-INH-5: the MCP client must pin PROJECT_ROOT for the spawned Kim server so
client (cwd) and server (sandbox root) never disagree.

Before the fix the server env only carried PROJECT_ROOT when it happened to be
inherited from the parent process; otherwise the client used ``Path.cwd()`` as
the server cwd while the server independently resolved its config-file
directory — two different roots.
"""

from __future__ import annotations

import os
from pathlib import Path

from orchestrator.mcp_client import _resolve_project_root, _server_env


def test_resolve_project_root_env_takes_precedence(monkeypatch, tmp_path):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    assert _resolve_project_root({"project_root": "/some/other"}) == str(tmp_path.resolve())


def test_resolve_project_root_config_when_env_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    assert _resolve_project_root({"project_root": str(tmp_path)}) == str(tmp_path.resolve())


def test_resolve_project_root_defaults_to_cwd(monkeypatch):
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    assert _resolve_project_root({}) == str(Path.cwd().resolve())


def test_server_env_pins_project_root_even_when_unset(monkeypatch):
    """The regression: with PROJECT_ROOT NOT in the parent env, the server env
    must still carry the client's resolved root (so the server does not fall
    back to its config-file directory)."""
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    root = "/tmp/kim-resolved-root"
    env = _server_env(root, broker_env={})
    assert env["PROJECT_ROOT"] == root


def test_server_env_pin_overrides_inherited_value(monkeypatch):
    """A stale/mismatched inherited PROJECT_ROOT must be overridden by the
    client's authoritative resolution, not silently win."""
    monkeypatch.setenv("PROJECT_ROOT", "/inherited/stale")
    resolved = "/tmp/kim-authoritative"
    env = _server_env(resolved, broker_env={})
    assert env["PROJECT_ROOT"] == resolved


def test_server_env_matches_resolved_root(monkeypatch, tmp_path):
    """End-to-end invariant: the PROJECT_ROOT the client hands the server is
    exactly the root the client resolved (they cannot diverge)."""
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    root = _resolve_project_root({})
    env = _server_env(root, broker_env={})
    assert env["PROJECT_ROOT"] == root == str(tmp_path.resolve())


def test_server_env_preserves_broker_and_parent_keys(monkeypatch):
    monkeypatch.setenv("SOME_PARENT_VAR", "keepme")
    env = _server_env("/root", broker_env={"KIM_APPROVAL_SOCKET": "sock"})
    assert env["SOME_PARENT_VAR"] == "keepme"
    assert env["KIM_APPROVAL_SOCKET"] == "sock"
    assert env["PROJECT_ROOT"] == "/root"

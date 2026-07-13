"""Regression tests for config-loading hardening (cobweb findings 1.1–1.5).

These cover the pure, importable pieces of mcp_server/config.py so the checks
run without the module's import-time coupling to a real config.yaml.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mcp_server import config as cfg


# ── 1.1 scalar allowed_paths must not become a filesystem-wide sandbox escape ──

def test_scalar_allowed_paths_does_not_grant_root():
    project_root = Path("/tmp/kim-projroot")
    project_dir = Path("/tmp/kim-projdir")
    # A bare string was previously iterated char-by-char, so "~/x" produced the
    # roots "/" and "$HOME". The coercion must treat it as a single entry.
    resolved = cfg._resolve_allowed_paths("~", project_dir, project_root)
    assert Path("/") not in resolved, "scalar allowed_paths leaked filesystem root"
    assert project_root in resolved
    # "~" expands to the home directory (one entry), never the individual chars.
    assert Path.home() in resolved


def test_scalar_slash_char_not_present():
    resolved = cfg._resolve_allowed_paths("/etc/passwd", Path("/pd"), Path("/pr"))
    # Treated as ONE path, not the characters '/', 'e', 't', 'c', ... — so the
    # only entries are the single (symlink-resolved) path + project root, and
    # crucially never the filesystem root "/".
    assert Path("/") not in resolved
    non_root = [p for p in resolved if p != Path("/pr")]
    assert len(non_root) == 1
    assert non_root[0].name == "passwd"


def test_list_allowed_paths_preserved(tmp_path):
    project_dir = tmp_path / "config-dir"
    project_root = tmp_path / "project-root"
    allowed_a = tmp_path / "allowed-a"
    allowed_b = tmp_path / "allowed-b"
    resolved = cfg._resolve_allowed_paths(
        [str(allowed_a), str(allowed_b)], project_dir, project_root
    )
    assert allowed_a.resolve() in resolved and allowed_b.resolve() in resolved
    assert project_root in resolved  # project_root always appended


def test_non_list_shape_falls_back_to_project_root(tmp_path):
    project_dir = tmp_path / "config-dir"
    project_root = tmp_path / "project-root"
    resolved = cfg._resolve_allowed_paths(5, project_dir, project_root)
    assert resolved == [project_root]


def test_non_string_entries_skipped(tmp_path):
    project_dir = tmp_path / "config-dir"
    project_root = tmp_path / "project-root"
    allowed_a = tmp_path / "allowed-a"
    allowed_b = tmp_path / "allowed-b"
    resolved = cfg._resolve_allowed_paths(
        [str(allowed_a), 5, None, str(allowed_b)], project_dir, project_root
    )
    assert allowed_a.resolve() in resolved and allowed_b.resolve() in resolved
    assert project_root in resolved


# ── 1.4 strict bool coercion (quoted "false" must not become True) ──

@pytest.mark.parametrize("val,expected", [
    (True, True), (False, False),
    ("true", True), ("false", False),
    ("yes", True), ("no", False), ("on", True), ("off", False),
    ("1", True), ("0", False), ("", False),
])
def test_as_bool_recognised_values(val, expected):
    # default is deliberately the OPPOSITE of expected so a fallthrough fails.
    assert cfg._as_bool(val, not expected) == expected


@pytest.mark.parametrize("val", [None, "garbage", "maybe", object()])
def test_as_bool_unrecognised_falls_back_to_default(val):
    sentinel = object()
    assert cfg._as_bool(val, sentinel) is sentinel


def test_as_bool_quoted_false_is_false():
    # The core 1.4 case: bool("false") is True, so a quoted false must be caught.
    assert cfg._as_bool("false", True) is False


# ── 1.2 strict int coercion (null section / bad values must not crash) ──

@pytest.mark.parametrize("val,expected", [
    (30, 30), ("45", 45), (12.9, 12), (None, 99),
    ("30s", 99), ("", 99), (True, 99), (False, 99), ([], 99),
])
def test_as_int(val, expected):
    assert cfg._as_int(val, 99) == expected


# ── 1.2 null / missing config sections are safe ──

def test_section_null_returns_empty_dict():
    assert cfg._section({"shell": None}, "shell") == {}
    assert cfg._section({}, "shell") == {}
    assert cfg._section({"shell": "oops"}, "shell") == {}
    assert cfg._section({"shell": {"timeout": 5}}, "shell") == {"timeout": 5}


# ── 1.2 / 1.3 malformed YAML tolerance ──

def test_load_yaml_tolerates_bad_yaml(tmp_path, monkeypatch):
    bad = tmp_path / "config.yaml"
    bad.write_text("{{{{ not valid yaml !@#$", encoding="utf-8")
    monkeypatch.setattr(cfg, "_CONFIG_PATH", bad)
    assert cfg._load_yaml() == {}


def test_load_yaml_non_mapping_returns_empty(tmp_path, monkeypatch):
    doc = tmp_path / "config.yaml"
    doc.write_text("- just\n- a\n- list\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "_CONFIG_PATH", doc)
    assert cfg._load_yaml() == {}


def test_load_yaml_empty_file_returns_empty(tmp_path, monkeypatch):
    doc = tmp_path / "config.yaml"
    doc.write_text("", encoding="utf-8")
    monkeypatch.setattr(cfg, "_CONFIG_PATH", doc)
    assert cfg._load_yaml() == {}


# ── 1.5 get_config returns a copy ──

def test_get_config_returns_copy():
    a = cfg.get_config()
    a["__mutation_probe__"] = 1
    assert "__mutation_probe__" not in cfg.get_config()


# ── 1.3 orchestrator loader degrades gracefully like the Rust loader ──

def test_orchestrator_load_config_tolerates_bad_yaml(tmp_path):
    from orchestrator.agent_config import load_config
    bad = tmp_path / "config.yaml"
    bad.write_text("{{{ broken", encoding="utf-8")
    assert load_config(str(bad)) == {}


def test_orchestrator_load_config_non_mapping(tmp_path):
    from orchestrator.agent_config import load_config
    doc = tmp_path / "config.yaml"
    doc.write_text("42\n", encoding="utf-8")
    assert load_config(str(doc)) == {}

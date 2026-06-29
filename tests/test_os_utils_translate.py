"""
Regression tests for mcp_server/os_utils.py — command translation layer.

These are guard tests that assert the CURRENT (HEAD) behavior of
translate_command() and _translate_powershell() so that accidental
regressions are caught immediately.

All tests run as-is on macOS/Linux.  Where the behavior depends on Unix
vs. Windows, IS_WINDOWS is monkeypatched to False (so tests pass even if
someone runs the suite on Windows in the future).
"""
from __future__ import annotations

import importlib
import sys

import pytest

import mcp_server.os_utils as os_utils
from mcp_server.os_utils import (
    _translate_powershell,
    translate_command,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _force_unix(monkeypatch):
    """Ensure the module-level OS flags reflect a Unix host for this test."""
    monkeypatch.setattr(os_utils, "IS_WINDOWS", False)
    monkeypatch.setattr(os_utils, "IS_MACOS", True)
    monkeypatch.setattr(os_utils, "IS_LINUX", False)


# ---------------------------------------------------------------------------
# Behavior 1 — untranslatable PowerShell → bash -c ... exit 1
# ---------------------------------------------------------------------------

class TestPowershellUntranslatableReturnsFailureCmd:
    """translate_command() must emit a real failure (exit 1) for PowerShell
    invocations it cannot translate, NOT a silent echo that exits 0."""

    def test_no_command_flag_gives_bash_exit1(self, monkeypatch):
        """powershell without -Command flag → bash -c '...; exit 1'."""
        _force_unix(monkeypatch)
        result = translate_command("powershell -NoProfile -File script.ps1")
        assert result.startswith("bash -c "), (
            f"Expected bash -c wrapper, got: {result!r}"
        )
        assert "exit 1" in result, (
            f"Expected 'exit 1' in failure command, got: {result!r}"
        )

    def test_multistatement_command_gives_bash_exit1(self, monkeypatch):
        """powershell -Command with semicolon (multi-statement) → bash -c '...; exit 1'."""
        _force_unix(monkeypatch)
        result = translate_command("powershell -Command 'Get-Process; Stop-Service'")
        assert result.startswith("bash -c "), (
            f"Expected bash -c wrapper, got: {result!r}"
        )
        assert "exit 1" in result, (
            f"Expected 'exit 1' in failure command, got: {result!r}"
        )

    def test_failure_cmd_does_not_exit_0(self, monkeypatch):
        """The fallback must not contain 'exit 0' (which would hide the error)."""
        _force_unix(monkeypatch)
        result = translate_command("powershell -EncodedCommand base64==")
        assert "exit 0" not in result, (
            f"Fallback must not silently exit 0, got: {result!r}"
        )

    def test_powershell_exe_variant_also_fails(self, monkeypatch):
        """powershell.exe (no -Command) also falls back to bash exit 1."""
        _force_unix(monkeypatch)
        result = translate_command("powershell.exe -NonInteractive")
        assert result.startswith("bash -c "), (
            f"Expected bash -c wrapper, got: {result!r}"
        )
        assert "exit 1" in result


# ---------------------------------------------------------------------------
# Behavior 2 — _translate_powershell returns None for multi-statement / no -Command
# ---------------------------------------------------------------------------

class TestTranslatePowershellNoneOnMultistatement:
    """_translate_powershell must return None (not a string) for inputs it
    cannot safely translate."""

    def test_no_command_flag_returns_none(self, monkeypatch):
        _force_unix(monkeypatch)
        assert _translate_powershell("powershell -File script.ps1") is None

    def test_semicolon_in_inner_returns_none(self, monkeypatch):
        _force_unix(monkeypatch)
        assert _translate_powershell("powershell -Command 'a; b'") is None

    def test_pipe_in_inner_returns_none(self, monkeypatch):
        _force_unix(monkeypatch)
        assert _translate_powershell("powershell -Command 'Get-Process | Out-File'") is None

    def test_double_ampersand_in_inner_returns_none(self, monkeypatch):
        _force_unix(monkeypatch)
        assert _translate_powershell("powershell -Command 'a && b'") is None

    def test_newline_in_inner_returns_none(self, monkeypatch):
        _force_unix(monkeypatch)
        cmd = "powershell -Command 'Get-Process\nStop-Service'"
        assert _translate_powershell(cmd) is None

    def test_absent_command_flag_no_args_returns_none(self, monkeypatch):
        _force_unix(monkeypatch)
        assert _translate_powershell("powershell") is None


# ---------------------------------------------------------------------------
# Behavior 3 — mkdir is NOT in the builtin map; Unix absolute paths unchanged
# ---------------------------------------------------------------------------

class TestMkdirNotTranslated:
    """mkdir was deliberately removed from _BUILTIN_MAP_UNIX to prevent Unix
    absolute paths from being rewritten to 'mkdir -p'."""

    def test_mkdir_absolute_path_unchanged(self, monkeypatch):
        _force_unix(monkeypatch)
        cmd = "mkdir /tmp/newdir"
        assert translate_command(cmd) == cmd, (
            f"mkdir absolute path must NOT be rewritten; got: {translate_command(cmd)!r}"
        )

    def test_mkdir_relative_path_unchanged(self, monkeypatch):
        _force_unix(monkeypatch)
        cmd = "mkdir some_relative_dir"
        assert translate_command(cmd) == cmd

    def test_mkdir_not_in_builtin_map(self):
        """Confirm the map itself has no 'mkdir' key (the source of truth)."""
        assert "mkdir" not in os_utils._BUILTIN_MAP_UNIX, (
            "mkdir must not appear in _BUILTIN_MAP_UNIX"
        )


# ---------------------------------------------------------------------------
# Behavior 4 — _WINDOWS_SHAPED_ONLY guard (type, where)
# ---------------------------------------------------------------------------

class TestWindowsShapedOnlyGuard:
    """Commands in _WINDOWS_SHAPED_ONLY (type, where) must only be translated
    when the invocation has at least one /flag-style argument.  Plain Unix
    invocations must pass through unchanged."""

    def test_type_without_flag_not_translated(self, monkeypatch):
        """'type ls' has no /flag → must NOT be translated to 'cat ls'."""
        _force_unix(monkeypatch)
        result = translate_command("type ls")
        assert result == "type ls", (
            f"'type ls' (no /flag) must be left unchanged; got: {result!r}"
        )

    def test_type_with_windows_flag_translated_to_cat(self, monkeypatch):
        """'type /P x' has /P flag → must be translated to 'cat /P x'."""
        _force_unix(monkeypatch)
        result = translate_command("type /P x")
        assert result.startswith("cat"), (
            f"'type /P x' must be translated to cat ...; got: {result!r}"
        )

    def test_where_without_flag_not_translated(self, monkeypatch):
        """'where foo' has no /flag → must NOT be translated to 'which foo'."""
        _force_unix(monkeypatch)
        result = translate_command("where foo")
        assert result == "where foo", (
            f"'where foo' (no /flag) must be left unchanged; got: {result!r}"
        )

    def test_where_with_windows_flag_translated_to_which(self, monkeypatch):
        """'where /R . foo' has /R flag → translated to 'which /R . foo'."""
        _force_unix(monkeypatch)
        result = translate_command("where /R . foo")
        assert result.startswith("which"), (
            f"'where /R . foo' must be translated to which ...; got: {result!r}"
        )

    def test_cls_always_maps_to_clear(self, monkeypatch):
        """cls is NOT in _WINDOWS_SHAPED_ONLY so it always translates to clear."""
        _force_unix(monkeypatch)
        result = translate_command("cls")
        assert result == "clear", (
            f"cls must always map to clear on Unix; got: {result!r}"
        )

    def test_type_in_windows_shaped_only_set(self):
        """Confirm 'type' and 'where' are in _WINDOWS_SHAPED_ONLY (source of truth)."""
        assert "type" in os_utils._WINDOWS_SHAPED_ONLY
        assert "where" in os_utils._WINDOWS_SHAPED_ONLY

    def test_cls_not_in_windows_shaped_only_set(self):
        """cls must NOT be gated — it has no ambiguous Unix meaning."""
        assert "cls" not in os_utils._WINDOWS_SHAPED_ONLY

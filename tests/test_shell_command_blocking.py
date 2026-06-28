"""
Contract tests for _check_blocked() in mcp_server/tools/shell.py.

Focuses on the newline/CR bypass: POSIX shells treat \\n identically to ';'
as a command separator, so 'ls\\nrm -rf /tmp' executes two commands even when
allow_chaining=False.  The fix adds \\n and \\r to _CHAIN_METACHAR_RE.
"""

from __future__ import annotations

import importlib
import os
from unittest.mock import patch

import pytest

from mcp_server.config import PROJECT_ROOT
from mcp_server.tools.shell import _SANDBOX_PATH, _check_blocked, _sandbox_env, handle_run_command


# ── Newline / CR injection bypass ─────────────────────────────────────────────


def test_newline_is_blocked_when_chaining_disabled():
    """\\n is a shell command separator — must be caught like ';'."""
    result = _check_blocked("echo hello\necho pwned")
    assert result is not None and "BLOCKED" in result


def test_carriage_return_is_blocked_when_chaining_disabled():
    """\\r can also act as a line separator in some shell configurations."""
    result = _check_blocked("echo hello\recho pwned")
    assert result is not None and "BLOCKED" in result


def test_newline_after_safe_command_still_blocked():
    """Even if the first command looks safe, a trailing newline must be caught."""
    result = _check_blocked("ls /tmp\npython3 -c 'import os'")
    assert result is not None and "BLOCKED" in result


def test_crlf_sequence_is_blocked():
    """Windows-style line ending \\r\\n must be caught."""
    result = _check_blocked("ls /tmp\r\necho pwned")
    assert result is not None and "BLOCKED" in result


# ── Existing metachar blocking (regression) ────────────────────────────────────


@pytest.mark.parametrize("cmd", [
    "ls; rm -rf /tmp",
    "ls | cat /etc/passwd",
    "ls && echo pwned",
    "ls || echo pwned",
    "ls `echo pwned`",
    "ls $(echo pwned)",
])
def test_classic_chain_metacharacters_still_blocked(cmd):
    result = _check_blocked(cmd)
    assert result is not None and "BLOCKED" in result, (
        f"Expected {cmd!r} to be blocked but it was not"
    )


# ── allow_chaining=True permits multi-command strings ─────────────────────────


def test_newline_allowed_when_chaining_enabled():
    """With allow_chaining=True, newlines must pass the metachar check."""
    result = _check_blocked("echo hello\necho world", allow_chaining=True)
    # May still be blocked by deny-set, but not by the metachar rule
    assert result is None or "chaining" not in (result or "").lower()


def test_semicolon_allowed_when_chaining_enabled():
    result = _check_blocked("echo hello; echo world", allow_chaining=True)
    assert result is None or "chaining" not in (result or "").lower()


# ── Blocked deny-set commands ──────────────────────────────────────────────────


@pytest.mark.parametrize("cmd", [
    "rm -rf /",
    "sudo rm important.txt",
    "dd if=/dev/zero of=/dev/sda",
])
def test_deny_set_commands_blocked(cmd):
    result = _check_blocked(cmd)
    assert result is not None and "BLOCKED" in result


# ── Safe commands pass ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("cmd", [
    "ls /tmp",
    "python3 script.py",
    "git status",
    "echo hello world",
    "cat README.md",
])
def test_safe_commands_not_blocked(cmd):
    result = _check_blocked(cmd)
    assert result is None, f"Safe command {cmd!r} was incorrectly blocked: {result}"


# -- Sandboxed execution ------------------------------------------------------


@pytest.mark.asyncio
async def test_run_command_default_uses_requested_cwd():
    # sandbox_mode is now operator-config-only (not accepted as a model arg).
    # Patch the module-level flag so the handler runs in non-sandbox mode,
    # which lets the requested cwd take effect (finding 2 regression fix).
    with patch("mcp_server.tools.shell.SHELL_SANDBOX_MODE", False):
        result = await handle_run_command({
            "cmd": "pwd",
            "cwd": str(PROJECT_ROOT),
            "timeout": 5,
        })
    assert "exit_code: 0" in result
    assert str(PROJECT_ROOT) in result
    assert "sandbox: enabled" not in result


@pytest.mark.asyncio
async def test_run_command_sandbox_ignores_requested_cwd(tmp_path):
    # Patch sandbox on explicitly so the test is not dependent on the
    # default config value (finding 2: sandbox_mode arg is now ignored).
    with patch("mcp_server.tools.shell.SHELL_SANDBOX_MODE", True):
        result = await handle_run_command({
            "cmd": "pwd",
            "cwd": str(tmp_path),
            "timeout": 5,
        })
    assert "exit_code: 0" in result
    assert "sandbox: enabled" in result
    assert str(tmp_path) not in result
    assert "kim-shell-" in result


@pytest.mark.asyncio
async def test_run_command_sandbox_does_not_write_into_requested_cwd(tmp_path):
    target = tmp_path / "sandbox-leak-check.txt"
    with patch("mcp_server.tools.shell.SHELL_SANDBOX_MODE", True):
        result = await handle_run_command({
            "cmd": f"printf probe > {target.name}",
            "cwd": str(tmp_path),
            "timeout": 5,
        })
    assert "exit_code: 0" in result
    assert not target.exists()


def test_sandbox_env_uses_restricted_path():
    env = _sandbox_env()
    assert env["PATH"] == _SANDBOX_PATH
    assert os.getcwd() not in env["PATH"]


def test_shell_sandbox_env_override_enables_mode(monkeypatch):
    monkeypatch.setenv("KIM_SHELL_SANDBOX_MODE", "true")
    import mcp_server.config as config
    reloaded = importlib.reload(config)
    assert reloaded.SHELL_SANDBOX_MODE is True


def test_shell_sandbox_env_override_disables_mode(monkeypatch):
    monkeypatch.setenv("KIM_SHELL_SANDBOX_MODE", "0")
    import mcp_server.config as config
    reloaded = importlib.reload(config)
    assert reloaded.SHELL_SANDBOX_MODE is False

"""Regression tests for F-C-5 / F-C-6 (Wave-2 C').

F-C-5: a model-supplied timeout must be clamped for run_python/run_node
(code.py) and the web wait tools (navigation.py), exactly like shell.py's
MAX_SHELL_TIMEOUT_S. The MCP server serializes tool calls, so an unclamped
`timeout=999999` / `timeout_ms=10**12` pins the whole server — the same
client/server desync the shell clamp closes, reopened for code/web exec.

F-C-6: code-exec subprocesses must run in their own process group so a timeout
kill reaps the whole tree (grandchildren of an approved run_python *file* that
Popen'd freely) instead of leaking orphans.
"""
from __future__ import annotations

import inspect

import pytest

from mcp_server.config import CODE_TIMEOUT, SHELL_TIMEOUT
from mcp_server.tools import code as code_tool
from mcp_server.tools.code import MAX_CODE_TIMEOUT_S, _clamp_code_timeout
from mcp_server.tools.shell import MAX_SHELL_TIMEOUT_S
from mcp_server.tools.web import navigation as nav
from mcp_server.tools.web.navigation import MAX_WEB_WAIT_MS, _clamp_wait_ms


# ── F-C-5: code timeout clamp ─────────────────────────────────────────────────

def test_code_clamp_upper_bound():
    assert _clamp_code_timeout(999_999, CODE_TIMEOUT) == MAX_CODE_TIMEOUT_S
    assert _clamp_code_timeout(MAX_CODE_TIMEOUT_S + 1, CODE_TIMEOUT) == MAX_CODE_TIMEOUT_S


def test_code_clamp_lower_bound():
    assert _clamp_code_timeout(0, CODE_TIMEOUT) == 1
    assert _clamp_code_timeout(-5, CODE_TIMEOUT) == 1


def test_code_clamp_passthrough_and_default():
    assert _clamp_code_timeout(30, CODE_TIMEOUT) == 30
    assert _clamp_code_timeout("abc", CODE_TIMEOUT) == CODE_TIMEOUT
    assert _clamp_code_timeout(None, SHELL_TIMEOUT) == SHELL_TIMEOUT
    assert _clamp_code_timeout("45", CODE_TIMEOUT) == 45


def test_code_cap_matches_shell_cap():
    # Kept in sync with the shell ceiling (see code.py comment).
    assert MAX_CODE_TIMEOUT_S == MAX_SHELL_TIMEOUT_S


def test_handlers_route_timeout_through_clamp():
    # Source-level guard that the handlers use the clamp, not a bare int().
    src = inspect.getsource(code_tool)
    assert 'int(args.get("timeout"' not in src, (
        "a code handler still reads timeout with a bare int() — must clamp"
    )
    assert src.count("_clamp_code_timeout(args.get") >= 3


# ── F-C-5: web wait-tool clamp ────────────────────────────────────────────────

def test_web_wait_clamp_upper_bound():
    assert _clamp_wait_ms(10 ** 12) == MAX_WEB_WAIT_MS
    assert _clamp_wait_ms(MAX_WEB_WAIT_MS + 1) == MAX_WEB_WAIT_MS


def test_web_wait_clamp_lower_bound_and_default():
    assert _clamp_wait_ms(0) == 1
    assert _clamp_wait_ms(-1) == 1
    assert _clamp_wait_ms("nope") == 10_000
    assert _clamp_wait_ms(None) == 10_000


def test_web_wait_clamp_passthrough():
    assert _clamp_wait_ms(2500) == 2500


def test_web_wait_handlers_use_clamp():
    src = inspect.getsource(nav)
    assert 'int(args.get("timeout_ms", 10000))' not in src, (
        "a wait handler still reads timeout_ms with a bare int() — must clamp"
    )
    assert src.count("_clamp_wait_ms(args.get") >= 2


# ── F-C-6: code exec runs in its own process group + kills the tree ───────────

def test_run_exec_spawns_in_new_session():
    src = inspect.getsource(code_tool._run_exec)
    assert "start_new_session=not IS_WINDOWS" in src, (
        "code exec must spawn in its own process group (F-C-6)"
    )


def test_run_exec_kills_process_tree_on_timeout():
    src = inspect.getsource(code_tool._run_exec)
    # The timeout branch must reap the whole group, not just the interpreter.
    assert "_kill_process_tree(proc)" in src
    # And must not fall back to the bare single-process kill in that branch.
    timeout_branch = src.split("asyncio.TimeoutError")[1]
    assert "proc.kill()" not in timeout_branch


@pytest.mark.asyncio
async def test_run_python_timeout_is_clamped_end_to_end(monkeypatch):
    # A run_python with an absurd timeout must not be handed to _run_exec as-is.
    captured = {}

    async def _fake_run_exec(cmd, cwd=None, timeout=None, extra_env=None):
        captured["timeout"] = timeout
        return "exit_code: 0"

    monkeypatch.setattr(code_tool, "_run_exec", _fake_run_exec)
    await code_tool.handle_run_python({"code": "x = 1", "timeout": 999_999})
    assert captured["timeout"] == MAX_CODE_TIMEOUT_S

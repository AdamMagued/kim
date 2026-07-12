"""F-H-4 + F-INH-6 (Wave-2 C'): the MCP JSON-RPC seam's error contract.

F-H-4: the declared `inputSchema.required` argument contract is enforced at the
boundary — a call missing a required field returns a distinct, actionable
`BAD_ARGS:` error, not the one-word `ERROR: 'path'` KeyError leak.

F-INH-6: tool errors are distinguishable from tool output at the protocol level
— every error result carries `isError=True` (while keeping the legacy string
prefixes for back-compat); success carries `isError=False`.
"""
from __future__ import annotations

import asyncio

import pytest

from mcp.types import CallToolResult
from mcp_server import server as srv
from orchestrator.tool_errors import classify_tool_output


def _call(name: str, args: dict) -> CallToolResult:
    return asyncio.run(srv.call_tool(name, args))


# ── F-H-4: required-argument enforcement ──────────────────────────────────────

def test_missing_required_arg_returns_bad_args():
    result = _call("read_file", {})  # 'path' is required
    text = result.content[0].text
    assert text.startswith("BAD_ARGS:"), text
    assert "read_file" in text
    assert "'path'" in text
    assert result.isError is True


def test_missing_required_arg_does_not_leak_keyerror():
    # The whole point of F-H-4: no one-word `ERROR: 'path'` KeyError riddle.
    result = _call("read_file", {})
    text = result.content[0].text
    assert text != "ERROR: 'path'"
    assert not text.startswith("ERROR: '")


def test_bad_args_present_for_run_command_without_cmd(monkeypatch):
    # Even for a tool whose handler would otherwise run, a missing required arg
    # short-circuits before dispatch. run_command requires 'cmd'.
    ran = []

    async def _h(args):
        ran.append(1)
        return "ran"

    monkeypatch.setitem(srv._DISPATCH, "run_command", _h)
    result = _call("run_command", {})
    assert result.content[0].text.startswith("BAD_ARGS:")
    assert ran == []  # handler never dispatched


def test_valid_required_args_not_flagged(monkeypatch):
    async def _h(args):
        return "handler-output"

    monkeypatch.setitem(srv._DISPATCH, "read_file", _h)
    result = _call("read_file", {"path": "README.md"})
    assert result.content[0].text == "handler-output"
    assert result.isError is False


def test_bad_args_prefix_is_stable_for_the_classifier_handoff():
    # Team A handoff: tool_errors.py should map BAD_ARGS: → bad_args. Until then
    # it must at least not be silently classified as success/None. This pins the
    # prefix shape the classifier update will key on.
    result = _call("read_file", {})
    assert result.content[0].text.split(":", 1)[0] == "BAD_ARGS"


# ── F-INH-6: isError flag on the protocol result ──────────────────────────────

def test_unknown_tool_is_error():
    result = _call("no_such_tool_xyz", {})
    assert result.isError is True
    assert "Unknown tool" in result.content[0].text


def test_policy_deny_is_error():
    result = _call("run_command", {"cmd": "cp ~/.ssh/id_rsa /tmp"})
    assert result.isError is True
    assert "POLICY_DENIED" in result.content[0].text


def test_success_is_not_error(monkeypatch):
    async def _h(args):
        return "ok"

    monkeypatch.setitem(srv._DISPATCH, "read_file", _h)
    result = _call("read_file", {"path": "README.md"})
    assert result.isError is False
    assert result.content[0].text == "ok"


def test_string_prefixes_preserved_for_backcompat():
    # isError is ADDITIVE — the legacy string-prefix contract must still hold so
    # the in-repo agent parser keeps working during the migration.
    assert classify_tool_output("PERMISSION_ERROR: nope") == "permission_denied"
    assert classify_tool_output("ERROR: boom") == "execution_error"

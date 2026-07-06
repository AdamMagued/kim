"""Regression tests for the shell timeout-arg clamp + client budget (finding 2.1).

A model-supplied `timeout` must be bounded server-side, and the agent's
client-side call timeout must exceed the server's worst case so a call the
server is still running is never abandoned and re-issued (double execution).
"""
from __future__ import annotations

from mcp_server.tools.shell import _clamp_shell_timeout, MAX_SHELL_TIMEOUT_S
from mcp_server.config import SHELL_TIMEOUT


def test_clamp_upper_bound():
    assert _clamp_shell_timeout(10_000) == MAX_SHELL_TIMEOUT_S
    assert _clamp_shell_timeout(MAX_SHELL_TIMEOUT_S + 1) == MAX_SHELL_TIMEOUT_S


def test_clamp_lower_bound():
    assert _clamp_shell_timeout(0) == 1
    assert _clamp_shell_timeout(-99) == 1


def test_clamp_passthrough_valid():
    assert _clamp_shell_timeout(45) == 45
    assert _clamp_shell_timeout(MAX_SHELL_TIMEOUT_S) == MAX_SHELL_TIMEOUT_S


def test_clamp_bad_type_uses_default():
    assert _clamp_shell_timeout("abc") == SHELL_TIMEOUT
    assert _clamp_shell_timeout(None) == SHELL_TIMEOUT
    assert _clamp_shell_timeout([1]) == SHELL_TIMEOUT


def test_clamp_numeric_string():
    assert _clamp_shell_timeout("60") == 60


def test_client_budget_exceeds_server_worst_case():
    # The agent's client cap for a gated shell call must be strictly greater
    # than approval backstop + max shell execution, or it abandons a call the
    # server is still legitimately running (finding 2.1).
    from orchestrator import agent as _agent
    server_worst_case = _agent._APPROVAL_BACKSTOP_S + _agent._MAX_SHELL_EXEC_S
    client_cap = (
        _agent._APPROVAL_BACKSTOP_S
        + _agent._MAX_SHELL_EXEC_S
        + _agent._CLIENT_TIMEOUT_MARGIN_S
    )
    assert client_cap > server_worst_case
    # And the client's shell-exec budget must be >= the server-side hard cap.
    assert _agent._MAX_SHELL_EXEC_S >= MAX_SHELL_TIMEOUT_S

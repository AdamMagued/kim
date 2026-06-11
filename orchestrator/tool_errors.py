"""
Tool error classification for Kim's MCP tool outputs.

Tool handlers return plain-text results.  Failures are signalled by
well-known string prefixes (e.g. "PERMISSION_ERROR:", "BLOCKED:").
This module converts those prefixes into stable error codes so that
traces, HITL logic, and future recovery paths can branch on error type
without fragile substring searches spread through calling code.

Error codes (stable identifiers):
  permission_denied  — PERMISSION_ERROR: path outside root, git checkout blocked
  blocked            — BLOCKED: deny-list command or dangerous pattern
  timeout            — TIMEOUT: subprocess exceeded time limit
  os_limitation      — OS_LIMITATION: feature not available on this OS
  not_found          — NOT_FOUND: memory key, file, or resource absent
  execution_error    — ERROR: generic tool logic failure
  internal_error     — ERROR calling <tool>: MCP session / transport failure

Codes NOT yet populated (require source-level structured errors, planned):
  bad_args           — missing required argument, wrong type
"""

from __future__ import annotations

from typing import Optional

# Ordered: longest/most-specific prefixes first so that "ERROR calling "
# is checked before the bare "ERROR:" fallback.
_PREFIX_TO_CODE: list[tuple[str, str]] = [
    ("PERMISSION_ERROR:", "permission_denied"),
    ("BLOCKED:", "blocked"),
    ("TIMEOUT:", "timeout"),
    ("OS_LIMITATION:", "os_limitation"),
    ("NOT_FOUND:", "not_found"),
    # Session/transport failure emitted by agent.py when session.call_tool raises.
    ("ERROR calling ", "internal_error"),
    # Generic tool logic error — checked last because "ERROR " (without colon)
    # would also match "ERROR calling".
    ("ERROR:", "execution_error"),
]


def classify_tool_output(output: str) -> Optional[str]:
    """Return the error code for *output*, or None if output is not an error.

    Only the leading bytes of *output* are inspected; the classifier does not
    scan for keywords inside the body, to avoid false positives on tool results
    that contain words like "not found" in their legitimate content.
    """
    if not output:
        return None
    for prefix, code in _PREFIX_TO_CODE:
        if output.startswith(prefix):
            return code
    return None

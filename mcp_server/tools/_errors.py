"""Shared error-formatting helper for MCP tool handlers.

Every tool handler in ``mcp_server/tools/`` that needs to return an error
string should use :func:`tool_error` to guarantee the ``ERROR: `` prefix
that downstream callers (``orchestrator/tool_errors.py``, the MCP
``isError`` flag logic in ``server.py``, and integration tests) rely on.

Usage::

    from mcp_server.tools._errors import tool_error

    return tool_error("File not found: {path}")
    return tool_error(f"click failed for {el_id}: {e}")
    return tool_error(e)                  # shorthand for f"ERROR: {e}"
"""

from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)

_PREFIX = "ERROR: "


def tool_error(message: str | Exception, *, log: bool = False) -> str:
    """Return a plain-text error string with the canonical ``ERROR: `` prefix.

    Parameters
    ----------
    message:
        Human-readable description *or* an exception whose ``str()`` will be
        used.  The caller is responsible for interpolation — this function
        simply prepends the prefix so the contract stays DRY.
    log:
        If ``True``, emit a ``logger.error(...)`` call with the message.
        Most call-sites already log before calling, so this defaults to
        ``False``.

    Returns
    -------
    str
        ``f"ERROR: {message}"`` — always starts with ``"ERROR: "``.
    """
    text = str(message)
    if log:
        _logger.error(text)
    return f"{_PREFIX}{text}"

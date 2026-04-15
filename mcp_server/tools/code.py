"""
Kim MCP Server — Code Execution Tools (Phase 6)

Provides code execution and linting tools:
  - run_python:  Execute a .py file or inline snippet
  - run_node:    Execute a .js file or inline snippet
  - lint_file:   Run ruff or flake8 on a Python file

Uses os_utils for cross-platform safety and availability checks.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import os

from mcp_server.config import PROJECT_ROOT, SHELL_TIMEOUT, validate_path
from mcp_server.os_utils import check_tool_available

logger = logging.getLogger(__name__)


async def _run_exec(
    cmd: list[str],
    cwd: str = None,
    timeout: int = None,
) -> str:
    """Run a command via create_subprocess_exec and return formatted output."""
    resolved_cwd = cwd or str(PROJECT_ROOT)
    resolved_timeout = timeout or SHELL_TIMEOUT

    logger.info(f"code exec: {' '.join(cmd)} (cwd={resolved_cwd})")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=resolved_cwd,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=resolved_timeout
        )
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        exit_code = proc.returncode

        parts = [f"exit_code: {exit_code}"]
        if out.strip():
            parts.append(f"stdout:\n{out}")
        if err.strip():
            parts.append(f"stderr:\n{err}")
        if not out.strip() and not err.strip():
            parts.append("(no output)")
        return "\n".join(parts)
    except asyncio.TimeoutError:
        return f"ERROR: Command timed out after {resolved_timeout}s"
    except FileNotFoundError:
        return f"ERROR: '{cmd[0]}' is not installed or not found on PATH."
    except Exception as e:
        logger.error(f"code exec failed: {e}", exc_info=True)
        return f"ERROR: {e}"


def _find_python() -> str:
    """Find the best available Python executable."""
    # Prefer python3 on Unix, python on Windows
    for name in ("python3", "python"):
        if check_tool_available(name):
            return name
    return "python3"  # fallback, let it error naturally


def _find_node() -> str:
    """Find the best available Node.js executable."""
    if check_tool_available("node"):
        return "node"
    return "node"  # fallback


async def handle_run_python(args: dict) -> str:
    """
    Execute Python code.

    Accepts either:
      - 'file': path to a .py file (relative to PROJECT_ROOT or absolute)
      - 'code': inline Python code snippet to execute

    If both are provided, 'file' takes priority.
    """
    file_path = args.get("file", "")
    code = args.get("code", "")
    cwd = args.get("cwd", str(PROJECT_ROOT))
    timeout = int(args.get("timeout", SHELL_TIMEOUT))

    python = _find_python()

    if file_path:
        # Execute a .py file
        try:
            resolved = validate_path(file_path)
        except PermissionError as e:
            return f"PERMISSION_ERROR: {e}"

        if not resolved.exists():
            return f"ERROR: File not found: {resolved}"
        if not str(resolved).endswith(".py"):
            return f"ERROR: Expected a .py file, got: {resolved.name}"

        return await _run_exec([python, str(resolved)], cwd=cwd, timeout=timeout)

    elif code:
        # Execute an inline snippet via -c flag
        return await _run_exec([python, "-c", code], cwd=cwd, timeout=timeout)

    else:
        return "ERROR: Provide either 'file' (path to .py file) or 'code' (inline Python snippet)."


async def handle_run_node(args: dict) -> str:
    """
    Execute JavaScript/Node.js code.

    Accepts either:
      - 'file': path to a .js file (relative to PROJECT_ROOT or absolute)
      - 'code': inline JavaScript code snippet to execute

    If both are provided, 'file' takes priority.
    """
    file_path = args.get("file", "")
    code = args.get("code", "")
    cwd = args.get("cwd", str(PROJECT_ROOT))
    timeout = int(args.get("timeout", SHELL_TIMEOUT))

    node = _find_node()

    if not check_tool_available(node):
        return (
            "ERROR: Node.js ('node') is not installed or not found on PATH. "
            "Install Node.js from https://nodejs.org and try again."
        )

    if file_path:
        # Execute a .js file
        try:
            resolved = validate_path(file_path)
        except PermissionError as e:
            return f"PERMISSION_ERROR: {e}"

        if not resolved.exists():
            return f"ERROR: File not found: {resolved}"
        if not str(resolved).endswith((".js", ".mjs", ".cjs")):
            return f"ERROR: Expected a .js file, got: {resolved.name}"

        return await _run_exec([node, str(resolved)], cwd=cwd, timeout=timeout)

    elif code:
        # Execute an inline snippet via -e flag
        return await _run_exec([node, "-e", code], cwd=cwd, timeout=timeout)

    else:
        return "ERROR: Provide either 'file' (path to .js file) or 'code' (inline JavaScript snippet)."


async def handle_lint_file(args: dict) -> str:
    """
    Lint a Python file using ruff (preferred) or flake8 (fallback).

    Args:
      - 'path': Path to the Python file to lint.
      - 'fix': If True, attempt auto-fix (ruff only). Default False.
    """
    file_path = args.get("path", "")
    fix = args.get("fix", False)
    cwd = args.get("cwd", str(PROJECT_ROOT))
    timeout = int(args.get("timeout", SHELL_TIMEOUT))

    if not file_path:
        return "ERROR: 'path' parameter is required (path to Python file to lint)."

    try:
        resolved = validate_path(file_path)
    except PermissionError as e:
        return f"PERMISSION_ERROR: {e}"

    if not resolved.exists():
        return f"ERROR: File not found: {resolved}"

    # Prefer ruff, fall back to flake8
    if check_tool_available("ruff"):
        linter = "ruff"
        if fix:
            cmd = ["ruff", "check", "--fix", str(resolved)]
        else:
            cmd = ["ruff", "check", str(resolved)]
        logger.info(f"lint_file: using ruff for {resolved}")
    elif check_tool_available("flake8"):
        linter = "flake8"
        cmd = ["flake8", str(resolved)]
        if fix:
            logger.info("lint_file: --fix is not supported by flake8, running check only")
        logger.info(f"lint_file: using flake8 for {resolved}")
    else:
        return (
            "ERROR: No Python linter found. Install ruff ('pip install ruff') "
            "or flake8 ('pip install flake8') and try again."
        )

    result = await _run_exec(cmd, cwd=cwd, timeout=timeout)
    return f"[{linter}] {result}"

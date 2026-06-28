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
import re
import tempfile
import os

from mcp_server.config import PROJECT_ROOT, CODE_TIMEOUT, SHELL_TIMEOUT, validate_path
from mcp_server.os_utils import check_tool_available

logger = logging.getLogger(__name__)


async def _run_exec(
    cmd: list[str],
    cwd: str | None = None,
    timeout: int | None = None,
    extra_env: dict | None = None,
) -> str:
    """Run a command via create_subprocess_exec and return formatted output."""
    resolved_cwd = cwd or str(PROJECT_ROOT)
    resolved_timeout = timeout or CODE_TIMEOUT

    logger.info(f"code exec: {' '.join(cmd)} (cwd={resolved_cwd})")

    env = None
    if extra_env:
        import os as _os
        env = {**_os.environ, **extra_env}

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=resolved_cwd,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=resolved_timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                logger.warning("code exec process did not exit after kill")
            return f"TIMEOUT: command exceeded {resolved_timeout}s"
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
    for name in ("node", "nodejs"):
        if check_tool_available(name):
            return name
    raise RuntimeError("OS_LIMITATION: node not installed")


# Inline code blocklist patterns.
# WARNING: This blocklist provides only shallow defence-in-depth; it cannot
# replace a real OS-level sandbox (finding 4).  Patterns scan the *full*
# code string — no truncation — to prevent padding-based bypasses.
_CODE_BLOCKLIST = [
    re.compile(r"\bos\.system\b"),
    re.compile(r"\bsubprocess\b"),
    re.compile(r"__import__\s*\("),
    re.compile(r"\beval\s*\("),
    re.compile(r"\bexec\s*\("),
    # Catch `import os` / `import os as o` style alias bypasses (finding 4)
    re.compile(r"\bimport\s+os\b"),
    re.compile(r"\bfrom\s+os\b"),
]


def _check_code_blocked(code: str) -> str | None:
    """Scan *all* inline code for dangerous patterns (no truncation — finding 4)."""
    for pat in _CODE_BLOCKLIST:
        if pat.search(code):
            return f"BLOCKED: Inline code contains blocked pattern '{pat.pattern}'"
    return None


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
    timeout = int(args.get("timeout", CODE_TIMEOUT))

    # Validate cwd (#4)
    try:
        validate_path(cwd)
    except PermissionError as e:
        return f"PERMISSION_ERROR: cwd {e}"

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

        # Scan file content against the inline blocklist so that an attacker
        # cannot bypass inline checks by writing a .py file and then executing
        # it with run_python(file=...) (finding 4).
        try:
            file_content = resolved.read_text(encoding="utf-8", errors="replace")
            block_msg = _check_code_blocked(file_content)
            if block_msg:
                return block_msg
        except OSError as e:
            return f"ERROR: Cannot read file for security scan: {e}"

        return await _run_exec([python, str(resolved)], cwd=cwd, timeout=timeout)

    elif code:
        # Check for dangerous patterns in inline code
        block_msg = _check_code_blocked(code)
        if block_msg:
            return block_msg

        # Execute in isolated mode: -I strips user site-packages, no PYTHONSTARTUP
        return await _run_exec(
            [python, "-I", "-c", code],
            cwd=cwd,
            timeout=timeout,
            extra_env={"PYTHONNOUSERSITE": "1"},
        )

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
    timeout = int(args.get("timeout", CODE_TIMEOUT))

    # Validate cwd (#4)
    try:
        validate_path(cwd)
    except PermissionError as e:
        return f"PERMISSION_ERROR: cwd {e}"

    try:
        node = _find_node()
    except RuntimeError as e:
        return f"ERROR: {e}"

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
        # Apply the same inline-code blocklist used by run_python (finding 4)
        block_msg = _check_code_blocked(code)
        if block_msg:
            return block_msg

        # Execute inline snippet with restricted flags
        return await _run_exec(
            [node, "--disable-proto=delete", "-e", code],
            cwd=cwd,
            timeout=timeout,
        )

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

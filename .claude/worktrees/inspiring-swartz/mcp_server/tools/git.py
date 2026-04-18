"""
Kim MCP Server — Git Tools (Phase 6)

Provides repository management tools:
  - git_status:   Current working tree status
  - git_diff:     Diff of a file or all changes
  - git_add:      Stage files
  - git_commit:   Commit with message
  - git_log:      Last N commits
  - git_checkout: Switch branch or restore file

All commands run within PROJECT_ROOT via asyncio.create_subprocess_exec
for safety (no shell injection).
"""

from __future__ import annotations

import asyncio
import logging

from mcp_server.config import PROJECT_ROOT, SHELL_TIMEOUT

logger = logging.getLogger(__name__)


async def _run_git(*args: str, cwd: str = None, timeout: int = None) -> str:
    """
    Run a git command safely via create_subprocess_exec.
    Returns formatted output string with exit code, stdout, and stderr.
    """
    resolved_cwd = cwd or str(PROJECT_ROOT)
    resolved_timeout = timeout or SHELL_TIMEOUT

    cmd = ["git"] + list(args)
    logger.info(f"git: {' '.join(cmd)} (cwd={resolved_cwd})")

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
        return f"ERROR: git command timed out after {resolved_timeout}s"
    except FileNotFoundError:
        return (
            "ERROR: 'git' is not installed or not found on PATH. "
            "Install git and try again."
        )
    except Exception as e:
        logger.error(f"git command failed: {e}", exc_info=True)
        return f"ERROR: {e}"


async def handle_git_status(args: dict) -> str:
    """Return current working tree status."""
    cwd = args.get("cwd", str(PROJECT_ROOT))
    short = args.get("short", False)
    try:
        git_args = ["status"]
        if short:
            git_args.append("--short")
        return await _run_git(*git_args, cwd=cwd)
    except Exception as e:
        logger.error(f"git_status failed: {e}", exc_info=True)
        return f"ERROR: {e}"


async def handle_git_diff(args: dict) -> str:
    """
    Show diff of changes.
    - If 'path' is provided, diff only that file.
    - If 'staged' is True, show staged changes (--cached).
    """
    cwd = args.get("cwd", str(PROJECT_ROOT))
    path = args.get("path", "")
    staged = args.get("staged", False)
    try:
        git_args = ["diff"]
        if staged:
            git_args.append("--cached")
        if path:
            git_args.extend(["--", path])
        return await _run_git(*git_args, cwd=cwd)
    except Exception as e:
        logger.error(f"git_diff failed: {e}", exc_info=True)
        return f"ERROR: {e}"


async def handle_git_add(args: dict) -> str:
    """
    Stage files for commit.
    - 'paths' can be a single path string or list of paths.
    - If empty or '.', stages all changes.
    """
    cwd = args.get("cwd", str(PROJECT_ROOT))
    paths = args.get("paths", ".")
    try:
        if isinstance(paths, str):
            paths = [paths]
        git_args = ["add"] + paths
        return await _run_git(*git_args, cwd=cwd)
    except Exception as e:
        logger.error(f"git_add failed: {e}", exc_info=True)
        return f"ERROR: {e}"


async def handle_git_commit(args: dict) -> str:
    """
    Commit staged changes with a message.
    Requires 'message' parameter.
    """
    cwd = args.get("cwd", str(PROJECT_ROOT))
    message = args.get("message", "")
    if not message.strip():
        return "ERROR: Commit message is required. Provide a 'message' parameter."
    try:
        git_args = ["commit", "-m", message]
        return await _run_git(*git_args, cwd=cwd)
    except Exception as e:
        logger.error(f"git_commit failed: {e}", exc_info=True)
        return f"ERROR: {e}"


async def handle_git_log(args: dict) -> str:
    """
    Show recent commit history.
    - 'n' controls how many commits to show (default 10).
    - 'oneline' for compact output.
    """
    cwd = args.get("cwd", str(PROJECT_ROOT))
    n = int(args.get("n", 10))
    oneline = args.get("oneline", True)
    try:
        git_args = ["log", f"-{n}"]
        if oneline:
            git_args.append("--oneline")
        else:
            git_args.append("--format=%h %an %ar %s")
        return await _run_git(*git_args, cwd=cwd)
    except Exception as e:
        logger.error(f"git_log failed: {e}", exc_info=True)
        return f"ERROR: {e}"


async def handle_git_checkout(args: dict) -> str:
    """
    Switch branch or restore a file.
    - 'target' is the branch name or file path to checkout.
    - 'create' if True, creates a new branch (-b flag).
    """
    cwd = args.get("cwd", str(PROJECT_ROOT))
    target = args.get("target", "")
    create = args.get("create", False)
    if not target.strip():
        return "ERROR: 'target' parameter is required (branch name or file path)."
    try:
        git_args = ["checkout"]
        if create:
            git_args.append("-b")
        git_args.append(target)
        return await _run_git(*git_args, cwd=cwd)
    except Exception as e:
        logger.error(f"git_checkout failed: {e}", exc_info=True)
        return f"ERROR: {e}"

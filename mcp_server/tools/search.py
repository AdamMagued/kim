"""
Kim MCP Server — Search Tools (Phase 6)

Provides project-wide search capabilities:
  - search_in_files: grep/ripgrep-style text search across project files
  - find_files:      glob/find-style pattern matching for file discovery

Cross-platform: uses ripgrep (rg) if available, falls back to grep,
and uses Python's pathlib.glob as the universal fallback for find.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
from pathlib import Path

from mcp_server.config import PROJECT_ROOT, SHELL_TIMEOUT, validate_path
from mcp_server.os_utils import check_tool_available, IS_WINDOWS

logger = logging.getLogger(__name__)

# Max results to prevent overwhelming the LLM context window
MAX_SEARCH_RESULTS = 100
MAX_FIND_RESULTS = 200


async def _run_search_cmd(cmd: list[str], cwd: str, timeout: int) -> str:
    """Run a search command and return output."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        exit_code = proc.returncode

        # grep/rg return exit code 1 when no matches found — that's not an error
        if exit_code == 1 and not err.strip():
            return "No matches found."

        if exit_code not in (0, 1):
            return f"ERROR (exit {exit_code}): {err}" if err else f"ERROR: exit code {exit_code}"

        if not out.strip():
            return "No matches found."

        # Truncate if too many results
        lines = out.strip().split("\n")
        if len(lines) > MAX_SEARCH_RESULTS:
            truncated = "\n".join(lines[:MAX_SEARCH_RESULTS])
            return (
                f"{truncated}\n\n... truncated ({len(lines)} total matches, "
                f"showing first {MAX_SEARCH_RESULTS}). Narrow your search pattern."
            )

        return out.strip()
    except asyncio.TimeoutError:
        return f"ERROR: Search timed out after {timeout}s. Try a more specific pattern."
    except FileNotFoundError:
        return f"ERROR: '{cmd[0]}' is not installed or not found on PATH."
    except Exception as e:
        logger.error(f"search command failed: {e}", exc_info=True)
        return f"ERROR: {e}"


async def handle_search_in_files(args: dict) -> str:
    """
    Search for a text pattern across files in the project.

    Uses ripgrep (rg) if available, otherwise falls back to grep.
    On Windows without rg or grep, falls back to findstr.

    Args:
      - 'pattern':       Text or regex pattern to search for (required).
      - 'path':          Directory to search in (default: PROJECT_ROOT).
      - 'include':       File glob to filter (e.g. '*.py', '*.ts'). Optional.
      - 'case_sensitive': Whether search is case-sensitive (default: True).
      - 'regex':         Whether pattern is a regex (default: False for literal match).
      - 'context_lines': Number of context lines around matches (default: 0).
    """
    pattern = args.get("pattern", "")
    if not pattern:
        return "ERROR: 'pattern' parameter is required."

    search_path = args.get("path", str(PROJECT_ROOT))
    include = args.get("include", "")
    case_sensitive = args.get("case_sensitive", True)
    is_regex = args.get("regex", False)
    context_lines = int(args.get("context_lines", 0))
    timeout = int(args.get("timeout", SHELL_TIMEOUT))

    # Validate search path
    try:
        resolved_path = validate_path(search_path)
    except PermissionError as e:
        return f"PERMISSION_ERROR: {e}"

    search_dir = str(resolved_path)

    # ── Try ripgrep first (fastest, best output) ─────────────────────────
    if check_tool_available("rg"):
        cmd = ["rg", "--no-heading", "--line-number", "--color=never"]
        if not case_sensitive:
            cmd.append("--ignore-case")
        if not is_regex:
            cmd.append("--fixed-strings")
        if context_lines > 0:
            cmd.extend([f"--context={context_lines}"])
        if include:
            cmd.extend(["--glob", include])
        cmd.extend(["--max-count=500", pattern, search_dir])

        logger.info(f"search_in_files [rg]: pattern={pattern!r} path={search_dir}")
        return await _run_search_cmd(cmd, cwd=search_dir, timeout=timeout)

    # ── Fallback to grep ─────────────────────────────────────────────────
    if check_tool_available("grep"):
        cmd = ["grep", "-r", "-n", "--color=never"]
        if not case_sensitive:
            cmd.append("-i")
        if not is_regex:
            cmd.append("-F")
        if context_lines > 0:
            cmd.extend([f"-C{context_lines}"])
        if include:
            cmd.extend(["--include", include])
        cmd.extend([pattern, search_dir])

        logger.info(f"search_in_files [grep]: pattern={pattern!r} path={search_dir}")
        return await _run_search_cmd(cmd, cwd=search_dir, timeout=timeout)

    # ── Windows fallback: findstr ────────────────────────────────────────
    if IS_WINDOWS and check_tool_available("findstr"):
        cmd = ["findstr", "/S", "/N"]
        if not case_sensitive:
            cmd.append("/I")
        if is_regex:
            cmd.append("/R")
        else:
            cmd.append("/C:" + pattern)
            pattern = None  # already embedded in /C:
        if include:
            cmd.append(os.path.join(search_dir, include))
        else:
            cmd.append(os.path.join(search_dir, "*"))
        if pattern:
            cmd.append(pattern)

        logger.info(f"search_in_files [findstr]: path={search_dir}")
        return await _run_search_cmd(cmd, cwd=search_dir, timeout=timeout)

    return (
        "ERROR: No search tool available. Install ripgrep ('brew install ripgrep' / "
        "'apt install ripgrep' / 'scoop install ripgrep') for best results, "
        "or ensure grep is on your PATH."
    )


async def handle_find_files(args: dict) -> str:
    """
    Find files matching a glob pattern within the project.

    Uses Python's pathlib.glob for universal cross-platform support.

    Args:
      - 'pattern':  Glob pattern to match (e.g. '*.py', '**/*.ts', 'src/**/*.js').
                     Required.
      - 'path':     Directory to search in (default: PROJECT_ROOT).
      - 'type':     Filter by type: 'file', 'dir', or 'all' (default: 'file').
    """
    pattern = args.get("pattern", "")
    if not pattern:
        return "ERROR: 'pattern' parameter is required (e.g. '*.py', '**/*.ts')."

    search_path = args.get("path", str(PROJECT_ROOT))
    type_filter = args.get("type", "file")

    # Validate search path
    try:
        resolved_path = validate_path(search_path)
    except PermissionError as e:
        return f"PERMISSION_ERROR: {e}"

    if not resolved_path.is_dir():
        return f"ERROR: '{resolved_path}' is not a directory."

    logger.info(f"find_files: pattern={pattern!r} path={resolved_path} type={type_filter}")

    try:
        # Auto-prepend **/ if the pattern doesn't contain a path separator
        # and doesn't start with ** — this makes '*.py' match recursively
        search_pattern = pattern
        if "/" not in pattern and not pattern.startswith("**"):
            search_pattern = "**/" + pattern

        results = []
        for match in resolved_path.glob(search_pattern):
            # Skip hidden dirs and common noise
            parts = match.relative_to(resolved_path).parts
            if any(p.startswith(".") for p in parts):
                continue
            if any(p in ("node_modules", "__pycache__", ".git", "venv", ".venv") for p in parts):
                continue

            # Apply type filter
            if type_filter == "file" and not match.is_file():
                continue
            if type_filter == "dir" and not match.is_dir():
                continue

            rel = match.relative_to(resolved_path)
            suffix = "/" if match.is_dir() else ""
            size = ""
            if match.is_file():
                try:
                    bytes_size = match.stat().st_size
                    if bytes_size < 1024:
                        size = f"  ({bytes_size} B)"
                    elif bytes_size < 1024 * 1024:
                        size = f"  ({bytes_size / 1024:.1f} KB)"
                    else:
                        size = f"  ({bytes_size / (1024*1024):.1f} MB)"
                except OSError:
                    pass

            results.append(f"{rel}{suffix}{size}")

            if len(results) >= MAX_FIND_RESULTS:
                break

        if not results:
            return f"No files found matching pattern '{pattern}' in {resolved_path}"

        header = f"Found {len(results)} match(es)"
        if len(results) >= MAX_FIND_RESULTS:
            header += f" (truncated at {MAX_FIND_RESULTS}, narrow your pattern)"
        header += f" in {resolved_path}:\n"

        return header + "\n".join(results)

    except Exception as e:
        logger.error(f"find_files failed: {e}", exc_info=True)
        return f"ERROR: {e}"

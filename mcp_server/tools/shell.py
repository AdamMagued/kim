"""
Kim MCP Server — Shell Execution Tools

Provides run_command and run_powershell tools with:
  - Blocked-command filtering
  - Cross-platform command translation via os_utils
  - Platform-aware PowerShell / bash fallback
"""

from __future__ import annotations

import asyncio
import logging

from mcp_server.config import BLOCKED_COMMANDS, SHELL_TIMEOUT, validate_path, PROJECT_ROOT
from mcp_server.os_utils import (
    CURRENT_OS,
    IS_WINDOWS,
    IS_MACOS,
    IS_LINUX,
    translate_command,
)

logger = logging.getLogger(__name__)


def _check_blocked(cmd: str) -> str | None:
    cmd_lower = cmd.lower().strip()
    for blocked in BLOCKED_COMMANDS:
        if blocked.lower() in cmd_lower:
            return f"BLOCKED: Command matches blocked pattern '{blocked}'"
    return None


async def handle_run_command(args: dict) -> str:
    cmd = args["cmd"]
    cwd = str(args.get("cwd", str(PROJECT_ROOT)))
    timeout = int(args.get("timeout", SHELL_TIMEOUT))

    block_msg = _check_blocked(cmd)
    if block_msg:
        logger.warning(f"run_command BLOCKED: {cmd}")
        return block_msg

    try:
        validate_path(cwd)
    except PermissionError as e:
        return f"PERMISSION_ERROR: cwd {e}"

    # ── Cross-platform translation ───────────────────────────────────────
    original_cmd = cmd
    cmd = translate_command(cmd)
    if cmd != original_cmd:
        logger.info(f"run_command translated: {original_cmd!r} → {cmd!r}")

    logger.info(f"run_command: {cmd!r} cwd={cwd}")
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        exit_code = proc.returncode
        parts = [f"exit_code: {exit_code}"]
        if out:
            parts.append(f"stdout:\n{out}")
        if err:
            parts.append(f"stderr:\n{err}")
        return "\n".join(parts)
    except asyncio.TimeoutError:
        return f"ERROR: Command timed out after {timeout}s"
    except Exception as e:
        logger.error(f"run_command failed: {e}", exc_info=True)
        return f"ERROR: {e}"


async def handle_run_powershell(args: dict) -> str:
    """
    Run a PowerShell script block.

    Cross-platform behaviour:
      - Windows: Runs natively via powershell.exe
      - macOS/Linux: Attempts to use pwsh (PowerShell Core) if installed.
        If pwsh is not available, returns a clear error message suggesting
        the LLM use run_command with bash/zsh instead.
    """
    script = args["script"]
    timeout = int(args.get("timeout", SHELL_TIMEOUT))

    block_msg = _check_blocked(script)
    if block_msg:
        logger.warning("run_powershell BLOCKED")
        return block_msg

    # ── Determine PowerShell executable ──────────────────────────────────
    if IS_WINDOWS:
        ps_exe = "powershell.exe"
    else:
        # macOS/Linux: try PowerShell Core (pwsh)
        import shutil
        ps_exe = shutil.which("pwsh")
        if ps_exe is None:
            os_name = "macOS" if IS_MACOS else "Linux"
            return (
                f"OS_LIMITATION: PowerShell is not available on this {os_name} system. "
                f"PowerShell Core (pwsh) is not installed. "
                f"Please use the 'run_command' tool with bash/zsh syntax instead. "
                f"For example, replace 'Get-ChildItem' with 'ls -la', "
                f"'Get-Content file.txt' with 'cat file.txt', etc."
            )

    logger.info(f"run_powershell [{ps_exe}]: {script[:80]}...")
    try:
        ps_args = [
            ps_exe,
            "-NonInteractive",
            "-NoProfile",
        ]
        # Only Windows powershell.exe needs -ExecutionPolicy
        if IS_WINDOWS:
            ps_args.extend(["-ExecutionPolicy", "Bypass"])
        ps_args.extend(["-Command", script])

        proc = await asyncio.create_subprocess_exec(
            *ps_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(PROJECT_ROOT),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        parts = [f"exit_code: {proc.returncode}"]
        if out:
            parts.append(f"stdout:\n{out}")
        if err:
            parts.append(f"stderr:\n{err}")
        return "\n".join(parts)
    except asyncio.TimeoutError:
        return f"ERROR: PowerShell timed out after {timeout}s"
    except Exception as e:
        logger.error(f"run_powershell failed: {e}", exc_info=True)
        return f"ERROR: {e}"

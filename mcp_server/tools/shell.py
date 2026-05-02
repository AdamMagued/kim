"""
Kim MCP Server — Shell Execution Tools

Provides run_command and run_powershell tools with:
  - Blocked-command filtering (shlex-based, exact-match deny set)
  - Metacharacter rejection for command chaining
  - Cross-platform command translation via os_utils
  - Platform-aware PowerShell / bash fallback
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex

from mcp_server.config import SHELL_TIMEOUT, validate_path, PROJECT_ROOT
from mcp_server.os_utils import (
    CURRENT_OS,
    IS_WINDOWS,
    IS_MACOS,
    IS_LINUX,
    translate_command,
)

logger = logging.getLogger(__name__)

# ── Deny sets (#2 — stronger shell blocklist) ─────────────────────────────────

# Commands that are unconditionally blocked (first token after shlex.split)
_DENY_COMMANDS = frozenset({
    "rm", "rmdir", "del", "format", "diskpart", "mkfs", "dd", "shred",
})

# Regex patterns that catch common destructive payloads even in arguments
_DENY_PATTERNS = [
    re.compile(r":\(\)\s*\{[^}]*\|[^}]*&\s*\}\s*;?\s*:", re.DOTALL),  # fork bomb
    re.compile(r"\bchmod\s+(-\w\s+)*777\s+/\s*$"),  # chmod -R 777 /
    re.compile(r"\bdd\b.*\bif=/dev/zero\b"),  # dd if=/dev/zero
]

# Metacharacters that enable command chaining / injection
_CHAIN_METACHAR_RE = re.compile(r"[;|&`]|\$\(")


def _check_blocked(cmd: str, allow_chaining: bool = False) -> str | None:
    """Check if a command should be blocked. Returns an error message or None."""
    cmd_stripped = cmd.strip()

    # 1. Check for dangerous regex patterns in raw command
    for pat in _DENY_PATTERNS:
        if pat.search(cmd_stripped):
            return f"BLOCKED: Command matches dangerous pattern"

    # 2. Reject command chaining metacharacters unless explicitly allowed
    if not allow_chaining and _CHAIN_METACHAR_RE.search(cmd_stripped):
        return (
            "BLOCKED: Command contains chaining metacharacters (;, &&, ||, |, `, $(...)). "
            "Use separate run_command calls for each command, or pass allow_chaining=True."
        )

    # 3. Parse with shlex and check first token against deny set
    try:
        tokens = shlex.split(cmd_stripped)
    except ValueError:
        # Malformed quoting — treat as suspicious
        return "BLOCKED: Command has malformed shell quoting"

    if not tokens:
        return None

    first_cmd = tokens[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()  # basename
    if first_cmd in _DENY_COMMANDS:
        return f"BLOCKED: '{first_cmd}' is a blocked command"

    # 4. Special case: rm with -r/-rf and a path that looks like root or parent traversal
    if first_cmd == "rm" or (len(tokens) > 1 and tokens[0] in ("sudo",) and tokens[1] == "rm"):
        flags = " ".join(tokens)
        if re.search(r"-\w*r\w*", flags) and re.search(r"\s+/\s*$|\s+\.\.\s*$", " " + flags):
            return "BLOCKED: recursive rm targeting root or parent directory"

    return None


async def handle_run_command(args: dict) -> str:
    cmd = args["cmd"]
    cwd = str(args.get("cwd", str(PROJECT_ROOT)))
    timeout = int(args.get("timeout", SHELL_TIMEOUT))
    allow_chaining = bool(args.get("allow_chaining", False))

    block_msg = _check_blocked(cmd, allow_chaining=allow_chaining)
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
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                logger.warning("run_command process did not exit after kill")
            return f"TIMEOUT: command exceeded {timeout}s"
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        exit_code = proc.returncode
        parts = [f"exit_code: {exit_code}"]
        if out:
            parts.append(f"stdout:\n{out}")
        if err:
            parts.append(f"stderr:\n{err}")
        return "\n".join(parts)
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

    block_msg = _check_blocked(script, allow_chaining=True)  # PS scripts naturally chain
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
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                logger.warning("run_powershell process did not exit after kill")
            return f"TIMEOUT: PowerShell exceeded {timeout}s"
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        parts = [f"exit_code: {proc.returncode}"]
        if out:
            parts.append(f"stdout:\n{out}")
        if err:
            parts.append(f"stderr:\n{err}")
        return "\n".join(parts)
    except Exception as e:
        logger.error(f"run_powershell failed: {e}", exc_info=True)
        return f"ERROR: {e}"

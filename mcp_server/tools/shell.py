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
import os
import logging
import re
import shlex
import tempfile

from mcp_server.config import SHELL_SANDBOX_MODE, SHELL_TIMEOUT, validate_path, PROJECT_ROOT
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

# Metacharacters that enable command chaining / injection.
# \n and \r are included because the POSIX shell treats newline identically
# to semicolon as a command separator, making them a bypass vector for the
# allow_chaining=False guard when the string is passed to create_subprocess_shell.
_CHAIN_METACHAR_RE = re.compile(r"[;|&`\n\r]|\$\(")

_SANDBOX_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _sandbox_enabled(args: dict) -> bool:
    if "sandbox_mode" in args:
        return bool(args.get("sandbox_mode"))
    return SHELL_SANDBOX_MODE


def _sandbox_env() -> dict[str, str]:
    env = {
        "PATH": _SANDBOX_PATH,
        "HOME": str(PROJECT_ROOT),
        "TMPDIR": tempfile.gettempdir(),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", ""),
    }
    return {key: value for key, value in env.items() if value}


def _basename(token: str) -> str:
    return token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()


def _first_non_option(tokens: list[str], start: int = 0) -> str | None:
    for token in tokens[start:]:
        if token == "--":
            continue
        if token.startswith("-"):
            continue
        if "=" in token and not token.startswith(("/", "\\")):
            continue
        return token
    return None


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

    first_cmd = _basename(tokens[0])
    if first_cmd in _DENY_COMMANDS:
        return f"BLOCKED: '{first_cmd}' is a blocked command"

    # 4. Wrapper commands can otherwise hide a blocked command as the next token.
    if first_cmd in {"sudo", "doas", "command", "env", "nohup", "nice", "time"}:
        wrapped = _first_non_option(tokens, 1)
        if wrapped:
            wrapped_name = _basename(wrapped)
            if wrapped_name in _DENY_COMMANDS:
                return f"BLOCKED: '{wrapped_name}' is a blocked command"
            wrapped_index = tokens.index(wrapped)
            if wrapped_name in {"sudo", "doas", "command", "env", "nohup", "nice", "time", "sh", "bash", "zsh", "fish"}:
                nested_msg = _check_blocked(" ".join(tokens[wrapped_index:]), allow_chaining=allow_chaining)
                if nested_msg:
                    return f"BLOCKED: wrapper contains blocked command. {nested_msg}"

    # 5. Shell wrappers (`bash -c`, `sh -c`, etc.) must recursively vet the script.
    if first_cmd in {"sh", "bash", "zsh", "fish"}:
        try:
            c_index = tokens.index("-c")
        except ValueError:
            c_index = -1
        if c_index >= 0 and c_index + 1 < len(tokens):
            nested_msg = _check_blocked(tokens[c_index + 1], allow_chaining=allow_chaining)
            if nested_msg:
                return f"BLOCKED: shell wrapper contains blocked command. {nested_msg}"

    return None


async def handle_run_command(args: dict) -> str:
    cmd = args["cmd"]
    cwd = str(args.get("cwd", str(PROJECT_ROOT)))
    timeout = int(args.get("timeout", SHELL_TIMEOUT))
    allow_chaining = bool(args.get("allow_chaining", False))
    sandbox_mode = _sandbox_enabled(args)

    block_msg = _check_blocked(cmd, allow_chaining=allow_chaining)
    if block_msg:
        logger.warning(f"run_command BLOCKED: {cmd}")
        return block_msg

    if not sandbox_mode:
        try:
            validate_path(cwd)
        except PermissionError as e:
            return f"PERMISSION_ERROR: cwd {e}"

    # ── Cross-platform translation ───────────────────────────────────────
    original_cmd = cmd
    cmd = translate_command(cmd)
    if cmd != original_cmd:
        logger.info(f"run_command translated: {original_cmd!r} → {cmd!r}")
        # The translated string is what actually executes — vet it too, since
        # translation can rewrite a benign-looking token into a blocked one.
        block_msg = _check_blocked(cmd, allow_chaining=allow_chaining)
        if block_msg:
            logger.warning(f"run_command BLOCKED after translation: {cmd}")
            return block_msg

    logger.info(f"run_command: {cmd!r} cwd={cwd} sandbox={sandbox_mode}")
    try:
        sandbox_dir = None
        if sandbox_mode:
            sandbox_dir = tempfile.TemporaryDirectory(prefix="kim-shell-")
            exec_cwd = sandbox_dir.name
            env = _sandbox_env()
        else:
            exec_cwd = cwd
            env = None

        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=exec_cwd,
            env=env,
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
        if sandbox_mode:
            parts.append("sandbox: enabled")
        return "\n".join(parts)
    except Exception as e:
        logger.error(f"run_command failed: {e}", exc_info=True)
        return f"ERROR: {e}"
    finally:
        if 'sandbox_dir' in locals() and sandbox_dir is not None:
            sandbox_dir.cleanup()


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
    sandbox_mode = _sandbox_enabled(args)

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

    logger.info(f"run_powershell [{ps_exe}]: {script[:80]}... sandbox={sandbox_mode}")
    try:
        sandbox_dir = None
        if sandbox_mode:
            sandbox_dir = tempfile.TemporaryDirectory(prefix="kim-powershell-")
            exec_cwd = sandbox_dir.name
            env = _sandbox_env()
        else:
            exec_cwd = str(PROJECT_ROOT)
            env = None

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
            cwd=exec_cwd,
            env=env,
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
        if sandbox_mode:
            parts.append("sandbox: enabled")
        return "\n".join(parts)
    except Exception as e:
        logger.error(f"run_powershell failed: {e}", exc_info=True)
        return f"ERROR: {e}"
    finally:
        if 'sandbox_dir' in locals() and sandbox_dir is not None:
            sandbox_dir.cleanup()

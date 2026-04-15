import asyncio
import logging

from mcp_server.config import BLOCKED_COMMANDS, SHELL_TIMEOUT, validate_path, PROJECT_ROOT

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
    script = args["script"]
    timeout = int(args.get("timeout", SHELL_TIMEOUT))

    block_msg = _check_blocked(script)
    if block_msg:
        logger.warning("run_powershell BLOCKED")
        return block_msg

    logger.info(f"run_powershell: {script[:80]}...")
    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe",
            "-NonInteractive",
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-Command", script,
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

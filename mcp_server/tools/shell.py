"""
Kim MCP Server — Shell Execution Tools

Provides run_command and run_powershell tools with:
  - Blocked-command filtering (shlex-based, exact-match deny set)
  - Metacharacter rejection for command chaining, process substitution, and
    subshell grouping
  - Per-segment vetting when allow_chaining=True (finding 2)
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

# ── Deny sets ─────────────────────────────────────────────────────────────────

# Commands that are unconditionally blocked (first token after shlex.split).
# Includes network-transfer tools that can exfiltrate secret files in a single
# call, bypassing the validate_path sandbox (finding 3, partial — cat/cp/mv
# require a tier-based confirmation system outside this module's scope).
_DENY_COMMANDS = frozenset({
    # Destructive filesystem tools
    "rm", "rmdir", "del", "format", "diskpart", "mkfs", "dd", "shred",
    # Truncation (finding 5)
    "truncate",
    # Network / exfiltration tools (finding 3, partial)
    "curl", "wget", "scp", "rsync", "nc", "netcat",
})

# Regex patterns that catch common destructive payloads even in arguments
_DENY_PATTERNS = [
    re.compile(r":\(\)\s*\{[^}]*\|[^}]*&\s*\}\s*;?\s*:", re.DOTALL),  # fork bomb
    re.compile(r"\bchmod\s+(-\w\s+)*777\s+/\s*$"),  # chmod -R 777 /
    re.compile(r"\bdd\b.*\bif=/dev/zero\b"),  # dd if=/dev/zero
]

# Fast-path metacharacter regex: catches chaining, command/process substitution.
# NOTE: bare '>' and '<' (plain redirection) are intentionally excluded here —
# existing callers depend on redirection working (e.g. `printf x > file`).
# Process substitution '<(...)' / '>(' is caught by the '<\(' / '>\(' patterns.
# \n and \r are included: POSIX shells treat newline like ';' as a separator.
_CHAIN_METACHAR_RE = re.compile(r"[;|&`\n\r]|\$\(|<\(|>\(")

# Operator-level split used to isolate individual segments when chaining is
# allowed.  '&&' and '||' must appear before the single-char alternatives so
# the two-char forms consume both characters first (finding 2).
_OPERATOR_SPLIT_RE = re.compile(r"&&|\|\||[;|\n\r&]")

# Regex for shell redirection operators after shlex.split (e.g. ">", ">>", "2>", "&>").
# Used to detect redirections targeting absolute paths (finding 1a).
# Plain redirection to a *relative* name is allowed so that `printf probe > file`
# keeps working in sandbox tests.
_REDIR_OP_RE = re.compile(r"^\d*>>?$|^&>>?$")

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


def _check_single_segment(cmd: str) -> str | None:
    """Vet one already-isolated command segment (no chaining metacharacters
    expected at this level).  Returns an error string or None if safe."""
    cmd_clean = cmd.strip()
    if not cmd_clean:
        return None

    # Subshell group: an unquoted leading '(' opens a subshell (finding 1)
    if cmd_clean.startswith("("):
        return "BLOCKED: Command contains a subshell group"

    # Backtick command substitution: shlex does NOT strip backticks, so they
    # survive as literal characters and execute via /bin/sh.  Block here so
    # that `echo \`rm -rf /tmp/x\`` is caught even when allow_chaining=True
    # (finding 1b — backtick stays in one segment after operator split).
    if "`" in cmd_clean:
        return "BLOCKED: Command contains backtick command substitution"

    try:
        tokens = shlex.split(cmd_clean)
    except ValueError:
        # Malformed quoting — treat as suspicious
        return "BLOCKED: Command has malformed shell quoting"

    if not tokens:
        return None

    # Detect process/command substitution tokens that may survive operator
    # splitting (e.g. the <(rm ...) token in `cat <(rm ...)`) (finding 1)
    for token in tokens:
        if token.startswith(("<(", ">(", "$(")):
            return "BLOCKED: Command contains process or command substitution"

    # Redirection to an absolute path or parent-directory traversal is blocked
    # (finding 1a).  Relative redirects (`printf probe > file`) are allowed so
    # the sandbox write-isolation test keeps passing.
    for i, tok in enumerate(tokens):
        if _REDIR_OP_RE.match(tok) and i + 1 < len(tokens):
            target = tokens[i + 1]
            if target.startswith("/") or ".." in target:
                return "BLOCKED: Redirection to absolute path or parent traversal"

    first_cmd = _basename(tokens[0])
    if first_cmd in _DENY_COMMANDS:
        return f"BLOCKED: '{first_cmd}' is a blocked command"

    # Dangerous find usage: -delete flag or -exec/-execdir with a blocked cmd
    # (finding 5)
    if first_cmd == "find":
        if "-delete" in tokens:
            return "BLOCKED: 'find -delete' is a blocked pattern"
        for i, t in enumerate(tokens):
            if t in {"-exec", "-execdir"} and i + 1 < len(tokens):
                exec_name = _basename(tokens[i + 1])
                if exec_name in _DENY_COMMANDS:
                    return f"BLOCKED: 'find {t} {exec_name}' is a blocked pattern"

    # xargs executes its first non-option argument as a command.
    # Handles both `-I{}` (no-space) and `-I {}` (space-separated) replacement
    # forms: skip the placeholder token that follows `-I` so that the actual
    # command is correctly identified (finding 2 / finding 5).
    if first_cmd == "xargs":
        xargs_cmd: str | None = None
        i = 1
        while i < len(tokens):
            t = tokens[i]
            if t == "-I" and i + 1 < len(tokens):
                # Space-separated: `-I PLACEHOLDER COMMAND …` — skip placeholder
                i += 2
                continue
            if t == "--":
                i += 1
                continue
            if t.startswith("-"):
                i += 1
                continue
            if "=" in t and not t.startswith(("/", "\\")):
                i += 1
                continue
            xargs_cmd = t
            break
        if xargs_cmd:
            xargs_name = _basename(xargs_cmd)
            if xargs_name in _DENY_COMMANDS:
                return f"BLOCKED: 'xargs {xargs_name}' is a blocked command"

    # Wrapper commands can hide a blocked command as the next token.
    # busybox is included because it proxies any coreutil by name (finding 5).
    if first_cmd in {"sudo", "doas", "command", "env", "nohup", "nice", "time", "busybox"}:
        wrapped = _first_non_option(tokens, 1)
        if wrapped:
            wrapped_name = _basename(wrapped)
            if wrapped_name in _DENY_COMMANDS:
                return f"BLOCKED: '{wrapped_name}' is a blocked command"
            wrapped_index = tokens.index(wrapped)
            if wrapped_name in {"sudo", "doas", "command", "env", "nohup", "nice", "time", "sh", "bash", "zsh", "fish"}:
                nested_msg = _check_single_segment(" ".join(tokens[wrapped_index:]))
                if nested_msg:
                    return f"BLOCKED: wrapper contains blocked command. {nested_msg}"
            # busybox can proxy `find` — apply find-specific -delete/-exec checks
            # even though `find` itself is not in _DENY_COMMANDS (finding 5).
            if wrapped_name == "find":
                subtokens = tokens[wrapped_index:]
                if "-delete" in subtokens:
                    return "BLOCKED: 'find -delete' is a blocked pattern (via wrapper)"
                for i, t in enumerate(subtokens):
                    if t in {"-exec", "-execdir"} and i + 1 < len(subtokens):
                        exec_name = _basename(subtokens[i + 1])
                        if exec_name in _DENY_COMMANDS:
                            return f"BLOCKED: 'find {t} {exec_name}' is a blocked pattern (via wrapper)"

    # Shell wrappers (`bash -c`, `sh -c`, etc.) must recursively vet the script.
    if first_cmd in {"sh", "bash", "zsh", "fish"}:
        try:
            c_index = tokens.index("-c")
        except ValueError:
            c_index = -1
        if c_index >= 0 and c_index + 1 < len(tokens):
            nested_msg = _check_blocked(tokens[c_index + 1], allow_chaining=False)
            if nested_msg:
                return f"BLOCKED: shell wrapper contains blocked command. {nested_msg}"

    return None


def _check_blocked(cmd: str, allow_chaining: bool = False) -> str | None:
    """Check if a command should be blocked. Returns an error message or None."""
    cmd_stripped = cmd.strip()

    # 1. Check for dangerous regex patterns in raw command
    for pat in _DENY_PATTERNS:
        if pat.search(cmd_stripped):
            return "BLOCKED: Command matches dangerous pattern"

    # 2. Subshell-group fast path: a command that starts with '(' is a subshell
    #    (finding 1 — catches `(rm -rf /)` before shlex parsing)
    if cmd_stripped.startswith("("):
        return "BLOCKED: Command starts with a subshell group"

    # 3. Metacharacter handling
    if _CHAIN_METACHAR_RE.search(cmd_stripped):
        if not allow_chaining:
            return (
                "BLOCKED: Command contains chaining/injection metacharacters "
                "(;, &&, ||, |, `, $(...), <(...), >(...) or similar). "
                "Use separate run_command calls for each command, or pass allow_chaining=True."
            )
        # When chaining is allowed, split on operators and vet every individual
        # segment so that `true && rm -rf /` cannot sneak through (finding 2).
        # Note: splitting on raw operators cannot respect quoted operators (e.g.
        # a semicolon inside a string literal); this is an accepted trade-off
        # between security and a full POSIX-shell parse.
        segments = _OPERATOR_SPLIT_RE.split(cmd_stripped)
        for raw_seg in segments:
            seg_msg = _check_single_segment(raw_seg)
            if seg_msg:
                return seg_msg
        return None

    # 4. No metacharacters detected: vet as a single command segment
    return _check_single_segment(cmd_stripped)


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
    sandbox_dir = None
    try:
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
        if sandbox_dir is not None:
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
    sandbox_dir = None
    try:
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
        if sandbox_dir is not None:
            sandbox_dir.cleanup()

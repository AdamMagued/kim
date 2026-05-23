"""
Kim MCP Server — Cross-Platform OS Utilities (Phase 6)

Detects the host operating system and provides command translation so that
Windows-targeted commands from the LLM run correctly on macOS and Linux.

Supports three platforms:
  - "Windows"  (platform.system() == "Windows")
  - "Darwin"   (macOS)
  - "Linux"

Translation scope:
  1. App-launching commands (start, open -a, xdg-open)
  2. Common shell built-ins (cls ↔ clear, dir ↔ ls, etc.)
  3. Utility commands (notepad → TextEdit/xdg-open, etc.)
"""

from __future__ import annotations

import logging
import platform
import re
import shutil

logger = logging.getLogger(__name__)

# ─── OS Detection ────────────────────────────────────────────────────────────

CURRENT_OS: str = platform.system()  # "Windows", "Darwin", or "Linux"

IS_WINDOWS: bool = CURRENT_OS == "Windows"
IS_MACOS: bool = CURRENT_OS == "Darwin"
IS_LINUX: bool = CURRENT_OS == "Linux"


def get_os_info() -> dict:
    """Return a dict with current OS details for diagnostics."""
    return {
        "system": CURRENT_OS,
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "is_windows": IS_WINDOWS,
        "is_macos": IS_MACOS,
        "is_linux": IS_LINUX,
    }


# ─── App Launching Commands ─────────────────────────────────────────────────

# Windows-only executables → cross-platform alternatives
_APP_MAP_MAC: dict[str, str] = {
    "notepad": "TextEdit",
    "notepad.exe": "TextEdit",
    "calc": "Calculator",
    "calc.exe": "Calculator",
    "mspaint": "Preview",
    "mspaint.exe": "Preview",
    "explorer": "Finder",
    "explorer.exe": "Finder",
    "cmd": "Terminal",
    "cmd.exe": "Terminal",
    "powershell": "Terminal",
    "powershell.exe": "Terminal",
    "taskmgr": "Activity Monitor",
    "taskmgr.exe": "Activity Monitor",
    "control": "System Preferences",
    "control.exe": "System Preferences",
    "winword": "TextEdit",
    "winword.exe": "TextEdit",
}

_APP_MAP_LINUX: dict[str, str] = {
    "notepad": "gedit",
    "notepad.exe": "gedit",
    "calc": "gnome-calculator",
    "calc.exe": "gnome-calculator",
    "mspaint": "gimp",
    "mspaint.exe": "gimp",
    "explorer": "nautilus",
    "explorer.exe": "nautilus",
    "cmd": "x-terminal-emulator",
    "cmd.exe": "x-terminal-emulator",
    "powershell": "x-terminal-emulator",
    "powershell.exe": "x-terminal-emulator",
    "taskmgr": "gnome-system-monitor",
    "taskmgr.exe": "gnome-system-monitor",
    "control": "gnome-control-center",
    "control.exe": "gnome-control-center",
}

# Simple shell built-in translations
_BUILTIN_MAP_UNIX: dict[str, str] = {
    "cls": "clear",
    "dir": "ls -la",
    "type": "cat",
    "copy": "cp",
    "move": "mv",
    "del": "rm",
    "ren": "mv",
    "mkdir": "mkdir -p",
    "rmdir": "rm -rf",
    "tasklist": "ps aux",
    "ipconfig": "ifconfig",
    "findstr": "grep",
    "where": "which",
}

# ─── Regex patterns for command interception ─────────────────────────────────

# Matches: start notepad, start notepad.exe, start "" "some app"
_RE_START = re.compile(
    r'^start\s+(?:""?\s+)?["\']?(.+?)["\']?\s*$',
    re.IGNORECASE,
)

# Matches: notepad.exe somefile.txt  (direct exe invocation)
_RE_WIN_EXE = re.compile(
    r'^(\S+\.exe)\b(.*)$',
    re.IGNORECASE,
)


def _translate_start_command(app: str) -> str | None:
    """Translate a 'start <app>' Windows command to the current OS."""
    app_lower = app.strip().lower().rstrip('"').rstrip("'")

    if IS_MACOS:
        mac_app = _APP_MAP_MAC.get(app_lower)
        if mac_app:
            return f"open -a '{mac_app}'"
        # If it looks like a path or URL, just open it
        return f"open {app.strip()}"

    if IS_LINUX:
        linux_app = _APP_MAP_LINUX.get(app_lower)
        if linux_app:
            return linux_app
        return f"xdg-open {app.strip()}"

    return None  # Windows — no translation needed


def _translate_exe_invocation(exe: str, rest: str) -> str | None:
    """Translate a direct Windows .exe invocation to the current OS."""
    exe_lower = exe.lower()

    if IS_MACOS:
        mac_app = _APP_MAP_MAC.get(exe_lower)
        if mac_app:
            return f"open -a '{mac_app}'{rest}"
        return None  # Let it fail naturally — we can't guess every exe

    if IS_LINUX:
        linux_app = _APP_MAP_LINUX.get(exe_lower)
        if linux_app:
            return f"{linux_app}{rest}"
        return None

    return None


def _translate_builtin(cmd_parts: list[str]) -> str | None:
    """Translate a simple Windows shell built-in to its Unix equivalent."""
    if IS_WINDOWS:
        return None
    first = cmd_parts[0].lower()
    unix_equiv = _BUILTIN_MAP_UNIX.get(first)
    if unix_equiv:
        rest = " ".join(cmd_parts[1:])
        return f"{unix_equiv} {rest}".strip()
    return None


def _translate_powershell(cmd: str) -> str | None:
    """
    Translate a 'powershell ...' or 'powershell.exe ...' invocation.
    On non-Windows, we try to extract the -Command argument and run it
    through bash instead, with translated commands where possible.
    """
    if IS_WINDOWS:
        return None

    # Extract the -Command portion
    m = re.search(r'-Command\s+["\']?(.+?)(?:["\']?\s*$)', cmd, re.IGNORECASE)
    if m:
        inner = m.group(1)
        # Try to translate the inner command
        translated = translate_command(inner)
        return translated if translated != inner else inner

    return None


# ─── Public API ──────────────────────────────────────────────────────────────

def translate_command(cmd: str) -> str:
    """
    Translate a (potentially Windows-centric) shell command for the current OS.

    Returns the translated command string. If no translation is needed (either
    already native or platform is Windows), returns the original command.

    This function is safe to call on any command — it only modifies commands
    it recognizes as needing translation.
    """
    if IS_WINDOWS:
        return cmd  # No translation needed on Windows

    stripped = cmd.strip()
    if not stripped:
        return cmd

    # 1) "start <app>" → "open -a <app>" (Mac) / "xdg-open <app>" (Linux)
    m = _RE_START.match(stripped)
    if m:
        translated = _translate_start_command(m.group(1))
        if translated:
            logger.info(f"[os_utils] Translated '{stripped}' → '{translated}'")
            return translated

    # 2) "powershell -Command ..." → bash-compatible
    if stripped.lower().startswith(("powershell ", "powershell.exe ")):
        translated = _translate_powershell(stripped)
        if translated:
            logger.info(f"[os_utils] Translated PowerShell → '{translated}'")
            return translated
        # If we can't translate, warn and return a clear error command
        logger.warning(f"[os_utils] Cannot translate PowerShell command on {CURRENT_OS}")
        return f"echo 'ERROR: PowerShell is not available on {CURRENT_OS}. Please use bash/zsh instead.'"

    # 3) Direct .exe invocation → platform equivalent
    m_exe = _RE_WIN_EXE.match(stripped)
    if m_exe:
        translated = _translate_exe_invocation(m_exe.group(1), m_exe.group(2))
        if translated:
            logger.info(f"[os_utils] Translated '{stripped}' → '{translated}'")
            return translated

    # 4) Simple built-in translations (cls, dir, type, etc.)
    parts = stripped.split()
    translated = _translate_builtin(parts)
    if translated:
        logger.info(f"[os_utils] Translated '{stripped}' → '{translated}'")
        return translated

    return cmd


def get_shell_executable() -> str | None:
    """
    Return the appropriate shell executable for the current OS.
    Returns None to let asyncio.create_subprocess_shell use the default.
    On Windows this is cmd.exe; on Unix it is /bin/sh by default.
    """
    if IS_WINDOWS:
        return None  # Default (cmd.exe) is fine
    # Prefer the user's shell, but always have a fallback
    import os
    return os.environ.get("SHELL", "/bin/sh")


def check_tool_available(tool_name: str) -> bool:
    """Check if an external CLI tool is available on the PATH."""
    return shutil.which(tool_name) is not None

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
import os
import platform
import re
import shlex
import shutil

logger = logging.getLogger(__name__)

# ─── OS Detection ────────────────────────────────────────────────────────────

CURRENT_OS: str = platform.system()  # "Windows", "Darwin", or "Linux"

IS_WINDOWS: bool = CURRENT_OS == "Windows"
IS_MACOS: bool = CURRENT_OS == "Darwin"
IS_LINUX: bool = CURRENT_OS == "Linux"


# ─── Minimal subprocess environment (S4) ─────────────────────────────────────
# NO tool subprocess inherits the full parent environment: provider API keys,
# tokens, and injection vectors (LD_PRELOAD, PYTHONPATH, …) must never reach
# child processes. Children get this fixed allowlist instead — locale, paths,
# and the display plumbing GUI helpers (osascript, wmctrl, Chrome) need.
# PATH is inherited deliberately: it lists directories, not secrets, and dev
# tools (npm, cargo, brew installs) live outside the system dirs.

_ENV_ALLOWLIST_POSIX = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR",
    "LANG", "LC_ALL", "LC_CTYPE", "TERM",
    # Display plumbing for GUI-adjacent children (wmctrl, xdotool, browsers).
    "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR",
    "XDG_DATA_HOME", "XDG_CONFIG_HOME", "DBUS_SESSION_BUS_ADDRESS",
)

_ENV_ALLOWLIST_WINDOWS = (
    "Path", "PATH", "PATHEXT", "SystemRoot", "SystemDrive", "ComSpec",
    "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "ProgramFiles", "ProgramFiles(x86)", "ProgramData",
    "NUMBER_OF_PROCESSORS", "windir",
)


def minimal_subprocess_env(
    extra_keys: tuple[str, ...] = (),
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the allowlist environment every tool subprocess runs with.

    ``extra_keys`` names additional parent vars a specific tool legitimately
    needs (e.g. GH_TOKEN for the gh CLI). ``overrides`` sets fixed values.
    """
    keys = (_ENV_ALLOWLIST_WINDOWS if IS_WINDOWS else _ENV_ALLOWLIST_POSIX) + extra_keys
    env = {k: os.environ[k] for k in keys if k in os.environ}
    if overrides:
        env.update(overrides)
    return env


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
    # "del" and "rmdir" are intentionally omitted: blindly translating destructive
    # Windows verbs (del /F /Q *, rmdir /S /Q C:\) to rm / rm -rf passes Windows-
    # style flags as Unix paths and can produce dangerous commands.
    "ren": "mv",
    # "mkdir" intentionally omitted: Windows mkdir has no slash-flags, so its
    # only argument is always a path.  The only time a mkdir argument starts with
    # '/' is a Unix absolute path (e.g. `mkdir /tmp/newdir`), which must NOT be
    # rewritten to `mkdir -p`.  There is no reliable heuristic to distinguish a
    # Windows relative path from a Unix path, so we leave mkdir untranslated and
    # let the native command run unchanged on every platform.
    "tasklist": "ps aux",
    "ipconfig": "ifconfig",
    "findstr": "grep",
    "where": "which",
}

# These commands exist natively on Unix with different semantics:
#   type   — shell built-in that resolves commands, not a file-display tool
#   where  — non-standard on Unix; some distros ship it with different behaviour
# Only translate them when the invocation is clearly Windows-shaped, i.e. at
# least one argument starts with '/' (Windows flag style such as /P, /F, /Q).
#
# Note: `mkdir` was previously in this set but has been removed.  Windows mkdir
# carries no slash-flags, so the only time a mkdir argument begins with '/' is a
# Unix absolute path — the opposite of what the guard intends.  mkdir is now
# omitted from _BUILTIN_MAP_UNIX entirely; see comment there.
_WINDOWS_SHAPED_ONLY: frozenset[str] = frozenset({"type", "where"})

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
            return f"open -a {shlex.quote(mac_app)}"
        # If it looks like a path or URL, just open it (quoted to handle spaces/special chars)
        return f"open {shlex.quote(app.strip())}"

    if IS_LINUX:
        linux_app = _APP_MAP_LINUX.get(app_lower)
        if linux_app:
            return linux_app
        return f"xdg-open {shlex.quote(app.strip())}"

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
        args = cmd_parts[1:]
        # For commands that overlap with native Unix names, only translate when
        # the invocation is clearly Windows-shaped (has a /flag-style argument).
        # This prevents clobbering e.g. `type ls`, `where foo`
        # which are all valid native-Unix invocations with different semantics.
        if first in _WINDOWS_SHAPED_ONLY:
            if not any(a.startswith("/") for a in args):
                return None
        rest = " ".join(args)
        return f"{unix_equiv} {rest}".strip()
    return None


def _translate_powershell(cmd: str) -> str | None:
    """
    Translate a 'powershell ...' or 'powershell.exe ...' invocation.
    On non-Windows, we try to extract the -Command argument and run it
    through bash instead, with translated commands where possible.

    Returns None when the command cannot be reliably translated (multi-statement
    scripts, no -Command flag, or inner command not recognised).  Callers should
    treat None as an untranslatable error, NOT as a no-op.
    """
    if IS_WINDOWS:
        return None

    # Extract the -Command portion
    m = re.search(r'-Command\s+["\']?(.+?)(?:["\']?\s*$)', cmd, re.IGNORECASE)
    if not m:
        # No -Command flag — cannot safely translate (may be -File, -EncodedCommand, …)
        return None

    inner = m.group(1)

    # Refuse to blindly run multi-statement scripts; the individual statements
    # are PowerShell cmdlets that have no reliable one-to-one bash mapping.
    if any(sep in inner for sep in (";", "|", "&&", "||", "\n")):
        return None

    # Try to translate the single inner command
    translated = translate_command(inner)
    # Only return the translation when it actually changed; otherwise we'd
    # silently run an untranslated PowerShell cmdlet in bash.
    if translated != inner:
        return translated

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
        # If we can't translate, return a command that exits non-zero so callers
        # see a real failure rather than a success-looking echo exit-0.
        logger.warning(f"[os_utils] Cannot translate PowerShell command on {CURRENT_OS}")
        msg = shlex.quote(
            f"ERROR: PowerShell is not available on {CURRENT_OS}. Please use bash/zsh instead."
        )
        return f"bash -c 'echo {msg} >&2; exit 1'"

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

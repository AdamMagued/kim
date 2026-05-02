"""
Kim MCP Server — Window Management Tools (Cross-Platform)

Provides get_windows, focus_window, resize_window, and open_url with
platform-specific backends:
  - Windows:  pygetwindow (existing behaviour)
  - macOS:    osascript (AppleScript) via subprocess
  - Linux:    wmctrl / xdotool via subprocess

If the required tool (wmctrl, xdotool) is not installed on Linux, or if
a specific operation cannot be performed on the current OS, the handler
returns a clean OS_LIMITATION error message so the LLM can adapt.
"""

import asyncio
import logging
import re
import webbrowser

from mcp_server.os_utils import CURRENT_OS, IS_WINDOWS, IS_MACOS, IS_LINUX, check_tool_available

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# AppleScript escaping helper (#1 — prevent injection via window titles)
# ──────────────────────────────────────────────────────────────────────────────

def _applescript_quote(s: str) -> str:
    """Escape a string for safe interpolation into AppleScript source.

    Escapes backslashes and double-quotes, then wraps in literal "...".
    Raises ValueError if the string contains a null byte (unsupported by
    osascript and could indicate an injection attempt).
    """
    if "\x00" in s:
        raise ValueError("OS_LIMITATION: title contains null byte")
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# ──────────────────────────────────────────────────────────────────────────────
# Windows backend (pygetwindow)
# ──────────────────────────────────────────────────────────────────────────────

async def _get_windows_win() -> str:
    import pygetwindow as gw
    windows = gw.getAllWindows()
    lines = []
    for w in windows:
        if w.title.strip():
            lines.append(
                f"title={w.title!r:50s}  pos=({w.left},{w.top})  "
                f"size=({w.width}x{w.height})  visible={w.visible}"
            )
    return "\n".join(lines) if lines else "No windows found"


async def _focus_window_win(title: str) -> str:
    import pygetwindow as gw
    matches = gw.getWindowsWithTitle(title)
    if not matches:
        return f"ERROR: No window found with title containing '{title}'"
    win = matches[0]
    if win.isMinimized:
        win.restore()
    win.activate()
    return f"Focused window: {win.title!r}"


async def _resize_window_win(title: str, x: int, y: int, width: int, height: int) -> str:
    import pygetwindow as gw
    matches = gw.getWindowsWithTitle(title)
    if not matches:
        return f"ERROR: No window found with title containing '{title}'"
    win = matches[0]
    if win.isMinimized:
        win.restore()
    win.moveTo(x, y)
    win.resizeTo(width, height)
    return f"Resized '{win.title}' to ({x},{y}) {width}x{height}"


# ──────────────────────────────────────────────────────────────────────────────
# macOS backend (osascript / AppleScript)
# ──────────────────────────────────────────────────────────────────────────────

async def _run_osascript(script: str) -> tuple[int, str, str]:
    """Run an AppleScript via osascript and return (exit_code, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except asyncio.TimeoutError:
            logger.warning("osascript process did not exit after kill")
        return (1, "", "TIMEOUT: osascript exceeded 10s")
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


async def _get_windows_mac() -> str:
    # AppleScript: list all application windows with names, positions, sizes
    script = '''
        set output to ""
        tell application "System Events"
            set allProcs to (every process whose visible is true)
            repeat with proc in allProcs
                set procName to name of proc
                try
                    set wins to every window of proc
                    repeat with w in wins
                        set winName to name of w
                        set {posX, posY} to position of w
                        set {sizeW, sizeH} to size of w
                        set output to output & "title=" & quoted form of (procName & " - " & winName) & "  pos=(" & posX & "," & posY & ")  size=(" & sizeW & "x" & sizeH & ")  visible=true" & linefeed
                    end repeat
                end try
            end repeat
        end tell
        return output
    '''
    exit_code, out, err = await _run_osascript(script)
    if exit_code != 0:
        return f"ERROR: osascript failed: {err}"
    return out if out.strip() else "No windows found"


async def _focus_window_mac(title: str) -> str:
    try:
        safe_title = _applescript_quote(title)
    except ValueError as e:
        return f"ERROR: {e}"
    # Use AppleScript 'contains' with safely escaped string
    script = f'''
        tell application "System Events"
            set allProcs to (every process whose visible is true)
            repeat with proc in allProcs
                try
                    set wins to every window of proc whose name contains {safe_title}
                    if (count of wins) > 0 then
                        set frontmost of proc to true
                        perform action "AXRaise" of item 1 of wins
                        return "Focused: " & name of proc & " - " & name of item 1 of wins
                    end if
                end try
            end repeat
        end tell
        return "ERROR: No window found with title containing " & {safe_title}
    '''
    exit_code, out, err = await _run_osascript(script)
    if exit_code != 0:
        return f"ERROR: osascript failed: {err}"
    return out


async def _resize_window_mac(title: str, x: int, y: int, width: int, height: int) -> str:
    try:
        safe_title = _applescript_quote(title)
    except ValueError as e:
        return f"ERROR: {e}"
    script = f'''
        tell application "System Events"
            set allProcs to (every process whose visible is true)
            repeat with proc in allProcs
                try
                    set wins to every window of proc whose name contains {safe_title}
                    if (count of wins) > 0 then
                        set w to item 1 of wins
                        set position of w to {{{x}, {y}}}
                        set size of w to {{{width}, {height}}}
                        return "Resized: " & name of proc & " - " & name of w & " to (" & {x} & "," & {y} & ") " & {width} & "x" & {height}
                    end if
                end try
            end repeat
        end tell
        return "ERROR: No window found with title containing " & {safe_title}
    '''
    exit_code, out, err = await _run_osascript(script)
    if exit_code != 0:
        return f"ERROR: osascript failed: {err}"
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Linux backend (wmctrl / xdotool)
# ──────────────────────────────────────────────────────────────────────────────

async def _run_cmd(cmd: list[str]) -> tuple[int, str, str]:
    """Run a CLI command and return (exit_code, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


async def _get_windows_linux() -> str:
    if not check_tool_available("wmctrl"):
        return (
            "OS_LIMITATION: 'wmctrl' is not installed on this Linux system. "
            "Install it with 'sudo apt install wmctrl' (Debian/Ubuntu) or "
            "'sudo dnf install wmctrl' (Fedora). Alternatively, use the "
            "run_command tool with 'xdotool search --name \"\"' to list windows."
        )

    exit_code, out, err = await _run_cmd(["wmctrl", "-l", "-G"])
    if exit_code != 0:
        return f"ERROR: wmctrl failed: {err}"

    lines = []
    for line in out.splitlines():
        # wmctrl -l -G format:
        # 0x01c00003  0 0    0    1920 1080 hostname Desktop
        parts = line.split(None, 7)
        if len(parts) >= 8:
            wid, desktop, x, y, w, h, host, title = parts
            lines.append(
                f"title={title!r:50s}  pos=({x},{y})  size=({w}x{h})  visible=true"
            )
    return "\n".join(lines) if lines else "No windows found"


async def _focus_window_linux(title: str) -> str:
    # Try wmctrl first, fall back to xdotool
    if check_tool_available("wmctrl"):
        exit_code, out, err = await _run_cmd(["wmctrl", "-a", title])
        if exit_code == 0:
            return f"Focused window matching: '{title}'"
        return f"ERROR: wmctrl could not find window matching '{title}': {err}"

    if check_tool_available("xdotool"):
        exit_code, wid, err = await _run_cmd(
            ["xdotool", "search", "--name", title]
        )
        if exit_code != 0 or not wid.strip():
            return f"ERROR: xdotool could not find window matching '{title}': {err}"
        # Take the first matching window ID
        first_wid = wid.strip().splitlines()[0]
        exit_code2, _, err2 = await _run_cmd(
            ["xdotool", "windowactivate", first_wid]
        )
        if exit_code2 == 0:
            return f"Focused window ID {first_wid} matching: '{title}'"
        return f"ERROR: xdotool windowactivate failed: {err2}"

    return (
        "OS_LIMITATION: Neither 'wmctrl' nor 'xdotool' is installed on this "
        "Linux system. Install one with 'sudo apt install wmctrl' or "
        "'sudo apt install xdotool'. Without these, window management "
        "is not available on Linux."
    )


async def _resize_window_linux(title: str, x: int, y: int, width: int, height: int) -> str:
    if check_tool_available("wmctrl"):
        # wmctrl -r <title> -e gravity,x,y,width,height
        exit_code, out, err = await _run_cmd(
            ["wmctrl", "-r", title, "-e", f"0,{x},{y},{width},{height}"]
        )
        if exit_code == 0:
            return f"Resized window '{title}' to ({x},{y}) {width}x{height}"
        return f"ERROR: wmctrl resize failed: {err}"

    if check_tool_available("xdotool"):
        # Find the window first
        exit_code, wid, err = await _run_cmd(
            ["xdotool", "search", "--name", title]
        )
        if exit_code != 0 or not wid.strip():
            return f"ERROR: xdotool could not find window matching '{title}': {err}"
        first_wid = wid.strip().splitlines()[0]

        # Move and resize
        exit_code2, _, err2 = await _run_cmd(
            ["xdotool", "windowmove", first_wid, str(x), str(y)]
        )
        exit_code3, _, err3 = await _run_cmd(
            ["xdotool", "windowsize", first_wid, str(width), str(height)]
        )
        if exit_code2 == 0 and exit_code3 == 0:
            return f"Resized window '{title}' (ID {first_wid}) to ({x},{y}) {width}x{height}"
        return f"ERROR: xdotool move/resize failed: {err2 or err3}"

    return (
        "OS_LIMITATION: Neither 'wmctrl' nor 'xdotool' is installed on this "
        "Linux system. Install one with 'sudo apt install wmctrl' or "
        "'sudo apt install xdotool'. Without these, window resize "
        "is not available on Linux."
    )


# ──────────────────────────────────────────────────────────────────────────────
# Public handlers (dispatch to platform backend)
# ──────────────────────────────────────────────────────────────────────────────

async def handle_get_windows(args: dict) -> str:
    try:
        if IS_WINDOWS:
            result = await _get_windows_win()
        elif IS_MACOS:
            result = await _get_windows_mac()
        elif IS_LINUX:
            result = await _get_windows_linux()
        else:
            result = f"OS_LIMITATION: Window listing is not supported on {CURRENT_OS}."
        logger.info(f"get_windows [{CURRENT_OS}]: returned {len(result)} chars")
        return result
    except ImportError as e:
        # pygetwindow not installed on Windows
        return (
            f"OS_LIMITATION: Required library not installed: {e}. "
            f"On Windows, install pygetwindow: pip install pygetwindow"
        )
    except Exception as e:
        logger.error(f"get_windows failed: {e}", exc_info=True)
        return f"ERROR: {e}"


async def handle_focus_window(args: dict) -> str:
    title = str(args["title"])
    try:
        if IS_WINDOWS:
            result = await _focus_window_win(title)
        elif IS_MACOS:
            result = await _focus_window_mac(title)
        elif IS_LINUX:
            result = await _focus_window_linux(title)
        else:
            result = f"OS_LIMITATION: Window focus is not supported on {CURRENT_OS}."
        logger.info(f"focus_window [{CURRENT_OS}]: {result}")
        return result
    except ImportError as e:
        return (
            f"OS_LIMITATION: Required library not installed: {e}. "
            f"On Windows, install pygetwindow: pip install pygetwindow"
        )
    except Exception as e:
        logger.error(f"focus_window failed: {e}", exc_info=True)
        return f"ERROR: {e}"


async def handle_resize_window(args: dict) -> str:
    title = str(args["title"])
    x = int(args.get("x", 0))
    y = int(args.get("y", 0))
    width = int(args.get("width", 800))
    height = int(args.get("height", 600))
    try:
        if IS_WINDOWS:
            result = await _resize_window_win(title, x, y, width, height)
        elif IS_MACOS:
            result = await _resize_window_mac(title, x, y, width, height)
        elif IS_LINUX:
            result = await _resize_window_linux(title, x, y, width, height)
        else:
            result = f"OS_LIMITATION: Window resize is not supported on {CURRENT_OS}."
        logger.info(f"resize_window [{CURRENT_OS}]: {result}")
        return result
    except ImportError as e:
        return (
            f"OS_LIMITATION: Required library not installed: {e}. "
            f"On Windows, install pygetwindow: pip install pygetwindow"
        )
    except Exception as e:
        logger.error(f"resize_window failed: {e}", exc_info=True)
        return f"ERROR: {e}"


async def handle_open_url(args: dict) -> str:
    url = str(args["url"])
    try:
        webbrowser.open(url)
        logger.info(f"open_url: {url}")
        return f"Opened URL in default browser: {url}"
    except Exception as e:
        logger.error(f"open_url failed: {e}", exc_info=True)
        return f"ERROR: {e}"

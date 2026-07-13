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
import urllib.parse
import webbrowser

from mcp_server.os_utils import (
    CURRENT_OS,
    IS_WINDOWS,
    IS_MACOS,
    IS_LINUX,
    check_tool_available,
    minimal_subprocess_env,
)
from mcp_server.tools._errors import tool_error

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
    windows = gw.getAllWindows()  # type: ignore[attr-defined]  # Windows-only pygetwindow API
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
    matches = gw.getWindowsWithTitle(title)  # type: ignore[attr-defined]  # Windows-only pygetwindow API
    if not matches:
        return tool_error(f"No window found with title containing '{title}'")
    win = matches[0]
    if win.isMinimized:
        win.restore()
    win.activate()
    return f"Focused window: {win.title!r}"


async def _resize_window_win(title: str, x: int, y: int, width: int, height: int) -> str:
    import pygetwindow as gw
    matches = gw.getWindowsWithTitle(title)  # type: ignore[attr-defined]  # Windows-only pygetwindow API
    if not matches:
        return tool_error(f"No window found with title containing '{title}'")
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
        env=minimal_subprocess_env(),  # S4: no parent-env inherit
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


# AppleScript: list all application windows with names, positions, sizes.
# Per-window visibility is queried via the AXMinimized attribute (2.4) instead
# of hardcoding visible=true for every window: a minimized window must not be
# reported as visible, or the LLM believes content is on screen when it isn't.
_MAC_LIST_WINDOWS_SCRIPT = '''
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
                    set visFlag to "true"
                    try
                        if (value of attribute "AXMinimized" of w) is true then
                            set visFlag to "false"
                        end if
                    end try
                    set output to output & "title=" & quoted form of (procName & " - " & winName) & "  pos=(" & posX & "," & posY & ")  size=(" & sizeW & "x" & sizeH & ")  visible=" & visFlag & linefeed
                end repeat
            end try
        end repeat
    end tell
    return output
'''


async def _get_windows_mac() -> str:
    exit_code, out, err = await _run_osascript(_MAC_LIST_WINDOWS_SCRIPT)
    if exit_code != 0:
        return tool_error(f"osascript failed: {err}")
    return out if out.strip() else "No windows found"


async def _focus_window_mac(title: str) -> str:
    try:
        safe_title = _applescript_quote(title)
    except ValueError as e:
        return tool_error(e)
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
        return tool_error(f"osascript failed: {err}")
    return out


async def _resize_window_mac(title: str, x: int, y: int, width: int, height: int) -> str:
    try:
        safe_title = _applescript_quote(title)
    except ValueError as e:
        return tool_error(e)
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
        return tool_error(f"osascript failed: {err}")
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
        env=minimal_subprocess_env(),  # S4: no parent-env inherit
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    except (asyncio.TimeoutError, TimeoutError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise
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
        return tool_error(f"wmctrl failed: {err}")

    lines = []
    for line in out.splitlines():
        # wmctrl -l -G format:
        # 0x01c00003  0 0    0    1920 1080 hostname Desktop
        parts = line.split(None, 7)
        if len(parts) >= 8:
            wid, desktop, x, y, w, h, host, title = parts
            # 2.4: wmctrl -l cannot distinguish minimized/hidden windows, so
            # do NOT fabricate visible=true — report the honest unknown.
            lines.append(
                f"title={title!r:50s}  pos=({x},{y})  size=({w}x{h})  "
                f"desktop={desktop}  visible=unknown"
            )
    return "\n".join(lines) if lines else "No windows found"


async def _focus_window_linux(title: str) -> str:
    # Try wmctrl first, fall back to xdotool
    if check_tool_available("wmctrl"):
        exit_code, out, err = await _run_cmd(["wmctrl", "-a", title])
        if exit_code == 0:
            return f"Focused window matching: '{title}'"
        return tool_error(f"wmctrl could not find window matching '{title}': {err}")

    if check_tool_available("xdotool"):
        exit_code, wid, err = await _run_cmd(
            ["xdotool", "search", "--name", title]
        )
        if exit_code != 0 or not wid.strip():
            return tool_error(f"xdotool could not find window matching '{title}': {err}")
        # Take the first matching window ID
        first_wid = wid.strip().splitlines()[0]
        exit_code2, _, err2 = await _run_cmd(
            ["xdotool", "windowactivate", first_wid]
        )
        if exit_code2 == 0:
            return f"Focused window ID {first_wid} matching: '{title}'"
        return tool_error(f"xdotool windowactivate failed: {err2}")

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
        return tool_error(f"wmctrl resize failed: {err}")

    if check_tool_available("xdotool"):
        # Find the window first
        exit_code, wid, err = await _run_cmd(
            ["xdotool", "search", "--name", title]
        )
        if exit_code != 0 or not wid.strip():
            return tool_error(f"xdotool could not find window matching '{title}': {err}")
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
        return tool_error(f"xdotool move/resize failed: {err2 or err3}")

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
        return tool_error(e)


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
        return tool_error(e)


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
        return tool_error(e)


async def _open_url_linux(url: str) -> str:
    """Open a URL on Linux via xdg-open with a real exit-code check (1.5).

    webbrowser.open() on Linux can silently fail (returns True even when the
    spawned handler dies, e.g. headless or stripped env), so shell out to
    xdg-open directly and surface a non-zero exit.
    """
    if not check_tool_available("xdg-open"):
        # Fall back to webbrowser, but check its return value.
        if webbrowser.open(url):
            return f"Opened URL in default browser: {url}"
        return tool_error(
            "Could not open URL: 'xdg-open' is not installed and no "
            "usable browser was found (headless session?)."
        )
    try:
        exit_code, out, err = await _run_cmd(["xdg-open", url])
    except (asyncio.TimeoutError, TimeoutError):
        # Some handlers keep xdg-open in the foreground; a timeout here means
        # the handler was launched and is still running — not a failure.
        return f"Opened URL (handler still running after 10s): {url}"
    if exit_code != 0:
        return tool_error(
            f"xdg-open exited with code {exit_code} for {url}: "
            f"{err or out or 'no browser/handler available (headless session?)'}"
        )
    return f"Opened URL in default browser: {url}"


async def handle_open_url(args: dict) -> str:
    url = str(args["url"])
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return tool_error(f"URL scheme '{parsed.scheme}' is not allowed; only http and https are permitted")
    try:
        if IS_LINUX:
            result = await _open_url_linux(url)
            logger.info(f"open_url [{CURRENT_OS}]: {result}")
            return result
        # webbrowser.open returns False when no browser could be launched —
        # report that instead of claiming success (1.5).
        if not webbrowser.open(url):
            return tool_error(f"No usable browser found to open {url}")
        logger.info(f"open_url: {url}")
        return f"Opened URL in default browser: {url}"
    except Exception as e:
        logger.error(f"open_url failed: {e}", exc_info=True)
        return tool_error(e)

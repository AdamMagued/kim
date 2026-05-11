"""
Fast structured UI observation tools.

The goal is to make ordinary desktop workflows text/structure-first instead
of screenshot-first. On macOS this uses the Accessibility tree via
System Events. Other platforms return a clean limitation for now.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from mcp_server.os_utils import CURRENT_OS, IS_MACOS

logger = logging.getLogger(__name__)


@dataclass
class UIElement:
    element_id: str
    role: str
    name: str
    description: str
    value: str
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + max(1, self.width) // 2, self.y + max(1, self.height) // 2)


_LAST_ELEMENTS: dict[str, UIElement] = {}


async def _run_osascript(script: str, timeout: float = 15.0) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass
        return (1, "", "TIMEOUT: UI observation exceeded timeout")
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


def _clean(text: str, max_len: int = 80) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) > max_len:
        return text[: max_len - 1].rstrip() + "…"
    return text


def _parse_int(raw: str) -> int:
    try:
        return int(float(raw))
    except Exception:
        return 0


def _parse_elements(raw: str) -> tuple[str, str, list[UIElement]]:
    lines = [line for line in raw.splitlines() if line.strip()]
    if not lines:
        return ("Unknown", "Unknown", [])

    header = lines[0].split("\t")
    app = header[1] if len(header) > 1 and header[0] == "APP" else "Unknown"
    window = header[2] if len(header) > 2 and header[0] == "APP" else "Unknown"

    elements: list[UIElement] = []
    seen: set[tuple[str, str, int, int, int, int]] = set()
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) < 9 or parts[0] != "EL":
            continue
        role = _clean(parts[2], 48)
        name = _clean(parts[3])
        desc = _clean(parts[4])
        value = _clean(parts[5])
        x, y, w, h = map(_parse_int, parts[6:10])
        if w <= 0 or h <= 0:
            continue
        key = (role, name or desc or value, x, y, w, h)
        if key in seen:
            continue
        seen.add(key)
        element_id = f"e{len(elements) + 1}"
        elements.append(UIElement(element_id, role, name, desc, value, x, y, w, h))
    return (app, window, elements)


def _format_empty_observation(app: str) -> str:
    """Format the message when no elements are found, handling browser specific instructions."""
    is_browser = any(b in app.lower() for b in ("chrome", "safari", "firefox", "edge", "arc", "brave"))
    if is_browser:
        return (
            "- No web controls visible YET. Chrome/Safari lazy-enable AX "
            "after the first query — the second or third observe_ui call "
            "usually returns the form fields. Retry observe_ui ONCE more "
            "with depth=4. If still empty, the page may be using "
            "non-standard widgets; only then fall back to "
            "take_annotated_screenshot."
        )
    return (
        "- No interactive controls at this depth. The window may be "
        "Electron-based (no AX). Try focus_window on a different app "
        "or take_annotated_screenshot for visual tasks. Do not loop "
        "observe_ui on the same window."
    )


def _sort_elements(elements: list[UIElement]) -> list[UIElement]:
    """Sort elements by role priority, then by position (y, x), then ID."""
    role_order = {
        "AXTextField": 0,
        "AXTextArea": 0,
        "AXSearchField": 0,
        "AXButton": 1,
        "AXCheckBox": 1,
        "AXRadioButton": 1,
        "AXPopUpButton": 1,
        "AXMenuButton": 1,
        "AXLink": 2,
        "AXStaticText": 3,
    }
    return sorted(
        elements,
        key=lambda el: (role_order.get(el.role, 9), el.y, el.x, el.element_id),
    )


def _format_element(el: UIElement) -> str:
    """Format a single UIElement into its string representation."""
    label = el.name or el.description or el.value or "(unlabeled)"
    cx, cy = el.center
    extra = f" value={el.value!r}" if el.value and el.value != label else ""
    return (
        f"- {el.element_id}: {el.role} {label!r} "
        f"center=({cx},{cy}) bounds=({el.x},{el.y},{el.width},{el.height}){extra}"
    )


def _format_observation(app: str, window: str, elements: list[UIElement], limit: int) -> str:
    global _LAST_ELEMENTS
    _LAST_ELEMENTS = {el.element_id: el for el in elements[:limit]}

    lines = [
        "STRUCTURED_UI_OBSERVATION",
        f"Active app: {app}",
        f"Active window: {window}",
        "Use click_ui(element_id) for visible controls. Use type_text after focusing an input.",
        "",
        "Elements:",
    ]

    if not elements:
        lines.append(_format_empty_observation(app))
        return "\n".join(lines)

    sorted_elements = _sort_elements(elements)[:limit]

    for el in sorted_elements:
        lines.append(_format_element(el))

    if len(elements) > limit:
        lines.append(
            f"... {len(elements) - limit} more elements omitted. "
            "Re-run observe_ui with a higher limit if needed."
        )

    return "\n".join(lines)


async def _observe_ui_macos(limit: int, depth: int) -> str:
    preflight = '''
        tell application "System Events"
            set frontProc to first application process whose frontmost is true
            try
                set frontWin to front window of frontProc
                return role of frontWin
            on error errMsg number errNum
                return "ERROR:" & errNum & ":" & errMsg
            end try
        end tell
    '''
    _, preflight_out, preflight_err = await _run_osascript(preflight, timeout=1.5)
    if "-1719" in preflight_out or "-1719" in preflight_err:
        return (
            "PERMISSION_ERROR: macOS Accessibility access is required for observe_ui. "
            "Open System Settings → Privacy & Security → Accessibility and add the "
            "process running Kim. NEED_HELP and ask the user to grant it."
        )

    # AppleScript returns tab-separated rows to avoid fragile JSON escaping.
    script = f'''
        set maxDepth to {max(1, min(depth, 8))}
        set maxItems to {max(20, min(limit * 3, 500))}
        set itemCount to 0
        set output to ""

        on cleanText(v)
            try
                set t to v as text
            on error
                set t to ""
            end try
            set AppleScript's text item delimiters to tab
            set t to text items of t as text
            set AppleScript's text item delimiters to linefeed
            set t to text items of t as text
            set AppleScript's text item delimiters to ""
            return t
        end cleanText

        on emitElement(el, depth)
            global output, itemCount, maxDepth, maxItems
            if itemCount > maxItems then return
            -- All property reads MUST be wrapped in `tell application "System
            -- Events"` because this handler runs outside the top-level tell
            -- block. Without the wrap, every read errors silently and we
            -- mis-report "No accessible controls found".
            try
                tell application "System Events" to set r to role of el as text
            on error
                set r to ""
            end try
            try
                tell application "System Events" to set rawN to name of el
                set n to my cleanText(rawN)
            on error
                set n to ""
            end try
            try
                tell application "System Events" to set rawD to description of el
                set d to my cleanText(rawD)
            on error
                set d to ""
            end try
            try
                tell application "System Events" to set rawV to value of el
                set v to my cleanText(rawV)
            on error
                set v to ""
            end try
            try
                tell application "System Events"
                    set {{px, py}} to position of el
                    set {{sx, sy}} to size of el
                end tell
            on error
                set px to 0
                set py to 0
                set sx to 0
                set sy to 0
            end try

            if (r is not "" and sx > 0 and sy > 0) then
                set interesting to false
                if r contains "Button" then set interesting to true
                if r contains "Text" then set interesting to true
                if r contains "Search" then set interesting to true
                if r contains "Field" then set interesting to true
                if r contains "Link" then set interesting to true
                if r contains "Menu" then set interesting to true
                if r contains "Check" then set interesting to true
                if r contains "Radio" then set interesting to true
                if interesting then
                    set itemCount to itemCount + 1
                    set output to output & "EL" & tab & depth & tab & r & tab & n \
                        & tab & d & tab & v & tab & px & tab & py & tab & sx & tab & sy & linefeed
                end if
            end if

            if depth is not less than maxDepth then return
            try
                tell application "System Events" to set childItems to UI elements of el
                repeat with child in childItems
                    my emitElement(child, depth + 1)
                    if itemCount > maxItems then exit repeat
                end repeat
            end try
        end emitElement

        tell application "System Events"
            set frontProc to first application process whose frontmost is true
            set procName to name of frontProc as text
            try
                set frontWin to front window of frontProc
                set winName to name of frontWin as text
            on error
                set frontWin to frontProc
                set winName to ""
            end try
            set output to "APP" & tab & procName & tab & winName & linefeed
            my emitElement(frontWin, 0)
        end tell
        return output
    '''
    code, out, err = await _run_osascript(script)
    if code != 0:
        return f"ERROR: macOS Accessibility observation failed: {err or out}"
    app, window, elements = _parse_elements(out)
    return _format_observation(app, window, elements, limit)


async def handle_observe_ui(args: dict) -> str:
    """Return a compact structured view of the active app/window."""
    limit = int(args.get("limit", 80))
    # Cap depth aggressively. AppleScript AX traversal is O(n^d) and the
    # `tell application "System Events"` round-trip per property read makes
    # depth>4 impractical on rich apps like Finder/Xcode (>15s).
    depth = min(int(args.get("depth", 4)), 5)
    if IS_MACOS:
        result = await _observe_ui_macos(limit=limit, depth=depth)
        logger.info(f"observe_ui [{CURRENT_OS}]: returned {len(result)} chars")
        return result
    return (
        f"OS_LIMITATION: observe_ui is not implemented for {CURRENT_OS} yet. "
        "Use get_windows, focus_window, keyboard shortcuts, or screenshot vision only for visual tasks."
    )


async def handle_click_ui(args: dict) -> str:
    """Click a UI element returned by the most recent observe_ui call."""
    import pyautogui

    element_id = str(args.get("element_id") or "").strip()
    if not element_id:
        return "ERROR: element_id is required. Call observe_ui first."
    el = _LAST_ELEMENTS.get(element_id)
    if not el:
        return f"ERROR: Unknown element_id {element_id!r}. Call observe_ui again and use one of its IDs."
    x, y = el.center
    button = str(args.get("button") or "left")
    clicks = int(args.get("clicks", 1))
    pyautogui.click(x=x, y=y, button=button, clicks=clicks, interval=0.08)
    logger.info(f"click_ui: {element_id} ({x},{y}) button={button} clicks={clicks}")
    return f"Clicked {element_id} ({el.role} {el.name or el.description or el.value!r}) at ({x},{y})"

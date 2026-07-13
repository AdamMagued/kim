"""
MCP tool definitions and dispatch map.

All Tool schemas and their handler mappings live here. server.py imports
TOOLS and DISPATCH to wire the MCP protocol handlers.

Grouped by domain:
  - File operations
  - Shell execution
  - Screen / UI observation
  - Web browser automation
  - Mouse input
  - Keyboard input
  - Window management
  - Git operations
  - Code execution
  - Search
"""

from mcp.types import Tool

from mcp_server.tools.code import handle_lint_file, handle_run_node, handle_run_python
from mcp_server.tools.files import (
    handle_delete_file,
    handle_edit_file,
    handle_list_dir,
    handle_read_file,
    handle_view_image,
    handle_write_file,
)
from mcp_server.tools.git import (
    handle_git_add,
    handle_git_checkout,
    handle_git_commit,
    handle_git_diff,
    handle_git_log,
    handle_git_status,
)
from mcp_server.tools.github import handle_github_create_repo
from mcp_server.tools.keyboard import handle_hotkey, handle_key_press, handle_type_text
from mcp_server.tools.mouse import (
    handle_click,
    handle_double_click,
    handle_drag,
    handle_right_click,
    handle_scroll,
)
from mcp_server.tools.screen import (
    handle_get_screen_info,
    handle_take_annotated_screenshot,
    handle_take_screenshot,
)
from mcp_server.tools.memory import handle_read_memory, handle_write_memory
from mcp_server.tools.search import handle_find_files, handle_search_in_files
from mcp_server.tools.shell import handle_run_command, handle_run_powershell
from mcp_server.tools.ui_observe import handle_click_ui, handle_observe_ui
from mcp_server.tools.web import (
    handle_web_back,
    handle_web_click,
    handle_web_close,
    handle_web_fill,
    handle_web_fill_form,
    handle_web_observe,
    handle_web_open,
    handle_web_press,
    handle_web_resolve,
    handle_web_screenshot,
    handle_web_text,
    handle_web_wait_for,
    handle_web_wait_for_url,
)
from mcp_server.tools.windows import (
    handle_focus_window,
    handle_get_windows,
    handle_open_url,
    handle_resize_window,
)


# ── File operations ──────────────────────────────────────────────────────────

_FILE_TOOLS: list[Tool] = [
    Tool(
        name="read_file",
        description=(
            "Read the text content of a file. Path can be absolute or relative to "
            "PROJECT_ROOT. By default returns the whole file verbatim. Pass offset "
            "(1-based line number) and/or limit (number of lines) to read a window "
            "of a large file instead: the result is then line-numbered, one line "
            "per row in the format '<n>→<line text>', with a footer noting the "
            "window and total line count when truncated (e.g. 'showing lines "
            "120-180 of 2368 total lines'). Line numbers from a windowed read let "
            "you target a follow-up edit_file call precisely."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (absolute or relative to PROJECT_ROOT)"},
                "offset": {"type": "integer", "description": "Optional 1-based line number to start reading from (default 1). When offset or limit is given, output is line-numbered as '<n>→<line>'."},
                "limit": {"type": "integer", "description": "Optional maximum number of lines to return (default: all remaining lines)."},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="write_file",
        description="Write text content to a file, creating parent directories if needed. Overwrites existing content. To write binary, set binary=true and pass content as a 'data:<mediatype>;base64,<data>' URI.",
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (absolute or relative to PROJECT_ROOT)"},
                "content": {"type": "string", "description": "Text content to write, or a 'data:<mediatype>;base64,<data>' URI when binary=true"},
                "binary": {"type": "boolean", "description": "When true, decode content as a data:...;base64,... URI and write raw bytes. Default false (write as text)."},
            },
            "required": ["path", "content"],
        },
    ),
    Tool(
        name="edit_file",
        description=(
            "Make a surgical, str-replace edit to an existing file without rewriting "
            "the whole thing. Give the EXACT text to find (old_string) and what to "
            "replace it with (new_string). old_string must match the file's current "
            "content byte-for-byte (including whitespace/indentation) — copy it "
            "verbatim from a prior read_file, don't retype it from memory. "
            "old_string MUST be unique in the file by default: if it matches more "
            "than once, the call is rejected so you don't accidentally edit the wrong "
            "occurrence — either add a few more lines of surrounding context to "
            "old_string to make it unique, or pass replace_all=true to intentionally "
            "replace every occurrence (e.g. renaming a variable). If you already know "
            "how many occurrences exist, pass expected_occurrences as a self-check: "
            "the edit only applies if the actual count matches, otherwise it errors "
            "instead of silently editing the wrong number of places. NOTE: when a "
            "matched expected_occurrences is greater than 1, ALL matched occurrences "
            "are replaced (same effect as replace_all). This is much "
            "cheaper than write_file for small changes because you don't have to "
            "reproduce the entire file content. Not for creating new files (use "
            "write_file for that) — old_string must already exist in the file."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (absolute or relative to PROJECT_ROOT)"},
                "old_string": {"type": "string", "description": "Exact text to find in the file. Must match verbatim (whitespace included) and must not be empty."},
                "new_string": {"type": "string", "description": "Text to replace old_string with. Must differ from old_string."},
                "replace_all": {"type": "boolean", "description": "Replace every occurrence of old_string instead of requiring exactly one match. Default false.", "default": False},
                "expected_occurrences": {"type": "integer", "description": "Optional self-check: the exact number of occurrences of old_string you expect. If the actual count differs, the edit is rejected instead of applied. When set and matched, satisfies the uniqueness requirement — and if greater than 1, ALL matched occurrences are replaced (same effect as replace_all)."},
            },
            "required": ["path", "old_string", "new_string"],
        },
    ),
    Tool(
        name="view_image",
        description=(
            "View an image file from disk. Returns the image itself (as a "
            "data:<mime>;base64 payload, same shape as take_screenshot) so you can "
            "look at its actual visual content — screenshots saved earlier, design "
            "mockups, photos, charts. Supported extensions: .png, .jpg, .jpeg, "
            ".gif, .webp, .bmp. Files over 10 MB or 25 megapixels are rejected. "
            "Use this instead of read_file for image files — read_file returns "
            "unreadable binary text for images."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Image file path (absolute or relative to PROJECT_ROOT)"},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="list_dir",
        description="List files and directories inside a directory.",
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path (defaults to PROJECT_ROOT)"},
                "recursive": {"type": "boolean", "description": "Recurse into subdirectories", "default": False},
            },
        },
    ),
    Tool(
        name="delete_file",
        description="Delete a single file. Does NOT delete directories.",
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to delete"},
            },
            "required": ["path"],
        },
    ),
]

_FILE_READ_DISPATCH = {
    "read_file": handle_read_file,
    "list_dir": handle_list_dir,
    "view_image": handle_view_image,
}

_FILE_WRITE_DISPATCH = {
    "write_file": handle_write_file,
    "delete_file": handle_delete_file,
    "edit_file": handle_edit_file,
}

# Merged for DISPATCH aggregation – preserves existing behavior when no tier
# filter is active.
_FILE_DISPATCH = {**_FILE_READ_DISPATCH, **_FILE_WRITE_DISPATCH}


# ── Shell execution ──────────────────────────────────────────────────────────

_SHELL_TOOLS: list[Tool] = [
    Tool(
        name="run_command",
        description="Run a shell command and return stdout, stderr, and exit code.",
        inputSchema={
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "Shell command to execute"},
                "cwd": {"type": "string", "description": "Working directory (defaults to PROJECT_ROOT)"},
                "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 30},
            },
            "required": ["cmd"],
            # additionalProperties:false prevents the model from injecting
            # undeclared keys such as sandbox_mode or allow_chaining, which
            # could otherwise disable security controls (finding 2).
            "additionalProperties": False,
        },
    ),
    Tool(
        name="run_powershell",
        description="Run a PowerShell script block and return stdout, stderr, and exit code.",
        inputSchema={
            "type": "object",
            "properties": {
                "script": {"type": "string", "description": "PowerShell script to execute"},
                "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 30},
            },
            "required": ["script"],
            # additionalProperties:false prevents sandbox_mode injection (finding 2).
            "additionalProperties": False,
        },
    ),
]

_SHELL_DISPATCH = {
    "run_command": handle_run_command,
    "run_powershell": handle_run_powershell,
}


# ── Screen / UI observation ──────────────────────────────────────────────────

_SCREEN_TOOLS: list[Tool] = [
    Tool(
        name="take_screenshot",
        description=(
            "Capture the screen as a base64-encoded PNG. Use only for genuinely visual "
            "inspection tasks or when observe_ui cannot expose the needed UI state."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "scale": {"type": "number", "description": "Scale factor (0.0–1.0, default 0.75)", "default": 0.75},
                "monitor": {"type": "integer", "description": "Monitor index (1 = primary)", "default": 1},
            },
        },
    ),
    Tool(
        name="get_screen_info",
        description="Get screen resolution, DPI, and monitor layout.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="observe_ui",
        description=(
            "Fast text-only UI observation of the active app/window using the accessibility tree. "
            "Use this BEFORE screenshots for normal desktop tasks like opening apps, reading buttons, "
            "finding inputs, and navigating email/browser UI. Returns element IDs, roles, labels, "
            "bounds, and click centers. Does not capture pixels."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Maximum elements to return", "default": 80},
                "depth": {"type": "integer", "description": "Accessibility traversal depth", "default": 5},
            },
        },
    ),
    Tool(
        name="click_ui",
        description=(
            "Click an element by ID from the most recent observe_ui result. "
            "Use this for accessible buttons, links, menu items, and inputs instead of screenshot coordinates."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "element_id": {"type": "string", "description": "Element ID from observe_ui, e.g. e12"},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                "clicks": {"type": "integer", "description": "Number of clicks", "default": 1},
            },
            "required": ["element_id"],
        },
    ),
    Tool(
        name="take_annotated_screenshot",
        description=(
            "Capture the screen with a visual ruler grid overlaid on the image. "
            "The grid has labeled cross-markers (columns A-J, rows 1-10) that you can "
            "use as reference points to calculate exact (X, Y) pixel coordinates for clicking. "
            "Returns JSON with the annotated image (base64), a grid mapping of marker labels "
            "to real screen coordinates, and instructions on how to interpolate coordinates. "
            "Use only as a fallback when observe_ui/click_ui cannot identify the target."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "scale": {"type": "number", "description": "Scale factor (0.0–1.0, default 0.75)", "default": 0.75},
                "monitor": {"type": "integer", "description": "Monitor index (1 = primary)", "default": 1},
                "grid_cols": {"type": "integer", "description": "Number of grid columns (default 10)", "default": 10},
                "grid_rows": {"type": "integer", "description": "Number of grid rows (default 10)", "default": 10},
            },
        },
    ),
]

_SCREEN_DISPATCH = {
    "take_screenshot": handle_take_screenshot,
    "get_screen_info": handle_get_screen_info,
    "observe_ui": handle_observe_ui,
    "click_ui": handle_click_ui,
    "take_annotated_screenshot": handle_take_annotated_screenshot,
}


# ── Web browser automation ───────────────────────────────────────────────────

_WEB_TOOLS: list[Tool] = [
    Tool(
        name="web_open",
        description=(
            "Navigate Kim's controlled browser to a URL. "
            "Use 'username' and 'password' arguments if the site requires a login popup (Basic Auth). "
            "Returns AUTH_REQUIRED/AUTH_FAILED instead of success when page content is blocked."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The destination URL."},
                "username": {"type": "string", "description": "Optional: username for login popups."},
                "password": {"type": "string", "description": "Optional: password for login popups."},
            },
            "required": ["url"],
        },
    ),
    Tool(
        name="web_observe",
        description=(
            "Return a structured list of every visible interactive element on the current "
            "web page (buttons, links, inputs, textareas, selects, ARIA widgets) with stable "
            "element IDs (w1, w2, …), labels, values, types, and bounding boxes. The IDs are "
            "valid input to web_click and web_fill until the next web_observe call. Use this "
            "INSTEAD of screenshots for web tasks — it sees form fields, hidden ARIA widgets, "
            "and dynamic content that AX/screenshots miss."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max elements to return", "default": 80},
            },
        },
    ),
    Tool(
        name="web_resolve",
        description=(
            "Resolve a semantic browser intent, such as 'repository name textbox' or "
            "'create repository button', to the best element_id from the most recent "
            "web_observe result. Use this after web_observe and before web_click/web_fill "
            "when the target element is described by purpose rather than a known ID."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "intent": {"type": "string", "description": "Semantic target description."},
                "preferred_roles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional preferred roles/tags such as textbox, button, radio, input.",
                },
                "text_hints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional visible text/value hints to prefer.",
                },
                "label_hints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional label/aria/name/placeholder hints to prefer.",
                },
                "require_visible": {"type": "boolean", "default": True},
                "require_enabled": {"type": "boolean", "default": False},
                "mode": {
                    "type": "string",
                    "enum": ["loose", "normal", "strict"],
                    "description": "Resolver strictness. Use strict for final submit/destructive actions.",
                    "default": "normal",
                },
                "scope": {
                    "type": "object",
                    "description": (
                        "Optional resolve scope, e.g. {'same_form_as': 'w12'}, "
                        "{'form_id': '...'}, {'same_container_as': 'w12'}, or {'after_element': 'w12'}."
                    ),
                },
            },
            "required": ["intent"],
        },
    ),
    Tool(
        name="web_click",
        description=(
            "Click a web element by ID returned from the most recent web_observe call. "
            "Works on real DOM elements, so it triggers full event chains (onclick, React "
            "handlers, form submission) just like a real user click."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "element_id": {"type": "string", "description": "Element ID from web_observe (e.g. w12)."},
            },
            "required": ["element_id"],
        },
    ),
    Tool(
        name="web_fill",
        description=(
            "Fill an input/textarea/contenteditable on the current web page by element ID "
            "from web_observe. Clears existing value first. For non-input fields, use "
            "web_click to focus and then type_text via the OS keyboard."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "element_id": {"type": "string", "description": "Element ID from web_observe (e.g. w7)."},
                "text": {"type": "string", "description": "Text to enter."},
            },
            "required": ["element_id", "text"],
        },
    ),
    Tool(
        name="web_fill_form",
        description=(
            "Fill an ENTIRE web form in one call — STRONGLY PREFERRED over chains of "
            "web_resolve/web_fill/web_click whenever a form has 2+ fields. Pass a mapping "
            "of semantic field descriptions to values, e.g. "
            '{"repository name": "my-repo", "visibility": "private", "add a README": true} '
            "plus an optional 'submit' button description. Kim observes the page itself, "
            "resolves each field (text, checkbox, radio option, or select), applies the "
            "value, clicks submit once every field succeeded, and returns a per-field "
            "JSON report with the final page state. Booleans toggle checkboxes; option "
            "names pick radios/selects."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "fields": {
                    "type": "object",
                    "description": (
                        "Map of field description -> value. Examples: "
                        '{"repository name": "demo"} fills a textbox; '
                        '{"visibility": "private"} clicks the Private radio; '
                        '{"add a README checkbox": true} checks a checkbox.'
                    ),
                    "additionalProperties": True,
                },
                "submit": {
                    "type": "string",
                    "description": (
                        "Optional submit/create/save button description. Clicked only "
                        "after all fields succeed."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["loose", "normal", "strict"],
                    "default": "normal",
                    "description": "Resolver match strictness.",
                },
            },
            "required": ["fields"],
        },
    ),
    Tool(
        name="web_press",
        description=(
            "Press a single key in the controlled browser (Enter, Tab, Escape, ArrowDown, "
            "etc.). The key is sent to whichever element currently has focus, so call "
            "web_click or web_fill on the target field first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Key name, e.g. 'Enter', 'Tab', 'Escape'."},
            },
            "required": ["key"],
        },
    ),
    Tool(
        name="web_text",
        description=(
            "Return the visible plain text of the current web page (document.body.innerText). "
            "Use for reading articles, search results, or verifying a page's contents. "
            "Truncated to ~8000 chars by default."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "max_chars": {"type": "integer", "description": "Truncate threshold", "default": 8000},
            },
        },
    ),
    Tool(
        name="web_screenshot",
        description=(
            "Capture the controlled browser page as a base64 PNG. Use ONLY when web_observe "
            "and web_text cannot answer the question (image content, layout, visual styling). "
            "Returns 'WEB_SCREENSHOT_BASE64:image/png:<b64>' — much faster than the OS screenshot."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "full_page": {"type": "boolean", "description": "Capture full scroll height", "default": False},
            },
        },
    ),
    Tool(
        name="web_wait_for",
        description=(
            "Wait until specific text or a CSS selector becomes visible on the current page. "
            "Use after navigation, form submission, or any action that triggers async UI "
            "updates, before re-running web_observe."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Substring of visible text to wait for."},
                "selector": {"type": "string", "description": "CSS selector instead of text."},
                "timeout_ms": {"type": "integer", "description": "Timeout in ms", "default": 10000},
            },
        },
    ),
    Tool(
        name="web_wait_for_url",
        description=(
            "Wait until the controlled browser URL matches url_contains or url_regex. "
            "Use this after navigation or form submission when verifying the destination URL."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "url_contains": {"type": "string", "description": "Substring expected in the URL."},
                "url_regex": {"type": "string", "description": "Regular expression expected to match the URL."},
                "timeout_ms": {"type": "integer", "description": "Timeout in milliseconds", "default": 10000},
            },
        },
    ),
    Tool(
        name="web_back",
        description="Navigate the controlled browser back one entry in its history.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="web_close",
        description=(
            "Indicate that the browser task is finished. The browser will remain open "
            "to preserve your login session and tabs for the next task."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    # open_url drives the controlled browser (handler is handle_web_open), so it
    # belongs to the `web` capability tier — NOT `windows`. Listing it under
    # windows let `KIM_ENABLED_TOOL_TIERS=windows`/`ui` grant browser navigation
    # that the operator meant to withhold (finding 2.3).
    Tool(
        name="open_url",
        description=(
            "Open a URL in Kim's controlled (Playwright-driven) web browser. "
            "Use this for sites you intend to inspect or interact with via "
            "the web_* tools afterwards. For opening URLs in the user's own "
            "default browser, use run_command with the appropriate platform "
            "command (open / xdg-open / start)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to open"},
            },
            "required": ["url"],
        },
    ),
]

_WEB_DISPATCH = {
    "web_open": handle_web_open,
    "open_url": handle_web_open,
    "web_observe": handle_web_observe,
    "web_resolve": handle_web_resolve,
    "web_click": handle_web_click,
    "web_fill": handle_web_fill,
    "web_fill_form": handle_web_fill_form,
    "web_press": handle_web_press,
    "web_text": handle_web_text,
    "web_screenshot": handle_web_screenshot,
    "web_wait_for": handle_web_wait_for,
    "web_wait_for_url": handle_web_wait_for_url,
    "web_back": handle_web_back,
    "web_close": handle_web_close,
}


# ── Mouse input ──────────────────────────────────────────────────────────────

_MOUSE_TOOLS: list[Tool] = [
    Tool(
        name="click",
        description="Click at absolute screen coordinates.",
        inputSchema={
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X coordinate"},
                "y": {"type": "integer", "description": "Y coordinate"},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                "clicks": {"type": "integer", "description": "Number of clicks", "default": 1},
            },
            "required": ["x", "y"],
        },
    ),
    Tool(
        name="double_click",
        description="Double-click at absolute screen coordinates.",
        inputSchema={
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X coordinate"},
                "y": {"type": "integer", "description": "Y coordinate"},
            },
            "required": ["x", "y"],
        },
    ),
    Tool(
        name="right_click",
        description="Right-click at absolute screen coordinates.",
        inputSchema={
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X coordinate"},
                "y": {"type": "integer", "description": "Y coordinate"},
            },
            "required": ["x", "y"],
        },
    ),
    Tool(
        name="drag",
        description="Click and drag from one screen position to another.",
        inputSchema={
            "type": "object",
            "properties": {
                "x1": {"type": "integer", "description": "Start X"},
                "y1": {"type": "integer", "description": "Start Y"},
                "x2": {"type": "integer", "description": "End X"},
                "y2": {"type": "integer", "description": "End Y"},
                "duration": {"type": "number", "description": "Duration in seconds", "default": 0.5},
            },
            "required": ["x1", "y1", "x2", "y2"],
        },
    ),
    Tool(
        name="scroll",
        description="Scroll the mouse wheel at optional screen coordinates.",
        inputSchema={
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X coordinate (-1 = current)"},
                "y": {"type": "integer", "description": "Y coordinate (-1 = current)"},
                "clicks": {"type": "integer", "description": "Number of scroll clicks", "default": 3},
                "direction": {"type": "string", "enum": ["up", "down"], "default": "up"},
            },
        },
    ),
]

_MOUSE_DISPATCH = {
    "click": handle_click,
    "double_click": handle_double_click,
    "right_click": handle_right_click,
    "drag": handle_drag,
    "scroll": handle_scroll,
}


# ── Keyboard input ───────────────────────────────────────────────────────────

_KEYBOARD_TOOLS: list[Tool] = [
    Tool(
        name="type_text",
        description=(
            "Type a string of text at the current cursor position. "
            "Uses the system clipboard for paste, so it's instantaneous; "
            "per-keystroke timing is not supported."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to type"},
            },
            "required": ["text"],
        },
    ),
    Tool(
        name="hotkey",
        description="Press a keyboard shortcut (e.g. 'ctrl+c', 'alt+F4', 'win+d'). Pass as a plus-separated string or array.",
        inputSchema={
            "type": "object",
            "properties": {
                "keys": {
                    "description": "Key combination as string ('ctrl+c') or array (['ctrl','c'])",
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                },
            },
            "required": ["keys"],
        },
    ),
    Tool(
        name="key_press",
        description="Press a single key one or more times (e.g. 'enter', 'tab', 'escape', 'f5').",
        inputSchema={
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Key name (pyautogui format)"},
                "presses": {"type": "integer", "description": "Number of presses", "default": 1},
                "interval": {"type": "number", "description": "Seconds between presses", "default": 0.1},
            },
            "required": ["key"],
        },
    ),
]

_KEYBOARD_DISPATCH = {
    "type_text": handle_type_text,
    "hotkey": handle_hotkey,
    "key_press": handle_key_press,
}


# ── Window management ────────────────────────────────────────────────────────

_WINDOW_TOOLS: list[Tool] = [
    Tool(
        name="get_windows",
        description="List all visible windows with their titles, positions, and sizes.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="focus_window",
        description="Bring a window to the foreground by matching title substring.",
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Window title substring to match"},
            },
            "required": ["title"],
        },
    ),
    Tool(
        name="resize_window",
        description="Move and resize a window by matching title substring.",
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Window title substring to match"},
                "x": {"type": "integer", "description": "Left position", "default": 0},
                "y": {"type": "integer", "description": "Top position", "default": 0},
                "width": {"type": "integer", "description": "Window width", "default": 800},
                "height": {"type": "integer", "description": "Window height", "default": 600},
            },
            "required": ["title"],
        },
    ),
]

_WINDOW_DISPATCH = {
    "get_windows": handle_get_windows,
    "focus_window": handle_focus_window,
    "resize_window": handle_resize_window,
}


# ── Git operations ───────────────────────────────────────────────────────────

_GIT_TOOLS: list[Tool] = [
    Tool(
        name="git_status",
        description="Show the current git working tree status (staged, unstaged, untracked files).",
        inputSchema={
            "type": "object",
            "properties": {
                "cwd": {"type": "string", "description": "Repository directory (defaults to PROJECT_ROOT)"},
                "short": {"type": "boolean", "description": "Compact output format", "default": False},
            },
        },
    ),
    Tool(
        name="git_diff",
        description="Show git diff of working tree changes. Can diff a specific file or all changes.",
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Specific file to diff (optional, omit for all)"},
                "staged": {"type": "boolean", "description": "Show staged changes (--cached)", "default": False},
                "cwd": {"type": "string", "description": "Repository directory (defaults to PROJECT_ROOT)"},
            },
        },
    ),
    Tool(
        name="git_add",
        description="Stage files for the next commit. Use '.' to stage all changes.",
        inputSchema={
            "type": "object",
            "properties": {
                "paths": {
                    "description": "File(s) to stage. String or array of strings. Use '.' for all.",
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "default": ".",
                },
                "cwd": {"type": "string", "description": "Repository directory (defaults to PROJECT_ROOT)"},
            },
        },
    ),
    Tool(
        name="git_commit",
        description="Commit staged changes with a descriptive message.",
        inputSchema={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Commit message (required)"},
                "cwd": {"type": "string", "description": "Repository directory (defaults to PROJECT_ROOT)"},
            },
            "required": ["message"],
        },
    ),
    Tool(
        name="git_log",
        description="Show recent commit history.",
        inputSchema={
            "type": "object",
            "properties": {
                "n": {"type": "integer", "description": "Number of commits to show", "default": 10},
                "oneline": {"type": "boolean", "description": "Compact one-line format", "default": True},
                "cwd": {"type": "string", "description": "Repository directory (defaults to PROJECT_ROOT)"},
            },
        },
    ),
    Tool(
        name="git_checkout",
        description="Switch to a branch, create a new branch, or restore a file to its last committed state.",
        inputSchema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Branch name or file path to checkout"},
                "create": {"type": "boolean", "description": "Create new branch (-b flag)", "default": False},
                "cwd": {"type": "string", "description": "Repository directory (defaults to PROJECT_ROOT)"},
            },
            "required": ["target"],
        },
    ),
    Tool(
        name="github_create_repo",
        description=(
            "Create a GitHub repository deterministically. Tries authenticated gh CLI first "
            "unless prefer_browser is true, then falls back to the controlled browser using "
            "web_observe + semantic element resolution. Defaults to private visibility."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Repository name to create."},
                "description": {"type": "string", "description": "Optional repository description."},
                "visibility": {
                    "type": "string",
                    "enum": ["public", "private"],
                    "description": "Repository visibility. Defaults to private.",
                    "default": "private",
                },
                "prefer_browser": {
                    "type": "boolean",
                    "description": "Skip gh CLI and use browser flow first.",
                    "default": False,
                },
                "open_in_browser": {
                    "type": "boolean",
                    "description": "Open the created repository in Kim's controlled browser when using gh CLI.",
                    "default": False,
                },
                "debug": {
                    "type": "boolean",
                    "description": "Include full attempts/candidates/diagnostics in the tool result.",
                    "default": False,
                },
            },
            "required": ["name"],
        },
    ),
]

_GIT_DISPATCH = {
    "git_status": handle_git_status,
    "git_diff": handle_git_diff,
    "git_add": handle_git_add,
    "git_commit": handle_git_commit,
    "git_log": handle_git_log,
    "git_checkout": handle_git_checkout,
    "github_create_repo": handle_github_create_repo,
}


# ── Code execution ───────────────────────────────────────────────────────────

_CODE_TOOLS: list[Tool] = [
    Tool(
        name="run_python",
        description="Execute a Python file or inline code snippet and return stdout/stderr.",
        inputSchema={
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Path to .py file to execute (relative or absolute)"},
                "code": {"type": "string", "description": "Inline Python code snippet to execute"},
                "cwd": {"type": "string", "description": "Working directory (defaults to PROJECT_ROOT)"},
                "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 30},
            },
        },
    ),
    Tool(
        name="run_node",
        description="Execute a JavaScript file or inline code snippet via Node.js and return stdout/stderr.",
        inputSchema={
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Path to .js file to execute (relative or absolute)"},
                "code": {"type": "string", "description": "Inline JavaScript code snippet to execute"},
                "cwd": {"type": "string", "description": "Working directory (defaults to PROJECT_ROOT)"},
                "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 30},
            },
        },
    ),
    Tool(
        name="lint_file",
        description="Lint a Python file using ruff (preferred) or flake8. Returns warnings and errors.",
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to Python file to lint"},
                "fix": {"type": "boolean", "description": "Auto-fix issues (ruff only)", "default": False},
                "cwd": {"type": "string", "description": "Working directory (defaults to PROJECT_ROOT)"},
            },
            "required": ["path"],
        },
    ),
]

_CODE_DISPATCH = {
    "run_python": handle_run_python,
    "run_node": handle_run_node,
    "lint_file": handle_lint_file,
}


# ── Search ───────────────────────────────────────────────────────────────────

_SEARCH_TOOLS: list[Tool] = [
    Tool(
        name="search_in_files",
        description="Search for a text pattern across all files in the project (like grep/ripgrep). Returns matching lines with file paths and line numbers.",
        inputSchema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Text or regex pattern to search for"},
                "path": {"type": "string", "description": "Directory to search in (defaults to PROJECT_ROOT)"},
                "include": {"type": "string", "description": "File glob filter (e.g. '*.py', '*.ts')"},
                "case_sensitive": {"type": "boolean", "description": "Case-sensitive search", "default": True},
                "regex": {"type": "boolean", "description": "Treat pattern as regex", "default": False},
                "context_lines": {"type": "integer", "description": "Context lines around matches", "default": 0},
            },
            "required": ["pattern"],
        },
    ),
    Tool(
        name="find_files",
        description="Find files matching a glob pattern in the project directory tree. Returns relative paths with file sizes.",
        inputSchema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern (e.g. '*.py', '**/*.ts', 'src/**/*.js')"},
                "path": {"type": "string", "description": "Directory to search in (defaults to PROJECT_ROOT)"},
                "type": {"type": "string", "enum": ["file", "dir", "all"], "description": "Filter by type", "default": "file"},
            },
            "required": ["pattern"],
        },
    ),
]

_SEARCH_DISPATCH = {
    "search_in_files": handle_search_in_files,
    "find_files": handle_find_files,
}


# ── Persistent agent memory ───────────────────────────────────────────────────

_MEMORY_TOOLS: list[Tool] = [
    Tool(
        name="write_memory",
        description=(
            "Store a named finding in persistent project memory so it survives "
            "across agent sessions.  Use this to record discoveries (API endpoints, "
            "file locations, credentials format, architecture notes) that would "
            "otherwise require re-running discovery steps next session.  "
            "Memory is scoped to the current project directory."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Short name for the memory entry (e.g. 'db_host', 'auth_flow')",
                },
                "value": {
                    "type": "string",
                    "description": "Content to store (plain text, up to 16 384 chars)",
                },
                "cwd": {
                    "type": "string",
                    "description": "Project directory to scope memory to (defaults to PROJECT_ROOT)",
                },
            },
            "required": ["key", "value"],
        },
    ),
    Tool(
        name="read_memory",
        description=(
            "Read a named finding from persistent project memory.  "
            "Omit key to list all stored entries for this project."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Key to retrieve (omit to list all entries)",
                },
                "cwd": {
                    "type": "string",
                    "description": "Project directory to scope memory to (defaults to PROJECT_ROOT)",
                },
            },
        },
    ),
]

_MEMORY_DISPATCH = {
    "write_memory": handle_write_memory,
    "read_memory": handle_read_memory,
}


# ── Public aggregates ────────────────────────────────────────────────────────

TOOLS: list[Tool] = (
    _FILE_TOOLS
    + _SHELL_TOOLS
    + _SCREEN_TOOLS
    + _WEB_TOOLS
    + _MOUSE_TOOLS
    + _KEYBOARD_TOOLS
    + _WINDOW_TOOLS
    + _GIT_TOOLS
    + _CODE_TOOLS
    + _SEARCH_TOOLS
    + _MEMORY_TOOLS
)

DISPATCH: dict[str, object] = {}
for _d in (
    _FILE_DISPATCH,
    _SHELL_DISPATCH,
    _SCREEN_DISPATCH,
    _WEB_DISPATCH,
    _MOUSE_DISPATCH,
    _KEYBOARD_DISPATCH,
    _WINDOW_DISPATCH,
    _GIT_DISPATCH,
    _CODE_DISPATCH,
    _SEARCH_DISPATCH,
    _MEMORY_DISPATCH,
):
    # Fail loudly on a duplicate tool name across groups rather than letting a
    # later group silently shadow an earlier handler (finding 2.4). A collision
    # is a programming error in this module, so it should surface at import.
    _dupes = DISPATCH.keys() & _d.keys()
    if _dupes:
        raise RuntimeError(
            f"Duplicate tool name(s) across dispatch groups: {sorted(_dupes)}"
        )
    DISPATCH.update(_d)

# Every advertised Tool must have a handler and vice-versa; a schema without a
# handler would list fine and then hit "Unknown tool" at call time (finding 2.4).
_TOOL_NAMES = {t.name for t in TOOLS}
_missing_handlers = _TOOL_NAMES - DISPATCH.keys()
_missing_schemas = DISPATCH.keys() - _TOOL_NAMES
if _missing_handlers or _missing_schemas:
    raise RuntimeError(
        "TOOLS/DISPATCH mismatch — "
        f"schemas without handlers: {sorted(_missing_handlers)}; "
        f"handlers without schemas: {sorted(_missing_schemas)}"
    )


# -- Tier membership ----------------------------------------------------------
# Maps each granular tier name to the dispatch dict whose keys are that tier's
# tool names.  server.py passes this to tool_tiers.get_active_tool_names() to
# apply KIM_ENABLED_TOOL_TIERS filtering at startup.  TOOLS and DISPATCH above
# remain the full unfiltered sets so default behavior is unchanged.

TIER_DISPATCH: dict[str, dict] = {
    "file_read":  _FILE_READ_DISPATCH,
    "file_write": _FILE_WRITE_DISPATCH,
    "shell":      _SHELL_DISPATCH,
    "screen":     _SCREEN_DISPATCH,
    "web":        _WEB_DISPATCH,
    "mouse":      _MOUSE_DISPATCH,
    "keyboard":   _KEYBOARD_DISPATCH,
    "windows":    _WINDOW_DISPATCH,
    "git":        _GIT_DISPATCH,
    "code":       _CODE_DISPATCH,
    "search":     _SEARCH_DISPATCH,
    "memory":     _MEMORY_DISPATCH,
}

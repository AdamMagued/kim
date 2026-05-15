"""
Kim MCP Server — Phase 1 + Phase 6
Exposes OS control, file I/O, shell execution, screen capture, input
automation, git, code execution, and search as MCP tools over stdio transport.

Usage (Claude Desktop):
    {
      "mcpServers": {
        "kim": {
          "command": "python",
          "args": ["-m", "mcp_server.server"],
          "cwd": "E:\\\\kim"
        }
      }
    }

Usage (Claude Code CLI):
    claude mcp add kim -- python -m mcp_server.server
"""

import asyncio
import logging
import os
import sys

# ──────────────────────────────────────────────────────────────────────────────
# Protect MCP stdio pipe from print() corruption
# ──────────────────────────────────────────────────────────────────────────────
import builtins
_orig_print = builtins.print
def _safe_print(*args, **kwargs):
    if "file" not in kwargs or kwargs["file"] is None:
        kwargs["file"] = sys.stderr
    _orig_print(*args, **kwargs)
builtins.print = _safe_print
# ──────────────────────────────────────────────────────────────────────────────

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from mcp_server.config import LOG_LEVEL, ENABLED_CONNECTOR_IDS
from mcp_server.sites import enabled_connectors, load_builtin_connectors
from mcp_server.tools.files import (
    handle_delete_file,
    handle_list_dir,
    handle_read_file,
    handle_write_file,
)
from mcp_server.tools.keyboard import (
    handle_hotkey,
    handle_key_press,
    handle_type_text,
)
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
from mcp_server.tools.shell import handle_run_command, handle_run_powershell
from mcp_server.tools.ui_observe import handle_click_ui, handle_observe_ui
from mcp_server.tools.web import (
    handle_web_back,
    handle_web_click,
    handle_web_close,
    handle_web_fill,
    handle_web_observe,
    handle_web_open,
    handle_web_press,
    handle_web_screenshot,
    handle_web_text,
    handle_web_wait_for,
)
from mcp_server.tools.windows import (
    handle_focus_window,
    handle_get_windows,
    handle_open_url,
    handle_resize_window,
)
from mcp_server.tools.git import (
    handle_git_status,
    handle_git_diff,
    handle_git_add,
    handle_git_commit,
    handle_git_log,
    handle_git_checkout,
)
from mcp_server.tools.code import (
    handle_run_python,
    handle_run_node,
    handle_lint_file,
)
from mcp_server.tools.search import (
    handle_search_in_files,
    handle_find_files,
)

# Logging goes to stderr — stdout is reserved for MCP protocol messages.
# Try to attach the structured JSONL handler so `logs/kim_*.jsonl` is
# actually produced (previously the helper was defined but never wired in).
# Fall back to plain stderr-only logging if log-dir creation fails for any
# reason — we must never crash the MCP server on a logging-setup failure.
_LOG_LEVEL = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
try:
    from mcp_server.logger import setup_structured_logging
    setup_structured_logging(level=_LOG_LEVEL, also_stderr=True)
except Exception as _log_e:
    logging.basicConfig(
        stream=sys.stderr,
        level=_LOG_LEVEL,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("kim.server").warning(
        "Structured logging disabled (%s); falling back to stderr only", _log_e
    )
logger = logging.getLogger("kim.server")

server = Server("kim")


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

_TOOLS: list[Tool] = [
    # ── File operations ─────────────────────────────────────────────────────
    Tool(
        name="read_file",
        description="Read the full text content of a file. Path can be absolute or relative to PROJECT_ROOT.",
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (absolute or relative to PROJECT_ROOT)"},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="write_file",
        description="Write text content to a file, creating parent directories if needed. Overwrites existing content.",
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (absolute or relative to PROJECT_ROOT)"},
                "content": {"type": "string", "description": "Text content to write"},
            },
            "required": ["path", "content"],
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
    # ── Shell execution ──────────────────────────────────────────────────────
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
        },
    ),
    # ── Screen ──────────────────────────────────────────────────────────────
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
    # ── Mouse ────────────────────────────────────────────────────────────────
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
    # ── Keyboard ─────────────────────────────────────────────────────────────
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
    # ── Windows / browser ────────────────────────────────────────────────────
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
    # ── Git operations ───────────────────────────────────────────────────────
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
    # ── Code execution ───────────────────────────────────────────────────────
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
    # ── Search ───────────────────────────────────────────────────────────────
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

# Build dispatch map
_DISPATCH = {
    "read_file": handle_read_file,
    "write_file": handle_write_file,
    "list_dir": handle_list_dir,
    "delete_file": handle_delete_file,
    "run_command": handle_run_command,
    "run_powershell": handle_run_powershell,
    "take_screenshot": handle_take_screenshot,
    "get_screen_info": handle_get_screen_info,
    "observe_ui": handle_observe_ui,
    "click_ui": handle_click_ui,
    "web_open": handle_web_open,
    "web_observe": handle_web_observe,
    "web_click": handle_web_click,
    "web_fill": handle_web_fill,
    "web_press": handle_web_press,
    "web_text": handle_web_text,
    "web_screenshot": handle_web_screenshot,
    "web_wait_for": handle_web_wait_for,
    "web_back": handle_web_back,
    "web_close": handle_web_close,
    "take_annotated_screenshot": handle_take_annotated_screenshot,
    "click": handle_click,
    "double_click": handle_double_click,
    "right_click": handle_right_click,
    "drag": handle_drag,
    "scroll": handle_scroll,
    "type_text": handle_type_text,
    "hotkey": handle_hotkey,
    "key_press": handle_key_press,
    "get_windows": handle_get_windows,
    "focus_window": handle_focus_window,
    "resize_window": handle_resize_window,
    "open_url": handle_web_open,
    # Git
    "git_status": handle_git_status,
    "git_diff": handle_git_diff,
    "git_add": handle_git_add,
    "git_commit": handle_git_commit,
    "git_log": handle_git_log,
    "git_checkout": handle_git_checkout,
    # Code
    "run_python": handle_run_python,
    "run_node": handle_run_node,
    "lint_file": handle_lint_file,
    # Search
    "search_in_files": handle_search_in_files,
    "find_files": handle_find_files,
}


# ---------------------------------------------------------------------------
# Site connectors — auto-discover + merge into _TOOLS / _DISPATCH for the
# connectors the user has enabled in config.yaml.
# ---------------------------------------------------------------------------
load_builtin_connectors()
_ACTIVE_CONNECTORS = enabled_connectors(ENABLED_CONNECTOR_IDS)
for _c in _ACTIVE_CONNECTORS:
    for _tool in _c.tools:
        _TOOLS.append(_tool)
    for _name, _handler in _c.handlers.items():
        if _name in _DISPATCH:
            logger.warning(
                "Connector %s tool %s collides with existing dispatch entry; "
                "overriding.",
                _c.id, _name,
            )
        _DISPATCH[_name] = _handler
if _ACTIVE_CONNECTORS:
    logger.info(
        "Active site connectors: %s",
        [c.id for c in _ACTIVE_CONNECTORS],
    )


# ---------------------------------------------------------------------------
# MCP handlers
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[Tool]:
    return _TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    handler = _DISPATCH.get(name)
    if handler is None:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    try:
        result = await handler(arguments or {})
        return [TextContent(type="text", text=str(result))]
    except PermissionError as e:
        logger.warning(f"PERMISSION_ERROR in {name}: {e}")
        return [TextContent(type="text", text=f"PERMISSION_ERROR: {e}")]
    except Exception as e:
        logger.error(f"Tool '{name}' raised unexpectedly: {e}", exc_info=True)
        return [TextContent(type="text", text=f"ERROR: {e}")]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    logger.info("Kim MCP server starting (stdio transport)")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
    logger.info("Kim MCP server stopped")


if __name__ == "__main__":
    asyncio.run(main())

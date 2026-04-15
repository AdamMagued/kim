"""
Kim MCP Server — Phase 1
Exposes OS control, file I/O, shell execution, screen capture, and input
automation as MCP tools over stdio transport.

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
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from mcp_server.config import LOG_LEVEL
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
from mcp_server.tools.screen import handle_get_screen_info, handle_take_screenshot
from mcp_server.tools.shell import handle_run_command, handle_run_powershell
from mcp_server.tools.windows import (
    handle_focus_window,
    handle_get_windows,
    handle_open_url,
    handle_resize_window,
)

# Logging goes to stderr — stdout is reserved for MCP protocol messages
logging.basicConfig(
    stream=sys.stderr,
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
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
        description="Capture the screen as a base64-encoded PNG. Returns 'data:image/png;base64,...'.",
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
        description="Type a string of text at the current cursor position.",
        inputSchema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to type"},
                "interval": {"type": "number", "description": "Seconds between keystrokes", "default": 0.02},
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
        description="Open a URL in the system default browser.",
        inputSchema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to open"},
            },
            "required": ["url"],
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
    "open_url": handle_open_url,
}


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

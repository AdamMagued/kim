"""
Kim MCP Server — stdio transport entry point.

Tool definitions and dispatch live in tool_registry.py.
Site connectors are merged at startup from mcp_server/sites/.

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
from mcp_server.tool_registry import DISPATCH, TOOLS

# Logging goes to stderr — stdout is reserved for MCP protocol messages.
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
# Mutable copies — connectors append at startup
# ---------------------------------------------------------------------------
_TOOLS: list[Tool] = list(TOOLS)
_DISPATCH: dict[str, object] = dict(DISPATCH)

# ---------------------------------------------------------------------------
# Site connectors — auto-discover + merge
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

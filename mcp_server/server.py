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
import builtins
import logging
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from mcp_server import approvals, policy
from mcp_server.config import LOG_LEVEL, ENABLED_CONNECTOR_IDS
from mcp_server.sites import enabled_connectors, load_builtin_connectors
from mcp_server.tool_registry import DISPATCH, TOOLS, TIER_DISPATCH
from mcp_server.tool_tiers import get_active_tool_names

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
# Mutable copies -- connectors append at startup
# Apply KIM_ENABLED_TOOL_TIERS filtering before connectors are merged so that
# only the requested tiers are exposed.  Connectors are always appended
# unchanged; they are already gated by ENABLED_CONNECTOR_IDS in config.
# ---------------------------------------------------------------------------
_enabled_names = get_active_tool_names(TIER_DISPATCH)
if _enabled_names is None:
    _TOOLS: list[Tool] = list(TOOLS)
    _DISPATCH: dict[str, object] = dict(DISPATCH)
else:
    _TOOLS = [t for t in TOOLS if t.name in _enabled_names]
    _DISPATCH = {k: v for k, v in DISPATCH.items() if k in _enabled_names}

# ---------------------------------------------------------------------------
# Site connectors — auto-discover + merge
# ---------------------------------------------------------------------------
load_builtin_connectors()
_ACTIVE_CONNECTORS = enabled_connectors(ENABLED_CONNECTOR_IDS)
for _c in _ACTIVE_CONNECTORS:
    for _tool in _c.tools:
        # Subject connector tools to the same tier filter as built-in tools.
        if _enabled_names is not None and _tool.name not in _enabled_names:
            logger.debug(
                "Connector %s tool %s excluded by KIM_ENABLED_TOOL_TIERS",
                _c.id, _tool.name,
            )
            continue
        _TOOLS.append(_tool)
    for _name, _handler in _c.handlers.items():
        # Skip handlers whose tool was excluded by the tier filter.
        if _enabled_names is not None and _name not in _enabled_names:
            continue
        if _name in _DISPATCH:
            raise RuntimeError(
                f"Connector {_c.id!r} tool {_name!r} collides with an existing "
                f"dispatch entry. Rename the connector tool or disable the "
                f"conflicting built-in via KIM_ENABLED_TOOL_TIERS."
            )
        _DISPATCH[_name] = _handler
if _ACTIVE_CONNECTORS:
    logger.info(
        "Active site connectors: %s",
        [c.id for c in _ACTIVE_CONNECTORS],
    )

# ---------------------------------------------------------------------------
# F-H-4: required-argument contract, enforced at the JSON-RPC seam.
# Each tool's inputSchema declares `required: [...]`, but nothing validated the
# arguments against it before dispatch — a call missing a required field hit
# `args["path"]` and surfaced as a one-word `ERROR: 'path'` (KeyError), which the
# agent could not distinguish from a real runtime failure. We build a name→
# required map once from the merged tool list (built-ins + connectors) and check
# it below, returning a distinct `BAD_ARGS:` error instead.
# ---------------------------------------------------------------------------
_REQUIRED_ARGS: dict[str, tuple[str, ...]] = {}
for _t in _TOOLS:
    _schema = getattr(_t, "inputSchema", None) or {}
    _req = _schema.get("required") if isinstance(_schema, dict) else None
    if isinstance(_req, (list, tuple)):
        _REQUIRED_ARGS[_t.name] = tuple(str(k) for k in _req)


def _missing_required(name: str, args: dict) -> list[str]:
    """Required argument keys that are absent or None for this call."""
    return [
        key
        for key in _REQUIRED_ARGS.get(name, ())
        if args.get(key) is None
    ]


# ---------------------------------------------------------------------------
# F-INH-6: at the MCP protocol level a tool error and ordinary tool output were
# indistinguishable — unknown tools, policy denials, bad-args and handler
# exceptions all came back as plain `TextContent` with `isError` unset, so the
# agent had to rely purely on string prefixes (`ERROR:`, `PERMISSION_ERROR:`,
# `POLICY_DENIED:`, `HITL_DENIED:`, `BAD_ARGS:`). We now ALSO set the protocol
# `isError` flag on every error result while KEEPING the string prefixes for
# back-compat (both signals aligned). Success returns isError=False. Handoff to
# Team A: orchestrator/tool_errors.py should recognize the `BAD_ARGS:`,
# `POLICY_DENIED:` and `HITL_DENIED:` prefixes so both ends of the seam agree.
# ---------------------------------------------------------------------------

def _ok(text: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)], isError=False
    )


def _err(text: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)], isError=True
    )


# ---------------------------------------------------------------------------
# MCP handlers
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[Tool]:
    return _TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> CallToolResult:
    handler = _DISPATCH.get(name)
    if handler is None:
        return _err(f"Unknown tool: {name}")

    args = arguments or {}

    # ── K1 chokepoint: EVERY tool call is policy-gated before dispatch. ──
    # policy.enforce never raises (it denies on internal errors), so nothing
    # can slip past on an exception path. The security gate runs BEFORE the
    # required-arg check so a denied tool stays denied regardless of arg
    # validity (no "your args are wrong" leak for a call you may not make).
    decision = policy.enforce(name, args)
    if decision.action == "deny":
        logger.warning("POLICY_DENIED %s: %s", name, decision.message)
        return _err(decision.message)

    # F-H-4: enforce the declared required-argument contract before dispatch so
    # a malformed (but allowed) call gets a typed, actionable error instead of a
    # KeyError leak (`ERROR: 'path'`). Runs before the approval prompt so a human
    # is not asked to approve a structurally-invalid call.
    missing = _missing_required(name, args)
    if missing:
        joined = ", ".join(repr(k) for k in missing)
        return _err(
            f"BAD_ARGS: {name} missing required argument(s): {joined}"
        )

    if decision.action == "approve" and not approvals.is_session_approved(
        decision.signature
    ):
        outcome = await approvals.request_approval(
            tool=name,
            args=args,
            risk=decision.risk,
            reason=decision.reason,
            preview=decision.preview,
        )
        if outcome == "acceptForSession":
            approvals.remember_session_approval(decision.signature)
        elif outcome != "accept":
            logger.warning("HITL_DENIED %s (%s)", name, decision.reason)
            return _err(
                f"HITL_DENIED: User denied '{name}' ({decision.reason}). "
                "Choose a different approach or ask the user for permission."
            )

    try:
        result = await handler(args)  # type: ignore[operator]
        return _ok(str(result))
    except PermissionError as e:
        logger.warning(f"PERMISSION_ERROR in {name}: {e}")
        return _err(f"PERMISSION_ERROR: {e}")
    except Exception as e:
        logger.error(f"Tool '{name}' raised unexpectedly: {e}", exc_info=True)
        return _err(f"ERROR: {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _protect_stdio_pipe() -> None:
    """Redirect stray print() to stderr so tool handlers can't corrupt the
    MCP stdout protocol pipe. Applied only when the server actually runs
    (main / __main__), NOT at import — importing this module in-process
    (tests, tooling) must not globally rebind builtins.print.
    """
    _orig_print = builtins.print

    def _safe_print(*args, **kwargs):
        if "file" not in kwargs or kwargs["file"] is None:
            kwargs["file"] = sys.stderr
        _orig_print(*args, **kwargs)

    builtins.print = _safe_print


async def main() -> None:
    _protect_stdio_pipe()
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

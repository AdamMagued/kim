"""
MCP client utilities for the Kim orchestrator.

Extracted from orchestrator/agent.py:
  - MultiMCPClient: multiplexes multiple MCP ClientSessions
  - mcp_session_context: async context manager that starts the Kim MCP
    server (plus any extras from config.yaml) and yields a MultiMCPClient

Imported in agent.py as:
    from orchestrator.mcp_client import MultiMCPClient, mcp_session_context
"""

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager, AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from orchestrator.approval_broker import get_approval_broker

logger = logging.getLogger(__name__)


class MultiMCPClient:
    """
    Multiplexes multiple MCP ClientSessions into a single session-like object.
    Used by KimAgent to interact with both the internal OS tools and any
    extra servers (like plantuml) defined in config.yaml.
    """
    def __init__(self, sessions: list[ClientSession]):
        self.sessions = sessions
        self._tool_map: dict[str, ClientSession] = {}

    async def initialize(self) -> None:
        """Initialize all underlying sessions."""
        await asyncio.gather(*(s.initialize() for s in self.sessions))

    async def list_tools(self) -> Any:
        """Aggregate tools from all servers and build a dispatch map."""
        all_tools = []
        self._tool_map.clear()
        for session in self.sessions:
            res = await session.list_tools()
            for t in res.tools:
                if t.name in self._tool_map and self._tool_map[t.name] is not session:
                    # Name collision across servers. Without this warning the
                    # later server would silently win and the tool would
                    # route somewhere unexpected. Keep the FIRST registration
                    # (servers earlier in config win) so behaviour is stable.
                    logger.warning(
                        "MCP tool name collision: %r is offered by multiple "
                        "servers; keeping the first registration",
                        t.name,
                    )
                    continue
                self._tool_map[t.name] = session
                all_tools.append(t)
        # Wrap in a simple object that has a .tools attribute to match MCP SDK expectations
        return type("ToolsResult", (), {"tools": all_tools})

    async def call_tool(self, name: str, arguments: dict) -> Any:
        """Route the call to the session that owns the tool."""
        session = self._tool_map.get(name)
        if not session:
            raise RuntimeError(f"Tool '{name}' not found in any active MCP session")
        return await session.call_tool(name, arguments)


@asynccontextmanager
async def mcp_session_context(config: dict):
    """Start the Kim MCP server (plus any extras from config.yaml) and
    yield a MultiMCPClient that aggregates all sessions."""
    project_root = str(
        Path(
            os.environ.get("PROJECT_ROOT") or config.get("project_root", str(Path.cwd()))
        ).resolve()
    )
    # The MCP SDK's stdio_client may use a restricted environment when
    # StdioServerParameters.env is provided.  We merge our extra keys
    # with the full parent environment so nothing critical is lost.
    _EXTRA_ENV_KEYS = [
        "PYTHONPATH", "PROJECT_ROOT", "VIRTUAL_ENV",
        "KIM_WEBVIEW_BRIDGE_URL", "KIM_WEBVIEW_BRIDGE_TOKEN",
    ]
    extra_env = {k: os.environ[k] for k in _EXTRA_ENV_KEYS if k in os.environ}

    # K1: start the approval broker BEFORE spawning the MCP server so the
    # server-side HITL gate (mcp_server/approvals.py) has a channel back to
    # this process. Without the env vars the server fail-closes approvals.
    broker = get_approval_broker()
    try:
        broker_env = await broker.start()
    except Exception as broker_err:
        logger.warning(
            "approval broker failed to start (%s) — server-side approvals "
            "will default-deny", broker_err,
        )
        broker_env = {}

    merged_env = {**os.environ, **extra_env, **broker_env}

    # 1. Prepare internal Kim server
    server_list = [
        StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_server.server"],
            cwd=project_root,
            env=merged_env,
        )
    ]

    # 2. Add extra servers from config.yaml
    extra_servers = config.get("mcp_servers", {})
    if isinstance(extra_servers, dict):
        for name, s_cfg in extra_servers.items():
            cmd = s_cfg.get("command")
            if not cmd:
                logger.warning(f"MCP server '{name}' missing 'command' — skipping")
                continue
            server_list.append(StdioServerParameters(
                command=cmd,
                args=s_cfg.get("args", []),
                cwd=s_cfg.get("cwd") or project_root,
                env=merged_env,
            ))

    async with AsyncExitStack() as stack:
        if broker.is_running():
            stack.push_async_callback(broker.stop)
        sessions = []
        internal_params = server_list[0]  # always the Kim MCP server
        internal_ok = False
        for params in server_list:
            try:
                transport = await stack.enter_async_context(stdio_client(params))
                read_stream, write_stream = transport
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                sessions.append(session)
                if params is internal_params:
                    internal_ok = True
            except Exception as e:
                logger.error(f"Failed to start MCP server {params.command}: {e}")
                if params is internal_params:
                    raise RuntimeError(
                        "Core Kim MCP server (mcp_server.server) failed to start — "
                        "OS-control tools are unavailable. Aborting."
                    ) from e

        if not internal_ok:
            # Should not be reachable (the loop raises above), but guard anyway.
            raise RuntimeError("Core Kim MCP server did not start; cannot continue.")

        if not sessions:
            raise RuntimeError("No MCP servers could be started.")

        multi_client = MultiMCPClient(sessions)
        try:
            await asyncio.wait_for(multi_client.initialize(), timeout=30.0)
        except asyncio.TimeoutError:
            raise RuntimeError("One or more MCP servers timed out during initialization.")

        logger.info(f"Initialized {len(sessions)} MCP sessions")
        yield multi_client

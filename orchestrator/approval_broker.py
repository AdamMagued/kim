"""
ApprovalBroker — the orchestrator side of the MCP server's HITL gate (K1).

The MCP server (mcp_server/approvals.py) connects here when policy.enforce()
demands a human decision. The broker forwards the request through the
EXISTING approval plumbing — emit_hitl_approval_request() on stdout, and the
{"type":"hitl_approve", …} stdin line that Tauri's hitl_respond_approval and
the kim CLI's terminal prompt already write — then replies with a normalized
decision: "accept" | "acceptForSession" | "decline".

Lifecycle: mcp_client.mcp_session_context() starts the broker before spawning
the MCP server and injects KIM_APPROVAL_SOCK (POSIX unix socket) or
KIM_APPROVAL_TCP (Windows loopback) into the server's environment. KimAgent
registers the resolver when it owns a UIBridge; until a resolver exists every
request is declined (fail closed).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

Resolver = Callable[[dict], Awaitable[str]]

_VALID_DECISIONS = frozenset({"accept", "acceptForSession", "decline"})


class ApprovalBroker:
    """Loopback listener that brokers server-side approval requests."""

    def __init__(self) -> None:
        self._resolver: Optional[Resolver] = None
        self._server: Optional[asyncio.AbstractServer] = None
        self._sock_dir: Optional[tempfile.TemporaryDirectory] = None
        self._env: dict[str, str] = {}

    # ── wiring ─────────────────────────────────────────────────────────

    def set_resolver(self, resolver: Optional[Resolver]) -> None:
        """Install the callable that asks the human. None → decline all."""
        self._resolver = resolver

    def is_running(self) -> bool:
        return self._server is not None

    @property
    def env(self) -> dict[str, str]:
        """Env vars the MCP server needs to reach this broker."""
        return dict(self._env)

    async def start(self) -> dict[str, str]:
        """Start the listener (idempotent). Returns the transport env vars."""
        if self._server is not None:
            return self.env
        if sys.platform == "win32":
            self._server = await asyncio.start_server(
                self._handle_conn, host="127.0.0.1", port=0
            )
            port = self._server.sockets[0].getsockname()[1]
            self._env = {"KIM_APPROVAL_TCP": f"127.0.0.1:{port}"}
        else:
            # Short prefix: macOS caps sun_path at ~104 chars.
            self._sock_dir = tempfile.TemporaryDirectory(prefix="kim-appr-")
            sock_path = os.path.join(self._sock_dir.name, "approval.sock")
            self._server = await asyncio.start_unix_server(
                self._handle_conn, path=sock_path
            )
            self._env = {"KIM_APPROVAL_SOCK": sock_path}
        logger.info("approval broker listening (%s)", self._env)
        return self.env

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            try:
                # Python 3.12+: wait_closed() also waits for in-flight
                # connection handlers — a still-pending approval must not
                # hang shutdown, so cap the wait.
                await asyncio.wait_for(server.wait_closed(), timeout=2.0)
            except Exception:
                pass
        if self._sock_dir is not None:
            try:
                self._sock_dir.cleanup()
            except Exception:
                pass
            self._sock_dir = None
        self._env = {}

    # ── request handling ───────────────────────────────────────────────

    async def _handle_conn(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        decision = "decline"
        request: dict = {}
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=30.0)
            request = json.loads(line.decode("utf-8", errors="replace"))
            resolver = self._resolver
            if resolver is None:
                logger.warning(
                    "approval request for %r with no resolver attached — declining",
                    request.get("tool"),
                )
            else:
                decision = await resolver(request)
                if decision not in _VALID_DECISIONS:
                    decision = "decline"
        except Exception as e:
            logger.warning("approval broker request failed (%s) — declining", e)
        try:
            reply = json.dumps({"id": request.get("id", ""), "decision": decision})
            writer.write((reply + "\n").encode("utf-8"))
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass


_BROKER: Optional[ApprovalBroker] = None


def get_approval_broker() -> ApprovalBroker:
    global _BROKER
    if _BROKER is None:
        _BROKER = ApprovalBroker()
    return _BROKER

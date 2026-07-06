"""
Kim MCP Server — server-side HITL approval transport (K1 approval flow).

When policy.enforce() returns "approve", call_tool blocks here until a human
decides, with default-deny on timeout or on any transport failure.

Transport: the orchestrator (the process that spawned this server) hosts an
approval broker and advertises it via environment variables:

  KIM_APPROVAL_SOCK — path to a Unix domain socket (POSIX)
  KIM_APPROVAL_TCP  — "127.0.0.1:<port>" loopback listener (Windows fallback)

Protocol (one JSON line each way per connection):
  → {"type": "tool_approval_request", "id": …, "tool": …, "risk": …,
     "reason": …, "preview": …, "args": {…}}
  ← {"id": …, "decision": "accept" | "acceptForSession" | "decline"}
    (legacy replies carrying only {"approved": bool} are accepted too)

The broker side (orchestrator/approval_broker.py) forwards the request to the
existing GUI/CLI plumbing: emit_hitl_approval_request on stdout, and the
{"type":"hitl_approve", …} stdin line written by Tauri's hitl_respond_approval
or the kim CLI's terminal prompt.

Session cache: "acceptForSession" remembers the decision signature for the
lifetime of this server process (== one Kim session), so identical calls stop
re-prompting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid

logger = logging.getLogger(__name__)

# How long a single approval may stay pending before the server default-denies.
# The broker's own resolver timeout (120 s, matching the legacy agent-side
# gate) fires first in practice; this is the backstop for a dead broker.
_APPROVAL_TIMEOUT_S = float(os.environ.get("KIM_APPROVAL_TIMEOUT_S", "150"))

_VALID_DECISIONS = frozenset({"accept", "acceptForSession", "decline"})

# Signatures the user approved for the rest of this session.
_SESSION_APPROVED: set[str] = set()


def is_session_approved(signature: str) -> bool:
    return bool(signature) and signature in _SESSION_APPROVED


def remember_session_approval(signature: str) -> None:
    if signature:
        _SESSION_APPROVED.add(signature)


def reset_session_approvals() -> None:
    """Test hook — a fresh session starts with an empty cache."""
    _SESSION_APPROVED.clear()


def _normalize_decision(data: dict) -> str:
    decision = str(data.get("decision", "")).strip()
    if decision == "accept_for_session":
        decision = "acceptForSession"
    if decision in _VALID_DECISIONS:
        return decision
    # Legacy vocabulary: bare {"approved": bool}.
    if "approved" in data:
        return "accept" if bool(data.get("approved")) else "decline"
    return "decline"


async def _open_channel() -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
    sock_path = os.environ.get("KIM_APPROVAL_SOCK", "").strip()
    if sock_path:
        return await asyncio.open_unix_connection(sock_path)
    tcp = os.environ.get("KIM_APPROVAL_TCP", "").strip()
    if tcp and ":" in tcp:
        host, _, port = tcp.rpartition(":")
        return await asyncio.open_connection(host, int(port))
    return None


def channel_configured() -> bool:
    return bool(
        os.environ.get("KIM_APPROVAL_SOCK", "").strip()
        or os.environ.get("KIM_APPROVAL_TCP", "").strip()
    )


async def request_approval(
    tool: str,
    args: dict,
    risk: str,
    reason: str,
    preview: str,
) -> str:
    """Round-trip one approval request. Returns a normalized decision string.

    Every failure mode — no channel configured, connect error, malformed
    reply, timeout — resolves to "decline" (fail closed).
    """
    request_id = uuid.uuid4().hex
    payload = json.dumps(
        {
            "type": "tool_approval_request",
            "id": request_id,
            "tool": tool,
            "risk": risk,
            "reason": reason,
            "preview": preview,
            "args": _json_safe(args),
        },
        ensure_ascii=False,
    )

    writer: asyncio.StreamWriter | None = None
    try:
        channel = await asyncio.wait_for(_open_channel(), timeout=10.0)
        if channel is None:
            logger.warning(
                "approval required for %r but no approval channel is configured "
                "(KIM_APPROVAL_SOCK / KIM_APPROVAL_TCP) — denying",
                tool,
            )
            return "decline"
        reader, writer = channel
        writer.write((payload + "\n").encode("utf-8"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=_APPROVAL_TIMEOUT_S)
        if not line:
            return "decline"
        data = json.loads(line.decode("utf-8", errors="replace"))
        reply_id = str(data.get("id", "") or "")
        if reply_id and reply_id != request_id:
            logger.warning("approval reply id mismatch for %r — denying", tool)
            return "decline"
        return _normalize_decision(data)
    except Exception as e:
        logger.warning("approval round-trip failed for %r (%s) — denying", tool, e)
        return "decline"
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass


def _json_safe(args: dict) -> dict:
    try:
        json.dumps(args)
        return args
    except (TypeError, ValueError):
        return {k: str(v) for k, v in args.items()}

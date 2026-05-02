"""
Kim Relay Server — FastAPI application

Thin message-bus between a phone (or any HTTP client) and the local PC agent.
The PC agent polls this server; no inbound connection to the PC is needed.

Endpoints
─────────
POST  /prompt            phone → submit task           auth: phone_key
GET   /prompt/next       PC   → dequeue next task       auth: pc_key
POST  /result            PC   → upload result           auth: pc_key
GET   /result/{task_id}  phone → poll result            auth: phone_key
WS    /ws                phone → real-time result push  auth: token query param
GET   /status            anyone → health check          public

Run locally:
    uvicorn relay_server.main:app --host 0.0.0.0 --port 3001 --reload

Deploy to Railway / Render:
    Set env vars RELAY_PHONE_API_KEY and RELAY_PC_API_KEY in the dashboard.
    The Dockerfile / railway.toml handle the rest.
"""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from relay_server.auth import require_any_key, require_pc_key, require_phone_key
from relay_server.models import (
    PromptRequest,
    PromptResponse,
    ResultRequest,
    ResultResponse,
    StatusResponse,
    TaskStatusResponse,
)
from relay_server.queue import db

logger = logging.getLogger(__name__)

# ── PC heartbeat tracking ──────────────────────────────────────────────────────

_last_pc_seen: Optional[datetime] = None
_PC_TIMEOUT_S = int(os.environ.get("PC_TIMEOUT_S", "15"))

def _mark_pc_seen() -> None:
    global _last_pc_seen
    _last_pc_seen = datetime.now(timezone.utc)

def _pc_connected() -> bool:
    if _last_pc_seen is None:
        return False
    return (datetime.now(timezone.utc) - _last_pc_seen).total_seconds() < _PC_TIMEOUT_S

# ── WebSocket manager ──────────────────────────────────────────────────────────

class _WsManager:
    """Broadcasts task-result events to all connected phone WebSocket clients."""

    def __init__(self) -> None:
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.append(ws)
        logger.info(f"WS client connected ({len(self._connections)} total)")

    def disconnect(self, ws: WebSocket) -> None:
        try:
            self._connections.remove(ws)
        except ValueError:
            pass
        logger.info(f"WS client disconnected ({len(self._connections)} remaining)")

    async def broadcast(self, payload: dict) -> None:
        dead: list[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self._connections:
                self._connections.remove(ws)


ws_manager = _WsManager()


# ── App lifespan ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(app: FastAPI):
    await db.init()
    logger.info("Kim Relay Server started")
    yield
    await db.close()
    logger.info("Kim Relay Server stopped")


# ── FastAPI app ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Kim Relay Server",
    description="Message bus between phone and PC agent",
    version="1.0.0",
    lifespan=_lifespan,
)

allowed_origins_str = os.environ.get("ALLOWED_ORIGINS", "")
allowed_origins = [o.strip() for o in allowed_origins_str.split(",")] if allowed_origins_str else []

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.post(
    "/prompt",
    response_model=PromptResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Phone: submit a task",
    dependencies=[Depends(require_phone_key)],
)
async def submit_prompt(body: PromptRequest) -> PromptResponse:
    """
    Accept a natural-language task from the phone and add it to the queue.
    Returns a task_id the phone can use to poll for the result.
    """
    task_id = await db.enqueue(body.task, body.priority)
    return PromptResponse(task_id=task_id, queued=True)


@app.get(
    "/prompt/next",
    summary="PC: dequeue the next pending task",
    dependencies=[Depends(require_pc_key)],
)
async def dequeue_prompt():
    """
    The PC agent calls this every poll_interval seconds.
    Returns the highest-priority pending task and marks it as 'running'.
    Returns HTTP 204 (no body) when the queue is empty.
    """
    _mark_pc_seen()
    item = await db.dequeue()
    if item is None:
        return JSONResponse(status_code=status.HTTP_204_NO_CONTENT, content=None)
    return item  # {"task_id": str, "task": str}


@app.post(
    "/result",
    response_model=ResultResponse,
    summary="PC: upload task result",
    dependencies=[Depends(require_pc_key)],
)
async def post_result(body: ResultRequest) -> ResultResponse:
    """
    The PC agent posts the final summary and an optional base64 screenshot
    after completing (or failing) a task.
    """
    _mark_pc_seen()
    await db.complete(
        task_id=body.task_id,
        summary=body.summary,
        screenshot=body.screenshot,
        success=body.success,
    )

    # Broadcast to any connected phone WebSocket clients
    row = await db.get(body.task_id)
    if row:
        await ws_manager.broadcast({
            "task_id":    body.task_id,
            "status":     row["status"],
            "summary":    body.summary,
            "screenshot": body.screenshot,
            "success":    body.success,
        })

    return ResultResponse(ok=True)


@app.get(
    "/result/{task_id}",
    response_model=TaskStatusResponse,
    summary="Phone: poll task status / result",
    dependencies=[Depends(require_phone_key)],
)
async def get_result(task_id: str) -> TaskStatusResponse:
    """
    Phone polls this endpoint after submitting a task.
    Returns status (pending | running | done | failed), summary, and screenshot.
    """
    row = await db.get(task_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id!r} not found",
        )
    return TaskStatusResponse(
        task_id=row["id"],
        status=row["status"],
        summary=row.get("summary"),
        screenshot=row.get("screenshot"),
        created_at=_parse_ts(row.get("created_at")),
        completed_at=_parse_ts(row.get("completed_at")),
    )


@app.get(
    "/status",
    response_model=StatusResponse,
    summary="Health check (auth required)",
    dependencies=[Depends(require_any_key)],
)
async def get_status() -> StatusResponse:
    """
    Returns whether the PC agent is connected (polling within the last 15 s),
    the ISO-8601 timestamp of the last PC poll, and the current queue depth.
    Requires either PC or Phone API key.
    """
    depth = await db.queue_depth()
    last = _last_pc_seen.strftime("%Y-%m-%dT%H:%M:%SZ") if _last_pc_seen else None
    return StatusResponse(
        pc_connected=_pc_connected(),
        last_seen=last,
        queue_depth=depth,
    )


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """
    Real-time result push to the phone.
    Connect with:  wss://your-relay.app/ws (requires X-API-Key header)

    The server pushes a JSON object whenever a task completes:
        {"task_id": "...", "status": "done"|"failed", "summary": "...", "screenshot": "..."}
    """
    # The client must send X-API-Key header instead of query param.
    # Note: browser JS WebSocket API doesn't support custom headers easily,
    # but since this is for the phone client (which might be native or using a library),
    # we enforce header-based auth to avoid token leakage in URL logs.
    token_header = ws.headers.get("x-api-key", "")
    phone_key = os.environ.get("RELAY_PHONE_API_KEY", "")
    if not phone_key or not token_header or not secrets.compare_digest(token_header, phone_key):
        await ws.close(code=4001, reason="Invalid or missing token header")
        return

    await ws_manager.connect(ws)
    try:
        import asyncio
        while True:
            # Keep alive — client should send periodic pings; we echo them
            try:
                # Add a timeout so half-open connections eventually close
                data = await asyncio.wait_for(ws.receive_text(), timeout=60.0)
                if data == "ping":
                    await ws.send_text("pong")
            except asyncio.TimeoutError:
                # No ping received within 60s, assume connection is dead
                await ws.close(code=1011, reason="Ping timeout")
                break
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(ws)


# ── Utility ────────────────────────────────────────────────────────────────────

def _parse_ts(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        # SQLite stores as ISO string; parse it
        s = str(value).rstrip("Z")
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except (ValueError, TypeError):
        return None

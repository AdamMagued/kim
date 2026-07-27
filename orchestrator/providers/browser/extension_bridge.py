"""
Extension WebSocket Bridge for Kim's Browser Provider.

Hosts a local WebSocket server on ws://127.0.0.1:10533 that bridges
Kim's BrowserProvider to the unpacked Chrome Extension (chrome_extension/content.js
& injected.js).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import uuid
from typing import Callable, Optional, Dict, Any

from aiohttp import web

logger = logging.getLogger("kim.extension_bridge")

WS_PORT = 10533


class ExtensionBridgeServer:
    def __init__(self, port: int = WS_PORT):
        self.port = port
        self.active_ws: Optional[web.WebSocketResponse] = None
        self.bridge_ready: bool = False
        self.pending_requests: Dict[str, asyncio.Future] = {}
        self.streaming_callbacks: Dict[str, Callable[[str], None]] = {}
        self._runner: Optional[web.AppRunner] = None
        # Persist conversation state across turns for single-thread continuity
        self._current_conversation_id: Optional[str] = None
        self._current_message_id: Optional[str] = None
        # Set whenever a connection is live. wait_for_connection awaits this
        # instead of polling, so a request dispatched while the extension is
        # already connected costs no wall-clock delay at all (the old 0.5s
        # poll loop added up to half a second to the FIRST turn after every
        # reconnect, and to every turn that raced a reload).
        self._connected_event: asyncio.Event = asyncio.Event()
        # Serializes send_completion. The browser side is a single ChatGPT
        # thread whose parent_message_id chains turn to turn — two overlapping
        # sends would interleave their conversation/message pointers and fork
        # the thread. See _current_conversation_id.
        self._send_lock: asyncio.Lock = asyncio.Lock()

    async def start(self):
        if self._runner is not None:
            return
        app = web.Application()
        app.router.add_get("/", self._ws_handler)
        app.router.add_get("/{tail:.*}", self._ws_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", self.port)
        try:
            await site.start()
        except OSError:
            # Bind failed (port taken by a stale proxy, or by another Kim).
            # Tear the half-built runner back down so a later start() can
            # retry instead of finding a non-None _runner and returning as
            # if the server were up.
            await runner.cleanup()
            raise
        self._runner = runner
        logger.info(f"[Kim Bridge] Extension WebSocket server running on ws://127.0.0.1:{self.port}")

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()
            # Clear the handle: a stopped server must be restartable, and
            # leaving _runner set made start() a permanent no-op afterwards.
            self._runner = None
        self.active_ws = None
        self.bridge_ready = False
        self._connected_event.clear()
        self._fail_pending(RuntimeError("Kim Bridge shut down"))

    def _fail_pending(self, exc: BaseException) -> None:
        """Complete every in-flight request with `exc` and drop its callbacks.

        Without this, losing the extension (tab closed, Chrome reloaded the
        unpacked extension, laptop slept) left each caller blocked on its
        future for the FULL send timeout — three minutes of nothing for an
        event we already know about. The pending/callback maps also grew
        without bound across reconnect cycles.
        """
        if not self.pending_requests:
            return
        logger.warning(
            "[Kim Bridge] Failing %d in-flight request(s): %s",
            len(self.pending_requests), exc,
        )
        for req_id, fut in list(self.pending_requests.items()):
            if not fut.done():
                fut.set_exception(exc)
            self.pending_requests.pop(req_id, None)
            self.streaming_callbacks.pop(req_id, None)

    async def wait_for_connection(self, timeout: float = 10.0) -> bool:
        if self.active_ws is not None and not self.active_ws.closed:
            return True
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return self.active_ws is not None and not self.active_ws.closed

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        previous = self.active_ws
        self.active_ws = ws
        self.bridge_ready = True
        self._connected_event.set()
        if previous is not None and previous is not ws:
            # A second tab (or a reloaded extension) took over. Anything still
            # in flight belonged to the socket we just displaced and will
            # never be answered — fail it now instead of at timeout.
            self._fail_pending(
                RuntimeError("Chrome Extension reconnected — in-flight request abandoned")
            )
            with contextlib.suppress(Exception):
                await previous.close()
        logger.info("[Kim Bridge] Chrome Extension connected!")

        try:
            async for msg in ws:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue

                msg_type = data.get("type")
                if msg_type in ("extension_connected", "bridge_ready"):
                    self.bridge_ready = True
                    logger.info("[Kim Bridge] Chrome Extension bridge ready!")
                elif msg_type == "ping":
                    with contextlib.suppress(Exception):
                        await ws.send_json({"type": "pong"})
                elif msg_type == "response":
                    self._handle_response_frame(data)

        except Exception as e:
            logger.warning(f"[Kim Bridge] WS exception: {e}")
        finally:
            logger.info("[Kim Bridge] Chrome Extension disconnected")
            if self.active_ws is ws:
                self.active_ws = None
                self.bridge_ready = False
                self._connected_event.clear()
                self._fail_pending(
                    RuntimeError(
                        "Chrome Extension disconnected from Kim Bridge — "
                        "reopen chatgpt.com in Chrome and retry."
                    )
                )
        return ws

    def _handle_response_frame(self, data: dict) -> None:
        """Dispatch one `response` frame from the extension.

        Kept off the `async for` loop body so that a raising delta callback
        cannot escape and tear down the whole WebSocket handler — which used
        to disconnect the extension (and strand every other request) because
        one consumer misbehaved.
        """
        req_id = data.get("requestId")
        if not isinstance(req_id, str):
            return
        event = data.get("event")

        if event == "log":
            logger.info("[Kim Bridge] %s", data.get("delta", ""))
            return

        if event == "delta":
            callback = self.streaming_callbacks.get(req_id)
            delta = data.get("delta", "")
            if callback and delta:
                try:
                    callback(delta)
                except Exception as exc:
                    logger.warning(
                        "[Kim Bridge] Delta callback for %s raised (%s) — dropping it "
                        "and continuing the stream.", req_id, exc,
                    )
                    self.streaming_callbacks.pop(req_id, None)
            return

        fut = self.pending_requests.get(req_id)
        if fut is None or fut.done():
            return
        if event == "done":
            fut.set_result(data)
        elif event == "error":
            fut.set_exception(RuntimeError(str(data.get("error") or "Extension error")))

    async def send_cancel(self, req_id: str):
        """Notify the extension to stop generating text for the specified request."""
        ws = self.active_ws
        if ws and not ws.closed:
            try:
                await ws.send_json({"type": "cancel", "requestId": req_id})
                logger.info(f"[Kim Bridge] Sent cancel signal for req_id={req_id}")
            except Exception as e:
                logger.warning(f"[Kim Bridge] Error sending cancel signal: {e}")

    def snapshot_thread_state(self) -> tuple[Optional[str], Optional[str]]:
        """Current (conversation_id, message_id) pointers for the live thread."""
        return self._current_conversation_id, self._current_message_id

    def restore_thread_state(
        self, conversation_id: Optional[str], message_id: Optional[str]
    ) -> None:
        """Put back pointers captured by ``snapshot_thread_state``.

        Side-conversations that must not join the user's thread (context
        compaction's summarizer, background title generation) call the
        provider with ``clear_chat=True``. That resets these pointers and then
        stores the throwaway thread's ids, so every following turn of the REAL
        session silently continued inside the summarizer's chat. Callers wrap
        such a side-call in snapshot/restore to keep the user's thread intact.
        """
        self._current_conversation_id = conversation_id
        self._current_message_id = message_id

    async def send_completion(
        self,
        prompt: str,
        on_delta: Optional[Callable[[str], None]] = None,
        conversation_id: Optional[str] = None,
        parent_message_id: Optional[str] = None,
        timeout: float = 180.0,
        clear_chat: bool = False,
        attachments: Optional[list[dict]] = None,
    ) -> dict[str, Any]:
        async with self._send_lock:
            return await self._send_completion_locked(
                prompt,
                on_delta=on_delta,
                conversation_id=conversation_id,
                parent_message_id=parent_message_id,
                timeout=timeout,
                clear_chat=clear_chat,
                attachments=attachments,
            )

    async def _send_completion_locked(
        self,
        prompt: str,
        *,
        on_delta: Optional[Callable[[str], None]],
        conversation_id: Optional[str],
        parent_message_id: Optional[str],
        timeout: float,
        clear_chat: bool,
        attachments: Optional[list[dict]] = None,
    ) -> dict[str, Any]:
        if not self.active_ws or self.active_ws.closed:
            connected = await self.wait_for_connection(timeout=10.0)
            if not connected:
                raise RuntimeError("Chrome Extension is not connected to Kim Bridge! Open chatgpt.com in Chrome.")

        ws = self.active_ws
        if ws is None or ws.closed:
            raise RuntimeError("Chrome Extension connection dropped before the request was sent.")

        # If clear_chat is requested, reset stored conversation state
        if clear_chat:
            logger.info("[Kim Bridge] clear_chat=True — resetting conversation state for new thread")
            self._current_conversation_id = None
            self._current_message_id = None
        else:
            # Auto-use stored conversation state if caller didn't provide one
            if not conversation_id and self._current_conversation_id:
                conversation_id = self._current_conversation_id
                logger.info(f"[Kim Bridge] Continuing conversation {conversation_id[:12]}…")
            if not parent_message_id and self._current_message_id:
                parent_message_id = self._current_message_id

        req_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        gizmo_id = os.getenv("KIM_GIZMO_ID") or None

        payload = {
            "type": "request",
            "requestId": req_id,
            "messages": [{"role": "user", "content": prompt}],
            "model": "auto",
        }
        if gizmo_id:
            payload["gizmoId"] = gizmo_id
            logger.info("[Kim Bridge] Custom GPT Gizmo mode: forwarding gizmoId=%s", gizmo_id)
        if attachments:
            payload["attachments"] = attachments
        if conversation_id:
            payload["conversationId"] = conversation_id
        if parent_message_id:
            payload["parentMessageId"] = parent_message_id

        # Register BEFORE awaiting the send (the reply can only arrive after
        # it, but registering after would race a fast extension), and unwind
        # the registration if the send itself fails — otherwise a dropped
        # socket left one orphaned future and one orphaned callback in these
        # maps forever, for every failed send.
        self.pending_requests[req_id] = fut
        if on_delta:
            self.streaming_callbacks[req_id] = on_delta
        try:
            await ws.send_json(payload)
        except Exception:
            self.pending_requests.pop(req_id, None)
            self.streaming_callbacks.pop(req_id, None)
            raise

        try:
            res_data = await asyncio.wait_for(fut, timeout=timeout)

            # Persist conversation state for next turn
            conv_id = res_data.get("conversationId") or ""
            msg_id = res_data.get("messageId") or ""
            if conv_id:
                self._current_conversation_id = conv_id
                logger.info(f"[Kim Bridge] Stored conversationId={conv_id[:12]}… for next turn")
            if msg_id:
                self._current_message_id = msg_id

            return {
                "full_text": res_data.get("fullText", ""),
                "conversation_id": conv_id,
                "message_id": msg_id,
            }
        except asyncio.TimeoutError:
            # The extension is still streaming into a request nobody is
            # waiting for. Tell it to stop, or the tab keeps generating (and
            # burns the thread's next parent_message_id) after we gave up.
            logger.error(
                "[Kim Bridge] Request %s timed out after %.0fs — cancelling the browser turn",
                req_id, timeout,
            )
            await self.send_cancel(req_id)
            raise
        except asyncio.CancelledError:
            logger.info(f"[Kim Bridge] Request {req_id} cancelled by client — stopping ChatGPT Web generation")
            await self.send_cancel(req_id)
            raise
        finally:
            self.pending_requests.pop(req_id, None)
            self.streaming_callbacks.pop(req_id, None)


_bridge_server: Optional[ExtensionBridgeServer] = None


@contextlib.asynccontextmanager
async def preserved_thread_state():
    """Protect the live browser thread across a ``clear_chat=True`` side-call.

    Compaction's summarizer (and any other background turn that must not land
    in the user's chat) asks the provider for a FRESH chat. On the extension
    bridge that clears ``_current_conversation_id``/``_current_message_id``
    and then stores the throwaway chat's ids in their place, so every later
    turn of the real session continued inside the summarizer's thread — the
    exact "thread got replaced mid-session" symptom, and it fires precisely
    when a session is long enough to matter.

    A no-op when the bridge is not running, so callers can wrap
    unconditionally.
    """
    bridge = _bridge_server
    if bridge is None:
        yield
        return
    saved = bridge.snapshot_thread_state()
    try:
        yield
    finally:
        bridge.restore_thread_state(*saved)
        logger.info("[Kim Bridge] Restored thread pointers after background side-call")


# Guards lazy construction: two concurrent first-callers would otherwise each
# build a server and race to bind port 10533, and the loser's OSError would
# surface as "extension bridge unavailable" on a perfectly healthy setup.
_bridge_lock: Optional[asyncio.Lock] = None


def _get_bridge_lock() -> asyncio.Lock:
    global _bridge_lock
    if _bridge_lock is None:
        _bridge_lock = asyncio.Lock()
    return _bridge_lock


async def get_extension_bridge() -> ExtensionBridgeServer:
    global _bridge_server
    if _bridge_server is not None:
        return _bridge_server
    async with _get_bridge_lock():
        if _bridge_server is None:
            server = ExtensionBridgeServer()
            await server.start()
            _bridge_server = server
    return _bridge_server

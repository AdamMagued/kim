"""
Extension WebSocket Bridge for Kim's Browser Provider.

Hosts a local WebSocket server on ws://127.0.0.1:10533 that bridges
Kim's BrowserProvider to the unpacked Chrome Extension (extension/content.js & injected.js).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import AsyncIterator, Callable, Optional, Dict, Any

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

    async def start(self):
        if self._runner is not None:
            return
        app = web.Application()
        app.router.add_get("/", self._ws_handler)
        app.router.add_get("/{tail:.*}", self._ws_handler)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        await site.start()
        logger.info(f"[Kim Bridge] Extension WebSocket server running on ws://127.0.0.1:{self.port}")

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()

    async def wait_for_connection(self, timeout: float = 10.0) -> bool:
        loop = asyncio.get_running_loop()
        start_time = loop.time()
        while loop.time() - start_time < timeout:
            if self.active_ws and not self.active_ws.closed:
                return True
            await asyncio.sleep(0.5)
        return False

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.active_ws = ws
        self.bridge_ready = True
        logger.info("[Kim Bridge] Chrome Extension connected!")

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except Exception:
                        continue

                    msg_type = data.get("type")
                    if msg_type in ("extension_connected", "bridge_ready"):
                        self.bridge_ready = True
                        logger.info("[Kim Bridge] Chrome Extension bridge ready!")
                    elif msg_type == "ping":
                        await ws.send_json({"type": "pong"})
                    elif msg_type == "response":
                        req_id = data.get("requestId")
                        event = data.get("event")

                        if event == "delta" and req_id in self.streaming_callbacks:
                            delta = data.get("delta", "")
                            if delta:
                                self.streaming_callbacks[req_id](delta)

                        elif event == "done" and req_id in self.pending_requests:
                            fut = self.pending_requests.get(req_id)
                            if fut and not fut.done():
                                fut.set_result(data)

                        elif event == "error" and req_id in self.pending_requests:
                            fut = self.pending_requests.get(req_id)
                            if fut and not fut.done():
                                fut.set_exception(RuntimeError(data.get("error", "Extension error")))

        except Exception as e:
            logger.warning(f"[Kim Bridge] WS exception: {e}")
        finally:
            logger.info("[Kim Bridge] Chrome Extension disconnected")
            if self.active_ws == ws:
                self.active_ws = None
                self.bridge_ready = False
        return ws

    async def send_cancel(self, req_id: str):
        """Notify the extension to stop generating text for the specified request."""
        if self.active_ws and not self.active_ws.closed:
            try:
                await self.active_ws.send_json({"type": "cancel", "requestId": req_id})
                logger.info(f"[Kim Bridge] Sent cancel signal for req_id={req_id}")
            except Exception as e:
                logger.warning(f"[Kim Bridge] Error sending cancel signal: {e}")

    async def send_completion(
        self,
        prompt: str,
        on_delta: Optional[Callable[[str], None]] = None,
        conversation_id: Optional[str] = None,
        parent_message_id: Optional[str] = None,
        timeout: float = 180.0,
    ) -> dict[str, Any]:
        if not self.active_ws or self.active_ws.closed:
            connected = await self.wait_for_connection(timeout=10.0)
            if not connected:
                raise RuntimeError("Chrome Extension is not connected to Kim Bridge! Open chatgpt.com in Chrome.")

        req_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending_requests[req_id] = fut

        if on_delta:
            self.streaming_callbacks[req_id] = on_delta

        payload = {
            "type": "request",
            "requestId": req_id,
            "messages": [{"role": "user", "content": prompt}],
            "model": "auto",
        }
        if conversation_id:
            payload["conversationId"] = conversation_id
        if parent_message_id:
            payload["parentMessageId"] = parent_message_id

        await self.active_ws.send_json(payload)

        try:
            res_data = await asyncio.wait_for(fut, timeout=timeout)
            return {
                "full_text": res_data.get("fullText", ""),
                "conversation_id": res_data.get("conversationId", ""),
                "message_id": res_data.get("messageId", ""),
            }
        except asyncio.CancelledError:
            logger.info(f"[Kim Bridge] Request {req_id} cancelled by client — stopping ChatGPT Web generation")
            await self.send_cancel(req_id)
            raise
        finally:
            self.pending_requests.pop(req_id, None)
            self.streaming_callbacks.pop(req_id, None)

_bridge_server: Optional[ExtensionBridgeServer] = None

async def get_extension_bridge() -> ExtensionBridgeServer:
    global _bridge_server
    if _bridge_server is None:
        _bridge_server = ExtensionBridgeServer()
        await _bridge_server.start()
    return _bridge_server

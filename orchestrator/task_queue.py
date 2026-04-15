"""
Task queue — handles two sources:
  1. Local asyncio queue (tasks submitted in-process, e.g. from the tray app)
  2. Remote relay server poller (GET /prompt/next every poll_interval seconds)

Usage:
    queue = TaskQueue(config)
    queue.set_agent_runner(async_fn)  # async fn(task_id, task) -> dict
    await queue.run()                 # blocks; runs both consumers
"""

import asyncio
import logging
import os
from uuid import uuid4

import httpx

logger = logging.getLogger(__name__)


class TaskQueue:
    def __init__(self, config: dict):
        relay_cfg = config.get("relay", {})
        self._relay_url: str = relay_cfg.get("url", "").rstrip("/")
        self._pc_key: str = os.environ.get(
            "RELAY_PC_API_KEY", relay_cfg.get("pc_api_key", "")
        )
        self._poll_interval: float = float(relay_cfg.get("poll_interval", 2))

        self._local_queue: asyncio.Queue = asyncio.Queue()
        self._agent_runner = None
        self._running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_agent_runner(self, fn) -> None:
        """
        Register an async callable invoked for every task.
        Signature:  async fn(task_id: str, task: str) -> dict
        Return value: {"success": bool, "summary": str, "screenshot": str}
        """
        self._agent_runner = fn

    async def submit_local(self, task: str) -> str:
        """Enqueue a task locally.  Returns the generated task_id."""
        task_id = f"local_{uuid4().hex[:8]}"
        await self._local_queue.put({"task_id": task_id, "task": task})
        logger.info(f"Local task queued: {task_id!r} — {task[:60]}")
        return task_id

    async def run(self) -> None:
        """Start consuming local queue and (optionally) polling relay."""
        self._running = True
        coroutines = [self._consume_local()]
        if self._relay_url:
            logger.info(f"Relay polling enabled: {self._relay_url}")
            coroutines.append(self._poll_relay())
        else:
            logger.info("No relay URL configured — local queue only")
        await asyncio.gather(*coroutines)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Internal consumers
    # ------------------------------------------------------------------

    async def _consume_local(self) -> None:
        logger.info("Local task queue consumer started")
        while self._running:
            try:
                item = await asyncio.wait_for(self._local_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            await self._run_item(item["task_id"], item["task"])
            self._local_queue.task_done()

    async def _poll_relay(self) -> None:
        logger.info(f"Relay poller started (interval={self._poll_interval}s)")
        async with httpx.AsyncClient(timeout=10) as client:
            while self._running:
                try:
                    resp = await client.get(
                        f"{self._relay_url}/prompt/next",
                        headers={"X-API-Key": self._pc_key},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        if data:  # non-null → there is a task
                            task_id = data["task_id"]
                            task = data["task"]
                            logger.info(f"Relay task received: {task_id!r}")
                            result = await self._run_item(task_id, task)
                            await self._post_result(client, task_id, result)
                    elif resp.status_code != 204:
                        logger.warning(f"Relay poll returned {resp.status_code}")
                except httpx.RequestError as e:
                    logger.warning(f"Relay poll failed: {e}")
                except Exception as e:
                    logger.error(f"Relay poll unexpected error: {e}", exc_info=True)
                await asyncio.sleep(self._poll_interval)

    async def _run_item(self, task_id: str, task: str) -> dict:
        if self._agent_runner is None:
            logger.error("No agent runner registered; dropping task")
            return {"success": False, "summary": "No agent runner", "screenshot": ""}
        try:
            logger.info(f"Running task {task_id!r}: {task[:80]}")
            result = await self._agent_runner(task_id, task)
            logger.info(f"Task {task_id!r} finished: success={result.get('success')}")
            return result
        except Exception as e:
            logger.error(f"Task {task_id!r} raised: {e}", exc_info=True)
            return {"success": False, "summary": f"ERROR: {e}", "screenshot": ""}

    async def _post_result(self, client: httpx.AsyncClient, task_id: str, result: dict) -> None:
        try:
            await client.post(
                f"{self._relay_url}/result",
                headers={"X-API-Key": self._pc_key},
                json={
                    "task_id": task_id,
                    "summary": result.get("summary", ""),
                    "screenshot": result.get("screenshot", ""),
                    "success": bool(result.get("success", False)),
                },
            )
        except Exception as e:
            logger.warning(f"Failed to post result for {task_id!r}: {e}")

"""
Relay worker — connects the dormant TaskQueue poller in this directory to a
real KimAgent so prompts arriving over the relay get answered by the PC.

Lifecycle is "long-lived background coroutine":

    asyncio.create_task(run_relay_worker(config, ui_bridge=bridge, voice_engine=voice))

The worker exits when:
  • `relay.url` isn't configured (logged once, returns)
  • `RELAY_PC_API_KEY` isn't set (logged warning, returns)
  • The surrounding loop is cancelled

Concurrency
───────────
Each relay task is fully run inside its own short-lived `mcp_agent_context`,
so a relay task gets the same toolset (file/shell/screen/voice) as a task
submitted from the tray. If `agent_lock` is provided, it's taken around the
run — pass the same lock the tray uses for local tasks so the two paths
never trample on each other.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional, TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # avoid heavy imports at module load time
    from orchestrator.agent import UIBridge
    from tray.voice import VoiceEngine


def _resolve_pc_key(config: dict) -> str:
    """Env wins so deployments can override without editing config.yaml."""
    env_key = os.environ.get("RELAY_PC_API_KEY", "")
    if env_key:
        return env_key
    return (config.get("relay") or {}).get("pc_api_key", "") or ""


async def run_relay_worker(
    config: dict,
    *,
    ui_bridge: Optional["UIBridge"] = None,
    voice_engine: Optional["VoiceEngine"] = None,
    agent_lock: Optional[asyncio.Lock] = None,
) -> None:
    """
    Spin up the relay poller. Returns when the relay isn't configured;
    otherwise blocks until the caller cancels.
    """
    relay_cfg = (config.get("relay") or {})
    url = (relay_cfg.get("url") or "").rstrip("/")
    if not url:
        logger.info("Relay worker disabled: relay.url is not set in config.")
        return

    pc_key = _resolve_pc_key(config)
    if not pc_key:
        logger.warning(
            "Relay worker disabled: RELAY_PC_API_KEY is not set. "
            "Add it to .env or set relay.pc_api_key in config.yaml."
        )
        return

    # Import lazily so the tray can boot even if mcp_session_context has heavy
    # transitive deps (playwright, etc.) on a stripped-down dev install.
    from orchestrator.agent import mcp_agent_context
    from orchestrator.task_queue import TaskQueue

    # TaskQueue reads RELAY_PC_API_KEY from env on construction, so it picks
    # up the same value we just validated.
    queue = TaskQueue(config)

    async def _run_one(task_id: str, task: str) -> dict:
        # Short-circuit cleanly if shutdown started between dequeue and run.
        if agent_lock is not None:
            await agent_lock.acquire()
        try:
            logger.info(f"Relay task {task_id!r} → opening agent context")
            async with mcp_agent_context(
                config,
                ui_bridge=ui_bridge,
                voice_engine=voice_engine,
            ) as agent:
                result = await agent.run(task)
            # `agent.run` returns a dict with at least "success" + "summary".
            # task_queue expects the same shape, so just normalise types.
            return {
                "success":    bool(result.get("success", False)),
                "summary":    str(result.get("summary", "") or ""),
                "screenshot": str(result.get("screenshot", "") or ""),
            }
        except asyncio.CancelledError:
            # Don't swallow — let it propagate so the queue can shut down.
            raise
        except Exception as e:
            logger.error(f"Relay task {task_id!r} raised: {e}", exc_info=True)
            return {"success": False, "summary": f"ERROR: {e}", "screenshot": ""}
        finally:
            if agent_lock is not None:
                # Lock might already be released by `acquire()` raising — guard.
                try:
                    agent_lock.release()
                except RuntimeError:
                    pass

    queue.set_agent_runner(_run_one)
    logger.info(f"Relay worker started: polling {url}")

    try:
        await queue.run()
    except asyncio.CancelledError:
        logger.info("Relay worker cancelled; shutting down.")
        queue.stop()
        raise
    except Exception as e:
        logger.error(f"Relay worker crashed: {e}", exc_info=True)
        raise

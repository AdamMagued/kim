"""
Kim autonomous agent loop.

The agent:
  1. Connects to the Kim MCP server over stdio
  2. Fetches the available tool list
  3. Builds a system prompt
  4. Enters a vision-tool loop:
       take screenshot → call LLM → execute tool (or finish)
  5. Detects stuck state (3 identical screenshots in a row)
  6. Guards against runaway loops (max_iterations)

CLI usage:
    python -m orchestrator.agent --task "open Notepad and type Hello World"
    python -m orchestrator.agent --task "..." --provider claude
    python -m orchestrator.agent --task "..." --provider browser
    python -m orchestrator.agent --task "..." --max-iter 10

Programmatic usage:
    async with mcp_agent_context(config) as agent:
        result = await agent.run("open Chrome")
"""

import argparse
import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from orchestrator.memory import ConversationMemory
from orchestrator.providers.base import BaseProvider, create_provider

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def load_config(path: Optional[str] = None) -> dict:
    cfg_path = Path(path) if path else _DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        logger.warning(f"config.yaml not found at {cfg_path}, using defaults")
        return {}
    with open(cfg_path) as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# MCP context manager
# ---------------------------------------------------------------------------

@asynccontextmanager
async def mcp_session_context(config: dict):
    """
    Async context manager that starts the Kim MCP server as a subprocess and
    yields an initialized ClientSession.
    """
    project_root = str(
        Path(
            os.environ.get("PROJECT_ROOT") or config.get("project_root", str(Path.cwd()))
        ).resolve()
    )
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server.server"],
        cwd=project_root,
    )
    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            logger.info("MCP session initialized")
            yield session


# ---------------------------------------------------------------------------
# KimAgent
# ---------------------------------------------------------------------------

class KimAgent:
    """
    One instance per task run.  Receives a live MCP session and a configured
    provider; runs the vision-tool loop until completion or guard conditions.
    """

    def __init__(
        self,
        config: dict,
        session: ClientSession,
        provider: BaseProvider,
    ):
        self.config = config
        self.session = session
        self.provider = provider
        self.max_iterations: int = int(config.get("max_iterations", 25))
        self.screenshot_scale: float = float(config.get("screenshot_scale", 0.75))
        self.memory = ConversationMemory(
            max_messages=int(config.get("memory_max_messages", 40)),
            keep_screenshots=int(config.get("memory_keep_screenshots", 4)),
        )
        self._screenshot_hashes: list[str] = []
        self._tools: list[dict] = []

    async def run(self, task: str) -> dict:
        """
        Run the agent loop for a single task.

        Returns:
            {"success": bool, "summary": str, "screenshot": str (base64)}
        """
        logger.info(f"=== Starting task: {task!r} ===")
        self.memory.clear()
        self._screenshot_hashes = []

        # Refresh tool list from MCP server
        await self._refresh_tools()
        if not self._tools:
            return {
                "success": False,
                "summary": "No MCP tools available",
                "screenshot": "",
            }

        system_prompt = self._build_system_prompt(task)

        # Initial screenshot + task → first user message
        screenshot_b64 = await self._take_screenshot()
        self.memory.add_user(
            [
                {"type": "text", "text": f"Task: {task}"},
                {"type": "image", "data": screenshot_b64, "media_type": "image/png"},
            ],
            has_screenshot=True,
        )

        last_screenshot_b64 = screenshot_b64

        for iteration in range(1, self.max_iterations + 1):
            logger.info(f"--- Iteration {iteration}/{self.max_iterations} ---")

            # Call the LLM
            try:
                response = await self.provider.complete(
                    messages=self.memory.get_messages(),
                    tools=self._tools,
                    system=system_prompt,
                )
            except Exception as e:
                logger.error(f"Provider error: {e}", exc_info=True)
                return {
                    "success": False,
                    "summary": f"LLM error: {e}",
                    "screenshot": last_screenshot_b64,
                }

            logger.debug(f"Provider response: {response}")

            # ── Tool call ────────────────────────────────────────────────
            if response["type"] == "tool_call":
                tool_name = response["tool"]
                tool_args = response.get("args", {})
                logger.info(f"Tool call: {tool_name}({json.dumps(tool_args)[:120]})")

                # Record assistant decision
                self.memory.add_assistant(json.dumps(response))

                # Execute via MCP
                result_text = await self._execute_tool(tool_name, tool_args)
                logger.info(f"Tool result ({len(result_text)} chars): {result_text[:200]}")

                # Take fresh screenshot after action
                screenshot_b64 = await self._take_screenshot()
                last_screenshot_b64 = screenshot_b64

                # Stuck detection
                if self._is_stuck(screenshot_b64) and iteration > 3:
                    logger.warning("Stuck detected — 3 identical screenshots in a row")
                    return {
                        "success": False,
                        "summary": "STUCK: Screen not changing after repeated actions. Stopping.",
                        "screenshot": screenshot_b64,
                    }

                # Tool result + fresh screenshot → next user turn
                self.memory.add_user(
                    [
                        {"type": "text", "text": f"[Tool result: {tool_name}]\n{result_text}"},
                        {"type": "image", "data": screenshot_b64, "media_type": "image/png"},
                    ],
                    has_screenshot=True,
                )
                continue

            # ── Text response ────────────────────────────────────────────
            if response["type"] == "text":
                content = str(response.get("content", "")).strip()
                self.memory.add_assistant(content)

                if content.startswith("TASK_COMPLETE:"):
                    summary = content[len("TASK_COMPLETE:"):].strip()
                    logger.info(f"Task complete: {summary}")
                    return {
                        "success": True,
                        "summary": summary,
                        "screenshot": last_screenshot_b64,
                    }

                if content.startswith("NEED_HELP:"):
                    reason = content[len("NEED_HELP:"):].strip()
                    logger.warning(f"Agent needs help: {reason}")
                    return {
                        "success": False,
                        "summary": f"NEED_HELP: {reason}",
                        "screenshot": last_screenshot_b64,
                    }

                # Plain text without a terminal signal — take a fresh screenshot
                # and prompt the LLM to continue
                logger.debug(f"Plain text response (continuing): {content[:120]}")
                screenshot_b64 = await self._take_screenshot()
                last_screenshot_b64 = screenshot_b64
                self.memory.add_user(
                    [
                        {"type": "text", "text": "Current screen. What is your next action?"},
                        {"type": "image", "data": screenshot_b64, "media_type": "image/png"},
                    ],
                    has_screenshot=True,
                )
                continue

        # Max iterations guard
        logger.warning(f"Max iterations ({self.max_iterations}) reached")
        return {
            "success": False,
            "summary": f"Reached maximum iterations ({self.max_iterations}) without completing the task.",
            "screenshot": last_screenshot_b64,
        }

    # ------------------------------------------------------------------
    # MCP helpers
    # ------------------------------------------------------------------

    async def _refresh_tools(self) -> None:
        """Fetch the tool list from the MCP server and store in canonical format."""
        result = await self.session.list_tools()
        self._tools = [
            {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.inputSchema if hasattr(t, "inputSchema") else {},
            }
            for t in result.tools
        ]
        logger.info(f"Loaded {len(self._tools)} MCP tools: {[t['name'] for t in self._tools]}")

    async def _execute_tool(self, name: str, args: dict) -> str:
        """Call a MCP tool and return its text output."""
        try:
            result = await self.session.call_tool(name=name, arguments=args)
            parts = []
            for content in result.content:
                if hasattr(content, "text"):
                    parts.append(content.text)
            return "\n".join(parts) if parts else "(no output)"
        except Exception as e:
            logger.error(f"MCP tool '{name}' failed: {e}", exc_info=True)
            return f"ERROR calling {name}: {e}"

    async def _take_screenshot(self) -> str:
        """
        Take a screenshot via MCP and return raw base64 (without data: URI prefix).
        Falls back to direct mss capture if the MCP call fails.
        """
        try:
            raw = await self._execute_tool(
                "take_screenshot", {"scale": self.screenshot_scale}
            )
            if raw.startswith("data:image/png;base64,"):
                return raw[len("data:image/png;base64,"):]
            return raw
        except Exception as e:
            logger.warning(f"MCP screenshot failed ({e}), falling back to direct capture")
            return _direct_screenshot(self.screenshot_scale)

    # ------------------------------------------------------------------
    # Stuck detection
    # ------------------------------------------------------------------

    def _is_stuck(self, screenshot_b64: str) -> bool:
        """Return True if the last 3 screenshots are all identical."""
        h = hashlib.md5(screenshot_b64[:4096].encode()).hexdigest()
        self._screenshot_hashes.append(h)
        if len(self._screenshot_hashes) > 3:
            self._screenshot_hashes.pop(0)
        return len(self._screenshot_hashes) == 3 and len(set(self._screenshot_hashes)) == 1

    # ------------------------------------------------------------------
    # System prompt
    # ------------------------------------------------------------------

    def _build_system_prompt(self, task: str) -> str:
        tool_names = [t["name"] for t in self._tools]
        return f"""You are Kim, an autonomous AI agent controlling a Windows PC.

## Current Task
{task}

## Available MCP Tools
{json.dumps(tool_names, indent=2)}

Full tool schemas are provided in the `tools` parameter of each API call.

## Response Rules
You MUST respond in EXACTLY one of these formats on every turn:

1. **Tool call** (JSON, no markdown, no extra text):
   {{"tool": "<tool_name>", "args": {{<arguments>}}}}

2. **Task complete**:
   TASK_COMPLETE: <one-sentence summary of what was accomplished>

3. **Need human help**:
   NEED_HELP: <brief reason you cannot proceed autonomously>

## Operational Guidelines
- Always examine the screenshot before deciding what to do next.
- After every click or keyboard action, wait for a new screenshot to verify the result.
- Prefer run_command for launching apps (e.g. `start notepad.exe`).
- Use focus_window before typing into an application.
- If an action fails, try an alternative approach before giving up.
- Maximum {self.max_iterations} iterations are allowed.
"""


# ---------------------------------------------------------------------------
# Fallback direct screenshot (no MCP)
# ---------------------------------------------------------------------------

def _direct_screenshot(scale: float = 0.75) -> str:
    import mss
    from PIL import Image

    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[1])
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if scale != 1.0:
        img = img.resize(
            (int(img.width * scale), int(img.height * scale)), Image.LANCZOS
        )
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------------------
# Convenience context manager for external callers
# ---------------------------------------------------------------------------

@asynccontextmanager
async def mcp_agent_context(config: dict, provider_name: Optional[str] = None):
    """
    Yields a KimAgent ready to run tasks.

    Example:
        async with mcp_agent_context(config) as agent:
            result = await agent.run("open Notepad")
    """
    name = provider_name or config.get("provider", "claude")
    provider = create_provider(name, config)
    async with mcp_session_context(config) as session:
        yield KimAgent(config=config, session=session, provider=provider)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

async def _cli_main(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    if args.provider:
        config["provider"] = args.provider
    if args.max_iter:
        config["max_iterations"] = args.max_iter

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    task = args.task
    if not task:
        task = input("Task: ").strip()

    print(f"Running task: {task!r}  provider={config.get('provider', 'claude')}", file=sys.stderr)

    async with mcp_agent_context(config) as agent:
        result = await agent.run(task)

    status = "SUCCESS" if result["success"] else "FAILED"
    print(f"\n[{status}] {result['summary']}")


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m orchestrator.agent",
        description="Kim — autonomous AI agent",
    )
    p.add_argument("--task", "-t", help="Task to execute")
    p.add_argument(
        "--provider", "-p",
        choices=["claude", "openai", "gemini", "deepseek", "browser"],
        help="Override provider from config.yaml",
    )
    p.add_argument("--config", "-c", help="Path to config.yaml")
    p.add_argument("--max-iter", type=int, help="Override max_iterations")
    p.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    return p


if __name__ == "__main__":
    parser = _build_arg_parser()
    cli_args = parser.parse_args()
    asyncio.run(_cli_main(cli_args))

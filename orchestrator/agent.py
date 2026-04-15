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
  7. Optionally pauses before every tool call for user confirmation (preview mode)

UIBridge
────────
KimAgent accepts an optional UIBridge that wires the async agent to a Tkinter
UI without any hard dependency on tkinter.  When no bridge is attached the
agent behaves identically to the CLI-only version.

CLI usage:
    python -m orchestrator.agent --task "open Notepad and type Hello World"
    python -m orchestrator.agent --task "..." --provider claude
    python -m orchestrator.agent --task "..." --provider browser
    python -m orchestrator.agent --task "..." --max-iter 10

Programmatic usage:
    async with mcp_agent_context(config) as agent:
        agent.set_ui_bridge(bridge)
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
import queue
import sys
import threading
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
# UIBridge — thread-safe channel between async agent and Tkinter UI
# ---------------------------------------------------------------------------

class UIBridge:
    """
    Connects the async KimAgent to a Tkinter UI (or any consumer) without
    coupling the agent to any UI framework.

    Thread safety
    ─────────────
    All public methods are safe to call from any thread.
    `confirm_action()` is async and must be awaited from the agent coroutine.
    """

    def __init__(self) -> None:
        # Log records → UI log window
        self.log_queue: queue.Queue = queue.Queue()
        # Latest screenshot (b64, no data: prefix) → UI thumbnail
        self.screenshot_queue: queue.Queue = queue.Queue(maxsize=1)
        # Confirmation requests: (tool_name, args, threading.Event, [bool])
        self._confirm_queue: queue.Queue = queue.Queue()
        # Cancellation flag — set by UI Stop button; checked by agent each iter
        self.cancelled: bool = False
        # Live toggle — UI checkbox sets this; agent reads it each iteration
        self.preview_mode: bool = False

    # ── Logging ────────────────────────────────────────────────────────────

    def log(self, level: str, message: str) -> None:
        """Put a (level, message) tuple for the UI to render."""
        self.log_queue.put_nowait((level.upper(), message))

    # ── Screenshot ─────────────────────────────────────────────────────────

    def update_screenshot(self, b64: str) -> None:
        """Replace the pending screenshot with the latest one (drop oldest)."""
        try:
            self.screenshot_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self.screenshot_queue.put_nowait(b64)
        except queue.Full:
            pass

    # ── Confirmation (preview mode) ────────────────────────────────────────

    async def confirm_action(self, tool_name: str, args: dict) -> bool:
        """
        Pause execution and ask the UI for confirmation.
        If cancelled, returns False immediately.
        If the UI takes > 60 s (or no UI is attached), auto-allows.
        """
        if self.cancelled:
            return False
        event: threading.Event = threading.Event()
        result: list[bool] = [True]
        self._confirm_queue.put_nowait((tool_name, args, event, result))
        # Wait without blocking the asyncio event loop
        loop = asyncio.get_event_loop()
        timed_out = not await loop.run_in_executor(None, lambda: event.wait(timeout=60.0))
        if timed_out:
            logger.warning("Confirmation timed out after 60 s — auto-allowing")
        return result[0]

    def resolve_confirm(
        self, event: threading.Event, result: list[bool], confirmed: bool
    ) -> None:
        """Called by the UI when the user clicks Confirm or Deny."""
        result[0] = confirmed
        event.set()

    # ── Cancel ─────────────────────────────────────────────────────────────

    def cancel(self) -> None:
        """Request agent stop.  Also unblocks any pending confirmation."""
        self.cancelled = True
        # Drain and deny any queued confirm requests
        while True:
            try:
                _, _, event, result = self._confirm_queue.get_nowait()
                result[0] = False
                event.set()
            except queue.Empty:
                break

    def reset(self) -> None:
        """Call before submitting a new task."""
        self.cancelled = False


# ---------------------------------------------------------------------------
# UIBridge logging handler — routes Python log records to the UI
# ---------------------------------------------------------------------------

class UIBridgeLogHandler(logging.Handler):
    """Attach to any logger to mirror records into the UIBridge log queue."""

    def __init__(self, bridge: UIBridge) -> None:
        super().__init__()
        self._bridge = bridge

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._bridge.log(record.levelname, msg)
        except Exception:
            pass


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
    Vision-tool agent loop.  Receives a live MCP session and a configured
    provider.  Optionally wired to a UIBridge for live UI updates.
    """

    def __init__(
        self,
        config: dict,
        session: ClientSession,
        provider: BaseProvider,
        ui_bridge: Optional[UIBridge] = None,
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
        self._ui_bridge: Optional[UIBridge] = ui_bridge

    def set_ui_bridge(self, bridge: Optional[UIBridge]) -> None:
        self._ui_bridge = bridge

    # ------------------------------------------------------------------
    # Helpers that are UI-aware
    # ------------------------------------------------------------------

    def _log(self, level: str, message: str) -> None:
        """Log to Python logger AND UIBridge (if attached)."""
        getattr(logger, level.lower(), logger.info)(message)
        if self._ui_bridge:
            self._ui_bridge.log(level, message)

    def _is_preview_mode(self) -> bool:
        if self._ui_bridge is not None:
            return self._ui_bridge.preview_mode
        return bool(self.config.get("preview_mode", False))

    def _is_cancelled(self) -> bool:
        return bool(self._ui_bridge and self._ui_bridge.cancelled)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self, task: str) -> dict:
        """
        Run the agent loop for a single task.

        Returns:
            {"success": bool, "summary": str, "screenshot": str (base64)}
        """
        self._log("INFO", f"=== Starting task: {task!r} ===")
        self.memory.clear()
        self._screenshot_hashes = []

        await self._refresh_tools()
        if not self._tools:
            return {"success": False, "summary": "No MCP tools available", "screenshot": ""}

        system_prompt = self._build_system_prompt(task)

        screenshot_b64 = await self._take_screenshot()
        if self._ui_bridge:
            self._ui_bridge.update_screenshot(screenshot_b64)

        self.memory.add_user(
            [
                {"type": "text", "text": f"Task: {task}"},
                {"type": "image", "data": screenshot_b64, "media_type": "image/png"},
            ],
            has_screenshot=True,
        )

        last_screenshot_b64 = screenshot_b64

        for iteration in range(1, self.max_iterations + 1):
            # ── Cancellation check ───────────────────────────────────────
            if self._is_cancelled():
                self._log("WARN", "Task cancelled by user")
                return {"success": False, "summary": "Cancelled by user", "screenshot": last_screenshot_b64}

            self._log("INFO", f"--- Iteration {iteration}/{self.max_iterations} ---")

            # ── LLM call ─────────────────────────────────────────────────
            try:
                response = await self.provider.complete(
                    messages=self.memory.get_messages(),
                    tools=self._tools,
                    system=system_prompt,
                )
            except Exception as e:
                self._log("ERROR", f"Provider error: {e}")
                return {"success": False, "summary": f"LLM error: {e}", "screenshot": last_screenshot_b64}

            # ── Tool call ─────────────────────────────────────────────────
            if response["type"] == "tool_call":
                tool_name = response["tool"]
                tool_args = response.get("args", {})
                self._log("TOOL", f"{tool_name}({json.dumps(tool_args)[:120]})")

                # Preview mode — pause and ask for confirmation
                if self._is_preview_mode() and self._ui_bridge:
                    self._log("INFO", f"[Preview] Waiting for confirmation: {tool_name}")
                    confirmed = await self._ui_bridge.confirm_action(tool_name, tool_args)
                    if not confirmed:
                        self._log("WARN", f"Action denied by user: {tool_name}")
                        self.memory.add_user(
                            f"[User denied the action: {tool_name}]. "
                            "Choose a different approach that does not require this action."
                        )
                        continue

                self.memory.add_assistant(json.dumps(response))

                # Execute via MCP
                result_text = await self._execute_tool(tool_name, tool_args)
                self._log("INFO", f"Result: {result_text[:200]}")

                # Fresh screenshot after action
                screenshot_b64 = await self._take_screenshot()
                last_screenshot_b64 = screenshot_b64
                if self._ui_bridge:
                    self._ui_bridge.update_screenshot(screenshot_b64)

                # Stuck detection
                if self._is_stuck(screenshot_b64) and iteration > 3:
                    self._log("WARN", "Stuck — 3 identical screenshots in a row. Stopping.")
                    return {
                        "success": False,
                        "summary": "STUCK: Screen not changing after repeated actions.",
                        "screenshot": screenshot_b64,
                    }

                self.memory.add_user(
                    [
                        {"type": "text", "text": f"[Tool result: {tool_name}]\n{result_text}"},
                        {"type": "image", "data": screenshot_b64, "media_type": "image/png"},
                    ],
                    has_screenshot=True,
                )
                continue

            # ── Text response ─────────────────────────────────────────────
            if response["type"] == "text":
                content = str(response.get("content", "")).strip()
                self.memory.add_assistant(content)

                if content.startswith("TASK_COMPLETE:"):
                    summary = content[len("TASK_COMPLETE:"):].strip()
                    self._log("INFO", f"TASK_COMPLETE: {summary}")
                    return {"success": True, "summary": summary, "screenshot": last_screenshot_b64}

                if content.startswith("NEED_HELP:"):
                    reason = content[len("NEED_HELP:"):].strip()
                    self._log("WARN", f"NEED_HELP: {reason}")
                    return {"success": False, "summary": f"NEED_HELP: {reason}", "screenshot": last_screenshot_b64}

                self._log("DEBUG", f"Text (continuing): {content[:120]}")
                screenshot_b64 = await self._take_screenshot()
                last_screenshot_b64 = screenshot_b64
                if self._ui_bridge:
                    self._ui_bridge.update_screenshot(screenshot_b64)
                self.memory.add_user(
                    [
                        {"type": "text", "text": "Current screen. What is your next action?"},
                        {"type": "image", "data": screenshot_b64, "media_type": "image/png"},
                    ],
                    has_screenshot=True,
                )
                continue

        self._log("WARN", f"Max iterations ({self.max_iterations}) reached")
        return {
            "success": False,
            "summary": f"Reached maximum iterations ({self.max_iterations}) without completing.",
            "screenshot": last_screenshot_b64,
        }

    # ------------------------------------------------------------------
    # MCP helpers
    # ------------------------------------------------------------------

    async def _refresh_tools(self) -> None:
        result = await self.session.list_tools()
        self._tools = [
            {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.inputSchema if hasattr(t, "inputSchema") else {},
            }
            for t in result.tools
        ]
        self._log("INFO", f"Loaded {len(self._tools)} MCP tools")

    async def _execute_tool(self, name: str, args: dict) -> str:
        try:
            result = await self.session.call_tool(name=name, arguments=args)
            parts = [c.text for c in result.content if hasattr(c, "text")]
            return "\n".join(parts) if parts else "(no output)"
        except Exception as e:
            logger.error(f"MCP tool '{name}' failed: {e}", exc_info=True)
            return f"ERROR calling {name}: {e}"

    async def _take_screenshot(self) -> str:
        try:
            raw = await self._execute_tool("take_screenshot", {"scale": self.screenshot_scale})
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
- After every click or keyboard action, verify the result in the next screenshot.
- Prefer run_command for launching apps (e.g. `start notepad.exe`).
- Use focus_window before typing into an application.
- Maximum {self.max_iterations} iterations are allowed.
"""


# ---------------------------------------------------------------------------
# Fallback direct screenshot
# ---------------------------------------------------------------------------

def _direct_screenshot(scale: float = 0.75) -> str:
    import mss
    from PIL import Image

    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[1])
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if scale != 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------------------
# Convenience context manager
# ---------------------------------------------------------------------------

@asynccontextmanager
async def mcp_agent_context(
    config: dict,
    provider_name: Optional[str] = None,
    ui_bridge: Optional[UIBridge] = None,
):
    """
    Yields a KimAgent ready to run tasks.

        async with mcp_agent_context(config, ui_bridge=bridge) as agent:
            result = await agent.run("open Notepad")
    """
    name = provider_name or config.get("provider", "claude")
    provider = create_provider(name, config)
    async with mcp_session_context(config) as session:
        yield KimAgent(config=config, session=session, provider=provider, ui_bridge=ui_bridge)


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

    task = args.task or input("Task: ").strip()
    print(f"Running: {task!r}  provider={config.get('provider', 'claude')}", file=sys.stderr)

    async with mcp_agent_context(config) as agent:
        result = await agent.run(task)

    status = "SUCCESS" if result["success"] else "FAILED"
    print(f"\n[{status}] {result['summary']}")


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m orchestrator.agent", description="Kim — autonomous AI agent")
    p.add_argument("--task", "-t", help="Task to execute")
    p.add_argument("--provider", "-p", choices=["claude", "openai", "gemini", "deepseek", "browser"])
    p.add_argument("--config", "-c", help="Path to config.yaml")
    p.add_argument("--max-iter", type=int)
    p.add_argument("--verbose", "-v", action="store_true")
    return p


if __name__ == "__main__":
    asyncio.run(_cli_main(_build_arg_parser().parse_args()))

"""
Kim autonomous agent loop.

The agent:
  1. Connects to the Kim MCP server over stdio
  2. Fetches the available tool list
  3. Builds a system prompt
  4. Enters a vision-tool loop:
       take screenshot -> call LLM -> execute tool (or finish)
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
import platform
import queue
import random
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, TYPE_CHECKING

import yaml
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from orchestrator.memory import ConversationMemory
from orchestrator.providers.base import BaseProvider, create_provider
from orchestrator.session_store import SessionStore
from orchestrator.context_loader import discover_instruction_files, build_instruction_prompt

if TYPE_CHECKING:
    from tray.voice import VoiceEngine

load_dotenv()

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OS detection (used by system prompt and operational guidelines)
# ---------------------------------------------------------------------------

def _detect_os() -> tuple[str, str, str]:
    """Return (os_display_name, launch_example, path_style)."""
    system = platform.system()
    if system == "Darwin":
        return (
            "macOS",
            "`open -a 'TextEdit'`",
            "POSIX paths (e.g. /Users/...)",
        )
    elif system == "Linux":
        return (
            "Linux",
            "`xdg-open` or `gedit`",
            "POSIX paths (e.g. /home/...)",
        )
    else:
        return (
            "Windows",
            "`start notepad.exe`",
            "Windows paths (e.g. C:\\...)",
        )


_OS_NAME, _LAUNCH_EXAMPLE, _PATH_STYLE = _detect_os()


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
        # Log records -> UI log window
        self.log_queue: queue.Queue = queue.Queue()
        # Confirmation requests: (tool_name, args, threading.Event, [bool])
        self._confirm_queue: queue.Queue = queue.Queue()
        # Hide/show requests for screenshot blink: ("hide"|"show", threading.Event)
        self._visibility_queue: queue.Queue = queue.Queue()
        # Cancellation — thread-safe Event instead of bare bool
        self._cancelled = threading.Event()
        # Live toggle — UI checkbox sets this; agent reads it each iteration
        self.preview_mode: bool = False

    # ── Cancellation (property for backward compatibility) ────────────

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    # ── Logging ────────────────────────────────────────────────────────

    def log(self, level: str, message: str) -> None:
        """Put a (level, message) tuple for the UI to render."""
        self.log_queue.put_nowait((level.upper(), message))

    # ── Window visibility (screenshot blink) ──────────────────────────

    async def hide_for_screenshot(self) -> None:
        """Ask the UI to hide all Kim windows.  Waits up to 0.5 s."""
        event = threading.Event()
        self._visibility_queue.put_nowait(("hide", event))
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: event.wait(timeout=0.5))

    async def show_after_screenshot(self) -> None:
        """Ask the UI to restore all Kim windows.  Waits up to 0.5 s."""
        event = threading.Event()
        self._visibility_queue.put_nowait(("show", event))
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: event.wait(timeout=0.5))

    # ── Confirmation (preview mode) ───────────────────────────────────

    async def confirm_action(self, tool_name: str, args: dict) -> bool:
        """
        Pause execution and ask the UI for confirmation.
        If cancelled, returns False immediately.
        If the UI takes > 60 s (or no UI is attached), auto-allows.
        """
        if self._cancelled.is_set():
            return False
        event: threading.Event = threading.Event()
        result: list[bool] = [True]
        self._confirm_queue.put_nowait((tool_name, args, event, result))
        # Wait without blocking the asyncio event loop
        loop = asyncio.get_running_loop()
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

    # ── Cancel ────────────────────────────────────────────────────────

    def cancel(self) -> None:
        """Request agent stop.  Also unblocks any pending confirmation."""
        self._cancelled.set()
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
        self._cancelled.clear()
        # Drain any stale visibility requests
        while not self._visibility_queue.empty():
            try:
                _, event = self._visibility_queue.get_nowait()
                event.set()
            except queue.Empty:
                break


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
            try:
                await asyncio.wait_for(session.initialize(), timeout=30.0)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    "MCP server did not respond within 30 seconds. "
                    "Ensure the mcp_server package is installed and importable."
                )
            logger.info("MCP session initialized")
            yield session


# ---------------------------------------------------------------------------
# KimAgent
# ---------------------------------------------------------------------------

class KimAgent:
    """
    Vision-tool agent loop.  Receives a live MCP session and a configured
    provider.  Optionally wired to a UIBridge for live UI updates.
    Optionally speaks via VoiceEngine when voice_enabled is True.
    """

    def __init__(
        self,
        config: dict,
        session: ClientSession,
        provider: BaseProvider,
        ui_bridge: Optional[UIBridge] = None,
        voice_engine: Optional["VoiceEngine"] = None,
        session_store: Optional[SessionStore] = None,
        resume_session_id: Optional[str] = None,
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
        self._voice = voice_engine
        self._session_store = session_store or SessionStore()
        self._resume_session_id = resume_session_id
        # Retry configuration for LLM API calls
        self._max_retries: int = int(config.get("max_retries", 5))
        self._retry_base_delay: float = float(config.get("retry_base_delay", 1.0))
        self._retry_max_delay: float = float(config.get("retry_max_delay", 60.0))
        # Token usage tracking
        self._total_tokens: dict = {"input": 0, "output": 0}

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

    async def _voice_speak(self, text: str) -> None:
        """Speak text via VoiceEngine if available and enabled.
        Uses fire-and-forget so audio plays in the background without
        blocking tool execution.  Skips JSON / technical output."""
        if self._voice and self._voice.enabled:
            # Filter out raw JSON and technical output
            stripped = text.strip()
            if (
                stripped.startswith("{")
                or stripped.startswith("[")
                or "'success':" in stripped
                or '"success":' in stripped
                or stripped.startswith("ERROR")
                or stripped.startswith("data:image/")
            ):
                return
            try:
                self._voice.speak_fire_and_forget(text)
            except Exception as e:
                logger.debug(f"Voice speak failed: {e}")

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
        self._screenshot_hashes = []

        # Let the provider reset any per-session state (e.g. BrowserProvider
        # clears _sent_system_prompt so the new task gets its system prompt).
        if hasattr(self.provider, "reset_session"):
            self.provider.reset_session()

        # Resume from saved session or start fresh
        if self._resume_session_id:
            saved = SessionStore.load_session(
                self._resume_session_id,
                base_dir=self._session_store.base_dir,
            )
            if saved:
                self._log("INFO", f"Resuming session {self._resume_session_id} ({len(saved)} messages)")
                self.memory.load_from_messages(saved)
            else:
                self._log("WARN", f"Session {self._resume_session_id} not found — starting fresh")
                self.memory.clear()
        else:
            self.memory.clear()

        await self._refresh_tools()
        if not self._tools:
            return {"success": False, "summary": "No MCP tools available", "screenshot": ""}

        system_prompt = self._build_system_prompt(task)

        first_msg = {"role": "user", "content": f"Task: {task}"}
        self.memory.add_user(f"Task: {task}")
        self._session_store.append_message(first_msg)

        last_screenshot_b64 = ""

        for iteration in range(1, self.max_iterations + 1):
            # ── Cancellation check ──────────────────────────────────────
            if self._is_cancelled():
                self._log("WARN", "Task cancelled by user")
                return {"success": False, "summary": "Cancelled by user", "screenshot": last_screenshot_b64}

            self._log("INFO", f"--- Iteration {iteration}/{self.max_iterations} ---")

            # ── LLM call with retry ─────────────────────────────────────
            try:
                response = await self._call_with_retry(
                    messages=self.memory.get_messages(),
                    tools=self._tools,
                    system=system_prompt,
                )
            except Exception as e:
                self._log("ERROR", f"Provider error (all retries exhausted): {e}")
                return {"success": False, "summary": f"LLM error: {e}", "screenshot": last_screenshot_b64}

            # ── Track token usage ────────────────────────────────────────
            usage = response.get("usage", {})
            if usage:
                self._total_tokens["input"] += usage.get("input", 0)
                self._total_tokens["output"] += usage.get("output", 0)
                total = self._total_tokens["input"] + self._total_tokens["output"]
                self._log(
                    "INFO",
                    f"[STATS] input_tokens={usage.get('input', 0)}"
                    f" output_tokens={usage.get('output', 0)}"
                    f" total_tokens={total}",
                )

            # ── Tool call ────────────────────────────────────────────────
            if response["type"] == "tool_call":
                tool_name = response["tool"]
                tool_args = response.get("args", {})
                self._log("TOOL", f"{tool_name}({json.dumps(tool_args)[:120]})")
                await self._voice_speak(f"Running {tool_name}")

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

                assistant_msg = {"role": "assistant", "content": json.dumps(response)}
                self.memory.add_assistant(json.dumps(response))
                self._session_store.append_message(assistant_msg)

                # Execute via MCP
                result_text = await self._execute_tool(tool_name, tool_args)
                self._log("INFO", f"Result: {result_text[:200]}")

                # Fresh screenshot after action
                screenshot_b64 = await self._take_screenshot()
                last_screenshot_b64 = screenshot_b64

                # Stuck detection
                if self._is_stuck(screenshot_b64) and iteration > 3:
                    self._log("WARN", "Stuck — 3 identical screenshots in a row. Stopping.")
                    await self._voice_speak("I appear to be stuck. The screen is not changing.")
                    return {
                        "success": False,
                        "summary": "STUCK: Screen not changing after repeated actions.",
                        "screenshot": screenshot_b64,
                    }

                user_content = [
                    {"type": "text", "text": f"[Tool result: {tool_name}]\n{result_text}"},
                    {"type": "image", "data": screenshot_b64, "media_type": "image/png"},
                ]
                self.memory.add_user(user_content, has_screenshot=True)
                self._session_store.append_message({"role": "user", "content": user_content})
                continue

            # ── Text response ────────────────────────────────────────────
            if response["type"] == "text":
                content = str(response.get("content", "")).strip()
                self.memory.add_assistant(content)
                self._session_store.append_message({"role": "assistant", "content": content})

                if content.startswith("TASK_COMPLETE:"):
                    summary = content[len("TASK_COMPLETE:"):].strip()
                    self._log("DEBUG", f"TASK_COMPLETE: {summary}")
                    await self._generate_and_save_summary(task, summary)
                    return {"success": True, "summary": summary, "screenshot": last_screenshot_b64}

                if content.startswith("NEED_HELP:"):
                    reason = content[len("NEED_HELP:"):].strip()
                    self._log("DEBUG", f"NEED_HELP: {reason}")
                    return {"success": False, "summary": f"NEED_HELP: {reason}", "screenshot": last_screenshot_b64}

                self._log("DEBUG", f"Text (continuing): {content[:120]}")
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
        import time as _time

        # ── Pre-execution: capture file state for diff ───────────────────
        _file_path: Optional[str] = None
        _before_lines: int = 0
        _write_ops = {"write_file", "create_file", "edit_file", "append_file"}
        if name in _write_ops:
            _file_path = args.get("path") or args.get("file_path")
            if _file_path:
                try:
                    with open(_file_path, "r", encoding="utf-8", errors="ignore") as _f:
                        _before_lines = sum(1 for _ in _f)
                except (OSError, IOError):
                    _before_lines = 0

        t0 = _time.monotonic()

        try:
            result = await self.session.call_tool(name=name, arguments=args)
            parts = [c.text for c in result.content if hasattr(c, "text")]
            output = "\n".join(parts) if parts else "(no output)"
        except Exception as e:
            logger.error(f"MCP tool '{name}' failed: {e}", exc_info=True)
            return f"ERROR calling {name}: {e}"

        duration_ms = int((_time.monotonic() - t0) * 1000)

        # ── Post-execution: emit line diff for file writes ───────────────
        if _file_path and name in _write_ops:
            try:
                with open(_file_path, "r", encoding="utf-8", errors="ignore") as _f:
                    after_lines = sum(1 for _ in _f)
                added = max(0, after_lines - _before_lines)
                removed = max(0, _before_lines - after_lines)
                import os as _os
                basename = _os.path.basename(_file_path)
                self._log("INFO", f"[DIFF] path={basename} +{added} -{removed} duration_ms={duration_ms}")
            except (OSError, IOError):
                pass

        return output

    async def _take_screenshot(self) -> str:
        """Take a screenshot, blinking the Kim UI off and back on so it
        doesn't appear in the capture."""
        # Hide
        if self._ui_bridge:
            try:
                await self._ui_bridge.hide_for_screenshot()
            except Exception:
                pass
            await asyncio.sleep(0.05)  # let the window manager process

        try:
            raw = await self._execute_tool("take_screenshot", {"scale": self.screenshot_scale})
            if raw.startswith("data:image/png;base64,"):
                return raw[len("data:image/png;base64,"):]
            return raw
        except Exception as e:
            logger.warning(f"MCP screenshot failed ({e}), falling back to direct capture")
            return _direct_screenshot(self.screenshot_scale)
        finally:
            # Always restore
            if self._ui_bridge:
                try:
                    await self._ui_bridge.show_after_screenshot()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Stuck detection
    # ------------------------------------------------------------------

    def _is_stuck(self, screenshot_b64: str) -> bool:
        """Return True if the last 3 screenshots are identical (MD5 of full b64)."""
        h = hashlib.md5(screenshot_b64.encode()).hexdigest()
        self._screenshot_hashes.append(h)
        if len(self._screenshot_hashes) > 3:
            self._screenshot_hashes.pop(0)
        if len(self._screenshot_hashes) == 3 and len(set(self._screenshot_hashes)) == 1:
            self._log("DEBUG", f"Stuck check: 3 identical hashes ({h[:12]}...)")
            return True
        return False

    # ------------------------------------------------------------------
    # LLM retry with exponential backoff
    # ------------------------------------------------------------------

    async def _call_with_retry(
        self,
        messages: list,
        tools: list,
        system: str,
    ) -> dict:
        """
        Call the LLM provider with retry + exponential backoff for:
          - HTTP 429 (Rate Limit)
          - HTTP 5xx (Server errors)
          - ConnectionError / TimeoutError

        Non-retryable errors (auth, invalid request) are raised immediately.
        """
        last_error = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return await self.provider.complete(
                    messages=messages,
                    tools=tools,
                    system=system,
                )
            except Exception as e:
                last_error = e
                if not self._is_retryable(e):
                    raise

                delay = min(
                    self._retry_base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1),
                    self._retry_max_delay,
                )
                self._log(
                    "WARN",
                    f"LLM call failed (attempt {attempt}/{self._max_retries}): "
                    f"{type(e).__name__}: {e} — retrying in {delay:.1f}s",
                )
                await asyncio.sleep(delay)

        raise last_error  # type: ignore[misc]

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        """Determine if an LLM error is worth retrying."""
        error_str = str(error).lower()
        error_type = type(error).__name__.lower()

        # Rate limit errors (HTTP 429)
        if "rate" in error_str and "limit" in error_str:
            return True
        if "429" in error_str:
            return True
        if "ratelimit" in error_type:
            return True

        # Server errors (HTTP 5xx) — match only standalone status codes,
        # not substrings like '500' inside file names or other numbers.
        import re as _re_retry
        for code in ("500", "502", "503", "529"):
            if _re_retry.search(r'(?<![\d])' + code + r'(?![\d])', error_str):
                return True
        if "server" in error_str and "error" in error_str:
            return True
        if "overloaded" in error_str:
            return True

        # Network / timeout errors
        if isinstance(error, (ConnectionError, TimeoutError, OSError)):
            return True
        if "timeout" in error_str or "connection" in error_str:
            return True

        return False

    # ------------------------------------------------------------------
    # System prompt
    # ------------------------------------------------------------------

    def _build_system_prompt(self, task: str) -> str:
        tool_names = [t["name"] for t in self._tools]
        prompt = f"""You are Kim, an autonomous AI agent controlling a {_OS_NAME} computer.

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
- Prefer run_command for launching apps (e.g. {_LAUNCH_EXAMPLE}).
- Use {_PATH_STYLE}.
- Use focus_window before typing into an application.
- Maximum {self.max_iterations} iterations are allowed.
"""
        if self.config.get("voice", {}).get("human_quirks", False):
            prompt += (
                "\n## Voice Directives\n"
                "You are speaking aloud. You MUST use conversational fillers "
                "(like 'Hmm...', 'Let\'s see...', 'Umm', 'Alright'). "
                "Speak casually, use short punchy sentences, and sound like a "
                "human peer thinking out loud. Avoid sounding like a formal AI assistant.\n"
            )

        # Inject KIM.md project instructions
        instruction_files = discover_instruction_files()
        instructions_section = build_instruction_prompt(instruction_files)
        if instructions_section:
            prompt += "\n" + instructions_section + "\n"

        # Inject recent session context
        recent = SessionStore.recent_summaries(count=3)
        if recent:
            prompt += "\n# Recent context\nSummaries of your most recent sessions:\n"
            for entry in recent:
                prompt += f"- [{entry['date']}] {entry['summary']}\n"
            prompt += "\n"

        return prompt

    async def _generate_and_save_summary(self, task: str, result_summary: str) -> None:
        """Ask the LLM for a 1-paragraph session summary and save to disk."""
        try:
            summary_prompt = (
                f"Write a single paragraph (3-4 sentences) summarizing this session. "
                f"Task: {task}\nOutcome: {result_summary}\n\n"
                f"Focus on what was done, what tools were used, and the final result. "
                f"Write in past tense, third person. No markdown. Plain text only."
            )
            response = await self.provider.complete(
                messages=[{"role": "user", "content": summary_prompt}],
                tools=[],
                system="You are a session summarizer. Output ONLY the summary paragraph, nothing else.",
            )
            summary_text = str(response.get("content", result_summary)).strip()
            # Fallback: if the LLM returned a tool call instead of text
            if not summary_text or response.get("type") != "text":
                summary_text = f"Task: {task}. Result: {result_summary}"
            self._session_store.save_summary(summary_text)
        except Exception as e:
            logger.warning(f"Failed to generate session summary: {e}")
            # Save a basic summary as fallback
            self._session_store.save_summary(f"Task: {task}. Result: {result_summary}")


# ---------------------------------------------------------------------------
# Fallback direct screenshot
# ---------------------------------------------------------------------------

def _direct_screenshot(scale: float = 0.75) -> str:
    import mss
    from PIL import Image

    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[1])
        img = Image.frombytes("RGB", shot.size, shot.rgb)
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
    voice_engine: Optional["VoiceEngine"] = None,
    resume_session_id: Optional[str] = None,
):
    """
    Yields a KimAgent ready to run tasks.

        async with mcp_agent_context(config, ui_bridge=bridge) as agent:
            result = await agent.run("open Notepad")
    """
    name = provider_name or config.get("provider", "claude")
    provider = create_provider(name, config)

    # Auto-create VoiceEngine if voice enabled and none provided
    _voice = voice_engine
    if _voice is None:
        voice_cfg = config.get("voice", {})
        voice_enabled = voice_cfg.get("enabled", config.get("voice_enabled", False))
        if voice_enabled:
            try:
                from tray.voice import VoiceEngine as _VE
                _voice = _VE(config)
            except ImportError:
                logger.debug("tray.voice not available — voice disabled")

    async with mcp_session_context(config) as session:
        store = SessionStore(session_id=resume_session_id) if resume_session_id else SessionStore()
        agent = KimAgent(
            config=config, session=session, provider=provider,
            ui_bridge=ui_bridge, voice_engine=_voice,
            session_store=store,
            resume_session_id=resume_session_id,
        )
        try:
            yield agent
        finally:
            if _voice and voice_engine is None:
                _voice.shutdown()


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

    async with mcp_agent_context(
        config,
        resume_session_id=args.resume,
    ) as agent:
        result = await agent.run(task)

    status = "SUCCESS" if result["success"] else "FAILED"
    print(f"\n[{status}] {result['summary']}")


def _cli_provider_type(value: str) -> str:
    """Allow `browser:claude` / `browser:chatgpt` (desktop) as well as plain provider names."""
    s = (value or "").strip().lower()
    base = {"claude", "openai", "gemini", "deepseek", "browser"}
    if s in base:
        return s
    if s.startswith("browser:") and len(s) > len("browser:"):
        return s
    raise argparse.ArgumentTypeError(
        f"unknown provider {value!r}; use claude, openai, gemini, deepseek, browser, "
        "or browser:<site> (e.g. browser:chatgpt)"
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m orchestrator.agent", description="Kim — autonomous AI agent")
    p.add_argument("--task", "-t", help="Task to execute")
    p.add_argument("--provider", "-p", type=_cli_provider_type, metavar="NAME")
    p.add_argument("--config", "-c", help="Path to config.yaml")
    p.add_argument("--max-iter", type=int)
    p.add_argument("--resume", "-r", metavar="SESSION_ID",
                   help="Resume a previous session by ID (loads saved messages)")
    p.add_argument("--verbose", "-v", action="store_true")
    return p


if __name__ == "__main__":
    asyncio.run(_cli_main(_build_arg_parser().parse_args()))

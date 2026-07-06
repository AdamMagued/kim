"""
UIBridge — thread-safe channel between the async KimAgent and a Tkinter UI
(or any consumer).

Extracted from orchestrator/agent.py to keep the agent class focused on the
agent loop and to let tray/ui.py import UIBridge without pulling in the full
agent module.

Imported in agent.py as:
    from orchestrator.ui_bridge import UIBridge, UIBridgeLogHandler, StdinApprovalBridge
"""

import asyncio
import json
import logging
import queue
import sys
import threading


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
        If the UI takes > 60 s (or no UI is attached), auto-denies (fail-closed).
        This applies to both preview confirms and HITL high-risk approvals.
        """
        if self._cancelled.is_set():
            return False
        event: threading.Event = threading.Event()
        result: list[bool] = [False]
        self._confirm_queue.put_nowait((tool_name, args, event, result))
        # Wait without blocking the asyncio event loop
        loop = asyncio.get_running_loop()
        timed_out = not await loop.run_in_executor(None, lambda: event.wait(timeout=60.0))
        if timed_out:
            logging.getLogger(__name__).warning(
                "Confirmation timed out after 60 s — auto-denying tool '%s'", tool_name
            )
        return result[0]

    def resolve_confirm(
        self, event: threading.Event, result: list[bool], confirmed: bool
    ) -> None:
        """Called by the UI when the user clicks Confirm or Deny."""
        result[0] = confirmed
        event.set()

    # ── Rich approval decisions (K1 server-side HITL gate) ────────────

    async def decide_action(self, tool_name: str, args: dict) -> str:
        """Return "accept" | "acceptForSession" | "decline" for a tool call.

        Base implementation adapts the boolean confirm_action() so UIs that
        only know approve/deny keep working. Subclasses with a richer wire
        format (StdinApprovalBridge) override this to surface
        acceptForSession.
        """
        approved = await self.confirm_action(tool_name, args)
        return "accept" if approved else "decline"

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


def normalize_decision(data: dict) -> str:
    """Map a hitl_approve stdin payload to accept/acceptForSession/decline.

    Understands both the generalized vocabulary ({"decision": "accept" |
    "acceptForSession" | "decline"}) and the legacy {"approved": bool} line
    that older Tauri/CLI builds write.
    """
    decision = str(data.get("decision", "")).strip()
    if decision == "accept_for_session":
        decision = "acceptForSession"
    if decision in ("accept", "acceptForSession", "decline"):
        return decision
    return "accept" if bool(data.get("approved", False)) else "decline"


class StdinApprovalBridge(UIBridge):
    """
    Minimal UIBridge for Tauri mode (no Tkinter).

    confirm_action() pauses the agent and waits for Rust to write a JSON
    approval line to the process's stdin:
        {"type": "hitl_approve", "approved": true|false}
    or, with the generalized K1 vocabulary:
        {"type": "hitl_approve", "id": "…", "decision": "accept"|"acceptForSession"|"decline"}

    Timeout: 120 s — auto-denies if no response arrives.
    """

    async def decide_action(self, tool_name: str, args: dict) -> str:
        if self._cancelled.is_set():
            return "decline"
        pump = get_stdin_pump()
        if pump.is_running():
            try:
                data = await pump.next_approval(timeout=120.0)
                return normalize_decision(data)
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "HITL stdin approval failed (%s) — denying tool '%s'",
                    exc, tool_name,
                )
                return "decline"
        loop = asyncio.get_running_loop()
        try:
            line: str = await asyncio.wait_for(
                loop.run_in_executor(None, sys.stdin.readline),
                timeout=120.0,
            )
            return normalize_decision(json.loads(line.strip()))
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "HITL stdin approval failed (%s) — denying tool '%s'",
                exc, tool_name,
            )
            return "decline"

    async def confirm_action(self, tool_name: str, args: dict) -> bool:
        if self._cancelled.is_set():
            return False
        # K3: when the shared stdin pump is running (runtime), approvals are
        # routed through it (so steer lines don't get eaten by a raw readline).
        # In unit tests the pump isn't started, so fall back to the legacy
        # readline path which the existing tests patch sys.stdin for.
        pump = get_stdin_pump()
        if pump.is_running():
            try:
                data = await pump.next_approval(timeout=120.0)
                return bool(data.get("approved", False))
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "HITL stdin approval failed (%s) — denying tool '%s'", exc, tool_name
                )
                return False
        loop = asyncio.get_running_loop()
        try:
            line: str = await asyncio.wait_for(
                loop.run_in_executor(None, sys.stdin.readline),
                timeout=120.0,
            )
            data = json.loads(line.strip())
            return bool(data.get("approved", False))
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "HITL stdin approval failed (%s) — denying tool '%s'", exc, tool_name
            )
            return False


class StdinPump:
    """K3: single background reader for the agent's stdin.

    Routes JSON lines by ``type``:
      - ``user_steer``  → the steer callback (agent injects as a user message)
      - ``hitl_approve``/``hitl_approval`` → the approval queue (HITL gate)

    A single reader avoids two consumers racing on stdin (steering vs approval).
    """

    def __init__(self) -> None:
        self._approval_q: "asyncio.Queue[dict]" = asyncio.Queue()
        self._steer_cb = None
        self._started = False
        self._loop = None

    def is_running(self) -> bool:
        return self._started

    def set_steer_callback(self, cb) -> None:
        self._steer_cb = cb

    def _dispatch(self, data: dict) -> None:
        """Route one parsed line. Pure enough to unit-test directly."""
        kind = data.get("type")
        if kind == "user_steer":
            text = str(data.get("text", "")).strip()
            if text and self._steer_cb is not None:
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(self._steer_cb, text)
                else:
                    self._steer_cb(text)
        elif kind in ("hitl_approve", "hitl_approval"):
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._approval_q.put_nowait, data)
            else:
                self._approval_q.put_nowait(data)

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except Exception:
                continue
            self._dispatch(data)

    async def next_approval(self, timeout: float) -> dict:
        return await asyncio.wait_for(self._approval_q.get(), timeout=timeout)


_STDIN_PUMP: "StdinPump | None" = None


def get_stdin_pump() -> StdinPump:
    global _STDIN_PUMP
    if _STDIN_PUMP is None:
        _STDIN_PUMP = StdinPump()
    return _STDIN_PUMP

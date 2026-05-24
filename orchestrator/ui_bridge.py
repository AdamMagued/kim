"""
UIBridge — thread-safe channel between the async KimAgent and a Tkinter UI
(or any consumer).

Extracted from orchestrator/agent.py to keep the agent class focused on the
agent loop and to let tray/ui.py import UIBridge without pulling in the full
agent module.

Imported in agent.py as:
    from orchestrator.ui_bridge import UIBridge, UIBridgeLogHandler
"""

import asyncio
import logging
import queue
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
            logging.getLogger(__name__).warning("Confirmation timed out after 60 s — auto-allowing")
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

"""
Kim Control Panel — Tkinter dashboard.

Layout
──────
  ┌───────────────────────────────────────────┐
  │  Task: [_________________________] [Run]  │
  │                                  [Stop]   │
  ├──────────────────────┬────────────────────┤
  │  Log (scrolled Text) │  Screenshot Canvas │
  │                      │                    │
  │                      ├────────────────────┤
  │                      │ Preview mode [✓]   │
  │                      │ [Confirm] [Deny]   │
  ├──────────────────────┴────────────────────┤
  │  Provider: browser   Relay: ● connected   │
  └───────────────────────────────────────────┘

This module has NO direct dependency on tray.app — it receives a reference to
the KimApp instance and calls back through its public methods.
"""

from __future__ import annotations

import base64
import io
import logging
import queue
import threading
import tkinter as tk
from tkinter import font as tkfont
from tkinter import scrolledtext, ttk
from typing import TYPE_CHECKING, Optional

from PIL import Image, ImageTk

if TYPE_CHECKING:
    from tray.app import KimApp

logger = logging.getLogger("kim.ui")

# ── log level colour map ──────────────────────────────────────────────────────
_LOG_COLOURS: dict[str, str] = {
    "DEBUG":    "#888888",
    "INFO":     "#d4d4d4",
    "TOOL":     "#4ec9b0",   # teal — tool calls
    "WARN":     "#dcdcaa",   # yellow
    "WARNING":  "#dcdcaa",
    "ERROR":    "#f44747",   # red
    "CRITICAL": "#f44747",
}
_BG = "#1e1e1e"
_FG = "#d4d4d4"
_BTN_BG = "#3c3c3c"
_THUMB_W = 320
_THUMB_H = 180


class ControlPanel(tk.Toplevel):
    """The Kim control panel window."""

    def __init__(self, parent: tk.Misc, app: "KimApp") -> None:
        super().__init__(parent)
        self._app = app
        self._photo: Optional[ImageTk.PhotoImage] = None  # keep reference alive
        self._confirm_pending: Optional[tuple] = None     # (event, result)

        self.title("Kim — Control Panel")
        self.configure(bg=_BG)
        self.geometry("900x580")
        self.minsize(700, 420)

        self._build_ui()
        self.refresh_status()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── top bar: task input ───────────────────────────────────────────────
        top = tk.Frame(self, bg=_BG, pady=6, padx=8)
        top.pack(fill=tk.X)

        tk.Label(top, text="Task:", bg=_BG, fg=_FG).pack(side=tk.LEFT)
        self._task_var = tk.StringVar()
        self._task_entry = tk.Entry(
            top, textvariable=self._task_var, bg="#2d2d2d", fg=_FG,
            insertbackground=_FG, relief=tk.FLAT, font=("Consolas", 10),
        )
        self._task_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))
        self._task_entry.bind("<Return>", lambda _e: self._on_run())

        self._run_btn = tk.Button(
            top, text="Run", bg="#007acc", fg="white", relief=tk.FLAT,
            padx=10, command=self._on_run,
        )
        self._run_btn.pack(side=tk.LEFT, padx=(0, 4))

        self._stop_btn = tk.Button(
            top, text="Stop", bg="#c0392b", fg="white", relief=tk.FLAT,
            padx=10, command=self._on_stop,
        )
        self._stop_btn.pack(side=tk.LEFT)
        self._stop_btn.config(state=tk.DISABLED)

        # ── middle: log + right panel ─────────────────────────────────────────
        middle = tk.Frame(self, bg=_BG)
        middle.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))

        # Log text widget
        self._log = scrolledtext.ScrolledText(
            middle, bg="#1a1a2e", fg=_FG, font=("Consolas", 9),
            state=tk.DISABLED, relief=tk.FLAT, wrap=tk.WORD,
        )
        self._log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        # Configure colour tags
        for level, colour in _LOG_COLOURS.items():
            self._log.tag_configure(level, foreground=colour)
        self._log.tag_configure("TIMESTAMP", foreground="#555555")

        # Right panel
        right = tk.Frame(middle, bg=_BG, width=_THUMB_W + 16)
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))
        right.pack_propagate(False)

        # Screenshot thumbnail
        tk.Label(right, text="Current View", bg=_BG, fg="#888", font=("Consolas", 8)).pack(anchor=tk.W)
        self._canvas = tk.Canvas(
            right, width=_THUMB_W, height=_THUMB_H,
            bg="#000", highlightthickness=0,
        )
        self._canvas.pack()
        self._canvas.create_text(
            _THUMB_W // 2, _THUMB_H // 2,
            text="No screenshot yet", fill="#444", tags="placeholder",
        )

        # Preview mode toggle
        sep = ttk.Separator(right, orient=tk.HORIZONTAL)
        sep.pack(fill=tk.X, pady=8)

        self._preview_var = tk.BooleanVar(
            value=self._app._config.get("preview_mode", False)
        )
        preview_cb = tk.Checkbutton(
            right,
            text="Action Preview Mode",
            variable=self._preview_var,
            bg=_BG, fg=_FG, selectcolor="#2d2d2d",
            activebackground=_BG, activeforeground=_FG,
            command=self._on_preview_toggle,
        )
        preview_cb.pack(anchor=tk.W)
        tk.Label(
            right,
            text="Agent pauses before each tool call",
            bg=_BG, fg="#666", font=("Consolas", 8),
        ).pack(anchor=tk.W)

        # Confirm / Deny frame (hidden until confirmation needed)
        self._confirm_frame = tk.Frame(right, bg="#2d2d2d", relief=tk.FLAT, bd=1)
        self._confirm_label = tk.Label(
            self._confirm_frame, text="", bg="#2d2d2d", fg=_FG,
            font=("Consolas", 8), wraplength=_THUMB_W - 16, justify=tk.LEFT,
        )
        self._confirm_label.pack(padx=6, pady=(6, 4), anchor=tk.W)

        btn_row = tk.Frame(self._confirm_frame, bg="#2d2d2d")
        btn_row.pack(fill=tk.X, padx=6, pady=(0, 6))
        tk.Button(
            btn_row, text="Confirm", bg="#27ae60", fg="white",
            relief=tk.FLAT, padx=8, command=self._on_confirm,
        ).pack(side=tk.LEFT, padx=(0, 4))
        tk.Button(
            btn_row, text="Deny", bg="#c0392b", fg="white",
            relief=tk.FLAT, padx=8, command=self._on_deny,
        ).pack(side=tk.LEFT)

        # ── status bar ────────────────────────────────────────────────────────
        self._status_bar = tk.Frame(self, bg="#252526", pady=3)
        self._status_bar.pack(fill=tk.X, side=tk.BOTTOM)

        self._provider_label = tk.Label(
            self._status_bar, text="", bg="#252526", fg="#9cdcfe",
            font=("Consolas", 9), padx=8,
        )
        self._provider_label.pack(side=tk.LEFT)

        self._relay_dot = tk.Label(
            self._status_bar, text="●", bg="#252526", fg="#555",
            font=("Consolas", 9),
        )
        self._relay_dot.pack(side=tk.LEFT)
        self._relay_label = tk.Label(
            self._status_bar, text="relay", bg="#252526", fg="#888",
            font=("Consolas", 9), padx=4,
        )
        self._relay_label.pack(side=tk.LEFT)

        self._agent_label = tk.Label(
            self._status_bar, text="idle", bg="#252526", fg="#888",
            font=("Consolas", 9), padx=8,
        )
        self._agent_label.pack(side=tk.RIGHT)

    # ── public API (called by KimApp from tkinter thread) ─────────────────────

    def append_log(self, level: str, message: str) -> None:
        """Append a log line with colour-coded level tag."""
        import datetime
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._log.config(state=tk.NORMAL)
        self._log.insert(tk.END, f"{ts} ", "TIMESTAMP")
        tag = level.upper()
        if tag not in _LOG_COLOURS:
            tag = "INFO"
        self._log.insert(tk.END, f"[{level:<7}] ", tag)
        self._log.insert(tk.END, message + "\n", tag)
        self._log.config(state=tk.DISABLED)
        self._log.see(tk.END)

    def update_screenshot(self, b64: str) -> None:
        """Decode base64 PNG and display as thumbnail."""
        try:
            # Strip data-URI prefix if present
            if "," in b64:
                b64 = b64.split(",", 1)[1]
            data = base64.b64decode(b64)
            img = Image.open(io.BytesIO(data))
            img.thumbnail((_THUMB_W, _THUMB_H), Image.LANCZOS)
            self._photo = ImageTk.PhotoImage(img)
            self._canvas.delete("all")
            # Centre on canvas
            x = (_THUMB_W - img.width) // 2
            y = (_THUMB_H - img.height) // 2
            self._canvas.create_image(x, y, anchor=tk.NW, image=self._photo)
        except Exception as e:
            logger.debug(f"Screenshot display failed: {e}")

    def show_confirm(
        self,
        tool_name: str,
        args: dict,
        event: threading.Event,
        result: list,
    ) -> None:
        """Show the Confirm/Deny panel for an action-preview request."""
        self._confirm_pending = (event, result)
        args_str = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:4])
        self._confirm_label.config(
            text=f"Agent wants to call:\n{tool_name}({args_str})"
        )
        self._confirm_frame.pack(fill=tk.X, pady=(8, 0))
        self._confirm_frame.lift()

    def on_task_done(self, result: str, success: bool) -> None:
        """Called when agent finishes a task."""
        self._stop_btn.config(state=tk.DISABLED)
        self._run_btn.config(state=tk.NORMAL)
        colour = "#27ae60" if success else "#e74c3c"
        status = "done" if success else "failed"
        self._agent_label.config(text=status, fg=colour)
        self._hide_confirm()
        self.append_log("INFO", f"{'✓' if success else '✗'} {result[:200]}")

    def refresh_status(self) -> None:
        """Update provider and relay labels from current config."""
        provider = self._app._active_provider
        self._provider_label.config(text=f"Provider: {provider}")
        relay_url = self._app._config.get("relay", {}).get("url", "")
        if relay_url:
            self._relay_dot.config(fg="#27ae60")
            self._relay_label.config(text=f"relay: {relay_url[:30]}")
        else:
            self._relay_dot.config(fg="#555")
            self._relay_label.config(text="relay: off")

    # ── private callbacks ──────────────────────────────────────────────────────

    def _on_run(self) -> None:
        task = self._task_var.get().strip()
        if not task:
            return
        self._task_var.set("")
        self._run_btn.config(state=tk.DISABLED)
        self._stop_btn.config(state=tk.NORMAL)
        self._agent_label.config(text="running…", fg="#4ec9b0")
        self._app.submit_task(task)

    def _on_stop(self) -> None:
        self._app._do_cancel()
        self._stop_btn.config(state=tk.DISABLED)
        self._run_btn.config(state=tk.NORMAL)
        self._agent_label.config(text="cancelled", fg="#e74c3c")
        self._hide_confirm()

    def _on_preview_toggle(self) -> None:
        enabled = self._preview_var.get()
        self._app._bridge.preview_mode = enabled
        self._app._config["preview_mode"] = enabled
        logger.info(f"Preview mode {'ON' if enabled else 'OFF'}")
        if not enabled:
            self._hide_confirm()

    def _on_confirm(self) -> None:
        if self._confirm_pending:
            event, result = self._confirm_pending
            self._app._bridge.resolve_confirm(event, result, confirmed=True)
            self._confirm_pending = None
        self._hide_confirm()

    def _on_deny(self) -> None:
        if self._confirm_pending:
            event, result = self._confirm_pending
            self._app._bridge.resolve_confirm(event, result, confirmed=False)
            self._confirm_pending = None
        self._hide_confirm()

    def _hide_confirm(self) -> None:
        self._confirm_frame.pack_forget()

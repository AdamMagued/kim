"""
Kim Control Panel — Modern dark-mode chat UI (macOS Tahoe aesthetic).

Layout
──────
  ┌─────────────────────────────────────────────────────┐
  │  ⚙  Voice [•]  Preview [•]         Provider: …     │  ← settings bar
  ├─────────────────────────────────────────────────────┤
  │                                                     │
  │  Kim:  Ready.                                       │  ← message thread
  │                                   User task bubble  │
  │  Kim:  Running take_screenshot…                     │
  │                                                     │
  ├─────────────────────────────────────────────────────┤
  │  [  Type a task…                            ] [➤]   │  ← input bar
  └─────────────────────────────────────────────────────┘

This module has NO direct dependency on tray.app — it receives a reference to
the KimApp instance and calls back through its public methods.
"""

from __future__ import annotations

import datetime
import logging
import threading
import tkinter as tk
from tkinter import font as tkfont
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from tray.app import KimApp

logger = logging.getLogger("kim.ui")

# ── colour palette ────────────────────────────────────────────────────────────

_BG             = "#121212"   # deep charcoal
_BG_SECONDARY   = "#1a1a1a"   # slightly lighter for panels
_BG_INPUT       = "#1e1e1e"   # input field
_BG_BUBBLE_USER = "#2a3a50"   # user message bubble (dark slate blue)
_BG_SETTINGS    = "#0e0e0e"   # settings bar
_BG_HOVER       = "#2a2a2a"   # button hover

_FG             = "#e0e0e0"   # primary text
_FG_DIM         = "#888888"   # secondary / dim text
_FG_ACCENT      = "#7eb8da"   # accent (muted blue)
_FG_KIM         = "#c8d0d8"   # Kim's messages
_FG_SUCCESS     = "#4ade80"   # green
_FG_ERROR       = "#f87171"   # red
_FG_TOOL        = "#67e8f9"   # cyan — tool calls
_FG_WARN        = "#fbbf24"   # amber

_ACCENT_BLUE    = "#3b82f6"   # send button / run accent
_ACCENT_RED     = "#ef4444"   # stop button

_TOGGLE_ON      = "#3b82f6"
_TOGGLE_OFF     = "#555555"

# ── log level → colour ───────────────────────────────────────────────────────

_LEVEL_FG: dict[str, str] = {
    "DEBUG":    _FG_DIM,
    "INFO":     _FG_KIM,
    "TOOL":     _FG_TOOL,
    "WARN":     _FG_WARN,
    "WARNING":  _FG_WARN,
    "ERROR":    _FG_ERROR,
    "CRITICAL": _FG_ERROR,
}

# ── preferred fonts (macOS → SF Pro, fallback → system) ──────────────────────

def _resolve_font(size: int, weight: str = "normal") -> tuple:
    """Return a font tuple preferring SF Pro on macOS, then Inter, then system."""
    for family in ("SF Pro Text", "SF Pro", "Inter", "Helvetica Neue", "Segoe UI", "TkDefaultFont"):
        try:
            f = tkfont.Font(family=family, size=size, weight=weight)
            if f.actual("family") != "TkDefaultFont" or family == "TkDefaultFont":
                return (family, size, weight)
        except Exception:
            continue
    return ("TkDefaultFont", size, weight)


# ── small custom toggle widget ────────────────────────────────────────────────

class _Toggle(tk.Canvas):
    """Minimal iOS-style toggle switch."""

    WIDTH = 36
    HEIGHT = 20

    def __init__(self, parent: tk.Misc, initial: bool = False, command=None, **kw):
        super().__init__(
            parent, width=self.WIDTH, height=self.HEIGHT,
            bg=kw.pop("bg", _BG_SETTINGS), highlightthickness=0, **kw,
        )
        self._on = initial
        self._command = command
        self._draw()
        self.bind("<Button-1>", self._click)

    def _draw(self):
        self.delete("all")
        bg = _TOGGLE_ON if self._on else _TOGGLE_OFF
        r = self.HEIGHT // 2
        # Track (rounded rect)
        self.create_oval(0, 0, self.HEIGHT, self.HEIGHT, fill=bg, outline="")
        self.create_oval(self.WIDTH - self.HEIGHT, 0, self.WIDTH, self.HEIGHT, fill=bg, outline="")
        self.create_rectangle(r, 0, self.WIDTH - r, self.HEIGHT, fill=bg, outline="")
        # Thumb
        pad = 2
        cx = (self.WIDTH - self.HEIGHT + pad * 2) if self._on else pad
        self.create_oval(cx, pad, cx + self.HEIGHT - pad * 2, self.HEIGHT - pad, fill="white", outline="")

    def _click(self, _event=None):
        self._on = not self._on
        self._draw()
        if self._command:
            self._command()

    def get(self) -> bool:
        return self._on

    def set(self, value: bool):
        self._on = value
        self._draw()


# ===========================================================================
# ControlPanel
# ===========================================================================

class ControlPanel(tk.Toplevel):
    """Modern dark-mode chat-style control panel."""

    def __init__(self, parent: tk.Misc, app: "KimApp") -> None:
        super().__init__(parent)
        self._app = app
        self._confirm_pending: Optional[tuple] = None

        # ── window chrome ──────────────────────────────────────────────────
        self.title("Kim")
        self.configure(bg=_BG)
        self.geometry("520x680")
        self.minsize(380, 480)

        # Slight translucency
        try:
            self.attributes("-alpha", 0.96)
        except Exception:
            pass  # not supported on all platforms

        # Dark title bar on macOS
        try:
            self.tk.call("::tk::unsupported::MacWindowStyle", "style",
                         self._w, "moveableModal", "")
        except Exception:
            pass

        # ── fonts ──────────────────────────────────────────────────────────
        self._font       = _resolve_font(13)
        self._font_small = _resolve_font(11)
        self._font_tiny  = _resolve_font(10)
        self._font_bold  = _resolve_font(13, "bold")

        self._build_ui()
        self.refresh_status()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ═══════════════════════════════════════════════════════════════════
        # 1. SETTINGS BAR (top)
        # ═══════════════════════════════════════════════════════════════════
        settings_bar = tk.Frame(self, bg=_BG_SETTINGS, pady=6, padx=12)
        settings_bar.pack(fill=tk.X)

        # Gear icon
        tk.Label(
            settings_bar, text="⚙", bg=_BG_SETTINGS, fg=_FG_DIM,
            font=_resolve_font(14),
        ).pack(side=tk.LEFT, padx=(0, 8))

        # Voice toggle
        tk.Label(
            settings_bar, text="Voice", bg=_BG_SETTINGS, fg=_FG_DIM,
            font=self._font_small,
        ).pack(side=tk.LEFT, padx=(0, 4))
        self._voice_toggle = _Toggle(
            settings_bar,
            initial=self._app._voice.enabled if hasattr(self._app, '_voice') else False,
            command=self._on_voice_toggle,
            bg=_BG_SETTINGS,
        )
        self._voice_toggle.pack(side=tk.LEFT, padx=(0, 12))

        # Human Quirks toggle
        tk.Label(
            settings_bar, text="Quirks", bg=_BG_SETTINGS, fg=_FG_DIM,
            font=self._font_small,
        ).pack(side=tk.LEFT, padx=(0, 4))
        voice_cfg = self._app._config.get("voice", {})
        self._quirks_toggle = _Toggle(
            settings_bar,
            initial=voice_cfg.get("human_quirks", False),
            command=self._on_quirks_toggle,
            bg=_BG_SETTINGS,
        )
        self._quirks_toggle.pack(side=tk.LEFT, padx=(0, 12))

        # Engine dropdown
        tk.Label(
            settings_bar, text="Engine:", bg=_BG_SETTINGS, fg=_FG_DIM,
            font=self._font_small,
        ).pack(side=tk.LEFT, padx=(0, 4))
        
        _ENGINE_UI_NAMES = {"kokoro": "Kokoro", "maya1": "Maya-1", "hume": "Hume"}
        current_engine = voice_cfg.get("engine", "kokoro")
        self._engine_var = tk.StringVar(value=_ENGINE_UI_NAMES.get(current_engine, "Kokoro"))
        self._engine_dropdown = tk.OptionMenu(
            settings_bar, self._engine_var, "Kokoro", "Maya-1", "Hume",
            command=self._on_engine_change
        )
        self._engine_dropdown.config(
            bg=_BG_SETTINGS, fg=_FG_DIM, font=self._font_small, 
            highlightthickness=0, bd=0, indicatoron=0
        )
        self._engine_dropdown.pack(side=tk.LEFT, padx=(0, 12))

        # Voice profile dropdown (Hume only — hidden for other engines)
        _HUME_VOICES = ["Ava Song", "Alice Bennett", "Imani Carter", "Vince Douglas"]
        hume_cfg = voice_cfg.get("hume", {})
        current_voice = hume_cfg.get("voice_name", "Ava Song")

        self._voice_label = tk.Label(
            settings_bar, text="Voice:", bg=_BG_SETTINGS, fg=_FG_DIM,
            font=self._font_small,
        )
        self._voice_var = tk.StringVar(value=current_voice)
        self._voice_dropdown = tk.OptionMenu(
            settings_bar, self._voice_var, *_HUME_VOICES,
            command=self._on_voice_change
        )
        self._voice_dropdown.config(
            bg=_BG_SETTINGS, fg=_FG_DIM, font=self._font_small,
            highlightthickness=0, bd=0, indicatoron=0
        )
        # Only show if Hume is the active engine
        if current_engine == "hume":
            self._voice_label.pack(side=tk.LEFT, padx=(0, 4))
            self._voice_dropdown.pack(side=tk.LEFT, padx=(0, 12))

        # Preview toggle
        tk.Label(
            settings_bar, text="Preview", bg=_BG_SETTINGS, fg=_FG_DIM,
            font=self._font_small,
        ).pack(side=tk.LEFT, padx=(0, 4))
        self._preview_toggle = _Toggle(
            settings_bar,
            initial=self._app._config.get("preview_mode", False),
            command=self._on_preview_toggle,
            bg=_BG_SETTINGS,
        )
        self._preview_toggle.pack(side=tk.LEFT)

        # Provider label (right side)
        self._provider_label = tk.Label(
            settings_bar, text="", bg=_BG_SETTINGS, fg=_FG_ACCENT,
            font=self._font_small,
        )
        self._provider_label.pack(side=tk.RIGHT)

        # Voice status label (reflects VoiceEngine state)
        self._voice_status_label = tk.Label(
            settings_bar, text="", bg=_BG_SETTINGS, fg=_FG_DIM,
            font=self._font_tiny,
        )
        self._voice_status_label.pack(side=tk.RIGHT, padx=(0, 8))

        # Task status dot + label
        self._status_label = tk.Label(
            settings_bar, text="idle", bg=_BG_SETTINGS, fg=_FG_DIM,
            font=self._font_tiny,
        )
        self._status_label.pack(side=tk.RIGHT, padx=(0, 12))

        # Thin separator
        tk.Frame(self, bg="#2a2a2a", height=1).pack(fill=tk.X)

        # ═══════════════════════════════════════════════════════════════════
        # 2. MESSAGE THREAD (centre — scrollable)
        # ═══════════════════════════════════════════════════════════════════
        thread_container = tk.Frame(self, bg=_BG)
        thread_container.pack(fill=tk.BOTH, expand=True)

        self._thread_canvas = tk.Canvas(
            thread_container, bg=_BG, highlightthickness=0,
        )
        self._thread_scrollbar = tk.Scrollbar(
            thread_container, orient=tk.VERTICAL,
            command=self._thread_canvas.yview,
        )
        self._thread_inner = tk.Frame(self._thread_canvas, bg=_BG)

        self._thread_inner.bind(
            "<Configure>",
            lambda e: self._thread_canvas.configure(
                scrollregion=self._thread_canvas.bbox("all")
            ),
        )
        self._canvas_window = self._thread_canvas.create_window(
            (0, 0), window=self._thread_inner, anchor="nw"
        )
        self._thread_canvas.configure(yscrollcommand=self._thread_scrollbar.set)

        self._thread_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._thread_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Resize inner frame width with canvas
        self._thread_canvas.bind("<Configure>", self._on_canvas_resize)

        # Mouse wheel scrolling (scoped to canvas — not bind_all which hijacks other windows)
        self._thread_canvas.bind(
            "<MouseWheel>",
            lambda e: self._thread_canvas.yview_scroll(
                int(-1 * (e.delta / 120)), "units"
            ),
        )
        self._thread_inner.bind(
            "<MouseWheel>",
            lambda e: self._thread_canvas.yview_scroll(
                int(-1 * (e.delta / 120)), "units"
            ),
        )

        # Welcome message
        self._add_kim_message("Ready. Type a task below or use Ctrl+Alt+J.")

        # ═══════════════════════════════════════════════════════════════════
        # 3. CONFIRM BAR (hidden, shown when preview mode triggers)
        # ═══════════════════════════════════════════════════════════════════
        self._confirm_bar = tk.Frame(self, bg="#1c2333", pady=8, padx=12)
        # Not packed until needed

        self._confirm_text = tk.Label(
            self._confirm_bar, text="", bg="#1c2333", fg=_FG,
            font=self._font_small, wraplength=460, justify=tk.LEFT,
            anchor="w",
        )
        self._confirm_text.pack(fill=tk.X, pady=(0, 6))

        btn_row = tk.Frame(self._confirm_bar, bg="#1c2333")
        btn_row.pack(fill=tk.X)

        self._confirm_btn = tk.Button(
            btn_row, text="✓ Confirm", bg=_FG_SUCCESS, fg="#000",
            font=self._font_small, relief=tk.FLAT, padx=12, pady=2,
            command=self._on_confirm, activebackground="#6ee7b7",
        )
        self._confirm_btn.pack(side=tk.LEFT, padx=(0, 6))

        self._deny_btn = tk.Button(
            btn_row, text="✗ Deny", bg=_ACCENT_RED, fg="white",
            font=self._font_small, relief=tk.FLAT, padx=12, pady=2,
            command=self._on_deny, activebackground="#fca5a5",
        )
        self._deny_btn.pack(side=tk.LEFT)

        # ═══════════════════════════════════════════════════════════════════
        # 4. INPUT BAR (bottom)
        # ═══════════════════════════════════════════════════════════════════
        # Separator
        tk.Frame(self, bg="#2a2a2a", height=1).pack(fill=tk.X)

        self._input_bar = tk.Frame(self, bg=_BG, pady=10, padx=12)
        self._input_bar.pack(fill=tk.X, side=tk.BOTTOM)
        input_bar = self._input_bar

        # Rounded input container
        input_wrap = tk.Frame(input_bar, bg=_BG_INPUT, padx=12, pady=8)
        input_wrap.pack(fill=tk.X, side=tk.LEFT, expand=True, padx=(0, 8))

        self._task_var = tk.StringVar()
        self._task_entry = tk.Entry(
            input_wrap, textvariable=self._task_var,
            bg=_BG_INPUT, fg=_FG, insertbackground=_FG_ACCENT,
            relief=tk.FLAT, font=self._font,
            border=0,
        )
        self._task_entry.pack(fill=tk.X, expand=True)
        self._task_entry.bind("<Return>", lambda _e: self._on_run())

        # Placeholder behaviour
        self._placeholder_active = True
        self._task_entry.insert(0, "Type a task…")
        self._task_entry.config(fg=_FG_DIM)
        self._task_entry.bind("<FocusIn>", self._on_entry_focus_in)
        self._task_entry.bind("<FocusOut>", self._on_entry_focus_out)

        # Send button
        self._send_btn = tk.Button(
            input_bar, text="➤", bg=_ACCENT_BLUE, fg="white",
            font=_resolve_font(16, "bold"), relief=tk.FLAT,
            padx=10, pady=4, command=self._on_run,
            activebackground="#60a5fa", cursor="hand2",
        )
        self._send_btn.pack(side=tk.RIGHT)

        # Stop button (hidden by default)
        self._stop_btn = tk.Button(
            input_bar, text="■", bg=_ACCENT_RED, fg="white",
            font=_resolve_font(14, "bold"), relief=tk.FLAT,
            padx=10, pady=4, command=self._on_stop,
            activebackground="#fca5a5", cursor="hand2",
        )
        # Not packed until a task runs

    # ── canvas resize handler ─────────────────────────────────────────────────

    def _on_canvas_resize(self, event):
        self._thread_canvas.itemconfig(self._canvas_window, width=event.width)

    # ── message thread helpers ────────────────────────────────────────────────

    def _add_kim_message(self, text: str, fg: str = _FG_KIM) -> None:
        """Add a left-aligned Kim message to the thread."""
        row = tk.Frame(self._thread_inner, bg=_BG, pady=4, padx=16)
        row.pack(fill=tk.X, anchor="w")

        # "Kim" label
        tk.Label(
            row, text="Kim", bg=_BG, fg=_FG_ACCENT,
            font=self._font_bold, anchor="w",
        ).pack(anchor="w")

        # Message text
        msg = tk.Label(
            row, text=text, bg=_BG, fg=fg,
            font=self._font, wraplength=420, justify=tk.LEFT,
            anchor="w",
        )
        msg.pack(anchor="w", pady=(2, 0))

        self._scroll_to_bottom()

    def _add_user_message(self, text: str) -> None:
        """Add a right-aligned user message bubble."""
        row = tk.Frame(self._thread_inner, bg=_BG, pady=4, padx=16)
        row.pack(fill=tk.X, anchor="e")

        # Bubble
        bubble = tk.Frame(row, bg=_BG_BUBBLE_USER, padx=12, pady=8)
        bubble.pack(anchor="e")

        tk.Label(
            bubble, text=text, bg=_BG_BUBBLE_USER, fg=_FG,
            font=self._font, wraplength=340, justify=tk.LEFT,
            anchor="w",
        ).pack()

        self._scroll_to_bottom()

    def _add_tool_message(self, text: str) -> None:
        """Add a tool-call message with distinctive colour."""
        row = tk.Frame(self._thread_inner, bg=_BG, pady=2, padx=24)
        row.pack(fill=tk.X, anchor="w")

        tk.Label(
            row, text=f"⚡ {text}", bg=_BG, fg=_FG_TOOL,
            font=self._font_small, anchor="w",
        ).pack(anchor="w")

        self._scroll_to_bottom()

    def _add_system_message(self, text: str, fg: str = _FG_DIM) -> None:
        """Add a small, dim system/status message."""
        row = tk.Frame(self._thread_inner, bg=_BG, pady=1, padx=24)
        row.pack(fill=tk.X, anchor="w")

        tk.Label(
            row, text=text, bg=_BG, fg=fg,
            font=self._font_tiny, anchor="w",
        ).pack(anchor="w")

        self._scroll_to_bottom()

    def _scroll_to_bottom(self) -> None:
        """Scroll the thread to the latest message."""
        self._thread_canvas.update_idletasks()
        self._thread_canvas.yview_moveto(1.0)

    # ── placeholder behaviour ─────────────────────────────────────────────────

    def _on_entry_focus_in(self, _event=None):
        if self._placeholder_active:
            self._task_entry.delete(0, tk.END)
            self._task_entry.config(fg=_FG)
            self._placeholder_active = False

    def _on_entry_focus_out(self, _event=None):
        if not self._task_var.get().strip():
            self._task_entry.insert(0, "Type a task…")
            self._task_entry.config(fg=_FG_DIM)
            self._placeholder_active = True

    # ══════════════════════════════════════════════════════════════════════════
    # PUBLIC API — called by KimApp from the Tkinter thread
    # These method signatures must remain stable.
    # ══════════════════════════════════════════════════════════════════════════

    def append_log(self, level: str, message: str) -> None:
        """Append a log event as a chat message.

        TOOL-level logs appear as tool call messages, INFO as Kim
        messages, and everything else as system messages.
        """
        level_upper = level.upper()
        if level_upper == "TOOL":
            self._add_tool_message(message)
        elif level_upper in ("INFO",):
            self._add_kim_message(message)
        elif level_upper in ("WARN", "WARNING"):
            self._add_system_message(f"⚠ {message}", fg=_FG_WARN)
        elif level_upper in ("ERROR", "CRITICAL"):
            self._add_system_message(f"✗ {message}", fg=_FG_ERROR)
        else:
            self._add_system_message(message)

    def update_screenshot(self, b64: str) -> None:
        """Accept a screenshot update (no-op — we removed the preview box)."""
        # Screenshots are no longer displayed in the UI.
        # The agent can still use take_screenshot; we just don't show it.
        pass

    def show_confirm(
        self,
        tool_name: str,
        args: dict,
        event: threading.Event,
        result: list,
    ) -> None:
        """Show the Confirm/Deny bar for an action-preview request."""
        self._confirm_pending = (event, result)
        args_str = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:4])
        self._confirm_text.config(
            text=f"Agent wants to call:  {tool_name}({args_str})"
        )
        self._confirm_bar.pack(fill=tk.X, before=self._get_input_bar_widget())
        self._add_tool_message(f"Awaiting confirmation: {tool_name}(…)")

    def on_task_done(self, result, success: bool) -> None:
        """Called when the agent finishes a task."""
        self._stop_btn.pack_forget()
        self._send_btn.pack(side=tk.RIGHT)
        colour = _FG_SUCCESS if success else _FG_ERROR
        icon = "✓" if success else "✗"
        status = "done" if success else "failed"
        self._status_label.config(text=status, fg=colour)
        self._hide_confirm()
        # Extract a clean display string
        if isinstance(result, dict):
            display = result.get("summary", str(result))
        else:
            display = str(result)
        self._add_kim_message(f"{icon}  {display[:300]}", fg=colour)

    def refresh_status(self) -> None:
        """Update provider label from current config."""
        provider = self._app._active_provider
        self._provider_label.config(text=f"{provider}")

    def set_voice_status(self, status, message: str) -> None:
        """Update the voice status label. Called from KimApp on the Tk thread.
        `status` is a VoiceStatus enum value."""
        from tray.voice import VoiceStatus  # local import to avoid circular
        _STATUS_COLOURS = {
            VoiceStatus.DISABLED: _FG_DIM,
            VoiceStatus.LOADING:  _FG_WARN,
            VoiceStatus.READY:    _FG_SUCCESS,
            VoiceStatus.FAILED:   _FG_ERROR,
        }
        _STATUS_ICONS = {
            VoiceStatus.DISABLED: "🔇",
            VoiceStatus.LOADING:  "⏳",
            VoiceStatus.READY:    "🔊",
            VoiceStatus.FAILED:   "⚠",
        }
        icon = _STATUS_ICONS.get(status, "")
        colour = _STATUS_COLOURS.get(status, _FG_DIM)
        self._voice_status_label.config(text=f"{icon} {message}", fg=colour)

    # ── private callbacks ─────────────────────────────────────────────────────

    def _on_run(self) -> None:
        if self._placeholder_active:
            return
        task = self._task_var.get().strip()
        if not task:
            return
        self._add_user_message(task)
        self._task_var.set("")
        # Swap send → stop
        self._send_btn.pack_forget()
        self._stop_btn.pack(side=tk.RIGHT)
        self._status_label.config(text="running…", fg=_FG_TOOL)
        self._app.submit_task(task)

    def _on_stop(self) -> None:
        self._app._do_cancel()
        self._stop_btn.pack_forget()
        self._send_btn.pack(side=tk.RIGHT)
        self._status_label.config(text="cancelled", fg=_FG_ERROR)
        self._hide_confirm()
        self._add_system_message("Task cancelled by user.", fg=_FG_WARN)

    def _on_preview_toggle(self) -> None:
        enabled = self._preview_toggle.get()
        self._app._bridge.preview_mode = enabled
        self._app._config["preview_mode"] = enabled
        logger.info(f"Preview mode {'ON' if enabled else 'OFF'}")
        if not enabled:
            self._hide_confirm()

    def _on_voice_toggle(self) -> None:
        enabled = self._voice_toggle.get()
        if hasattr(self._app, '_voice'):
            self._app._voice.set_enabled(enabled)
        # Persist to config.yaml (canonical key: voice.enabled)
        if "voice" not in self._app._config:
            self._app._config["voice"] = {}
        self._app._config["voice"]["enabled"] = enabled
        self._persist_config()
        logger.info(f"Voice {'ON' if enabled else 'OFF'}")

    def _on_quirks_toggle(self) -> None:
        enabled = self._quirks_toggle.get()
        if "voice" not in self._app._config:
            self._app._config["voice"] = {}
        self._app._config["voice"]["human_quirks"] = enabled
        self._persist_config()
        logger.info(f"Human Quirks {'ON' if enabled else 'OFF'}")

    def _on_voice_change(self, selected_voice: str) -> None:
        """Called when the user selects a different Hume voice profile."""
        if "voice" not in self._app._config:
            self._app._config["voice"] = {}
        if "hume" not in self._app._config["voice"]:
            self._app._config["voice"]["hume"] = {}
        self._app._config["voice"]["hume"]["voice_name"] = selected_voice
        self._persist_config()
        self._add_system_message(f"🎙 Voice profile set to: {selected_voice}", fg=_FG_SUCCESS)
        logger.info(f"Hume voice changed to: {selected_voice}")

    def _persist_config(self) -> None:
        """Write the current in-memory config to config.yaml."""
        try:
            import yaml
            from tray.app import _CONFIG_PATH
            with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                yaml.dump(self._app._config, f, default_flow_style=False, allow_unicode=True)
        except Exception as e:
            logger.warning(f"Failed to persist config: {e}")

    def _on_engine_change(self, selected_ui_name: str) -> None:
        """Called when the user selects a different voice engine from the UI."""
        _NAME_TO_ENGINE = {"Maya-1": "maya1", "Kokoro": "kokoro", "Hume": "hume"}
        new_engine = _NAME_TO_ENGINE.get(selected_ui_name, "kokoro")
        
        # Don't switch if it's already active
        current = self._app._config.get("voice", {}).get("engine", "kokoro")
        if new_engine == current:
            return
            
        self._add_system_message(f"Loading {selected_ui_name} engine, please wait...", fg=_FG_WARN)

        # Show/hide the Hume voice profile dropdown
        if new_engine == "hume":
            self._voice_label.pack(side=tk.LEFT, padx=(0, 4), after=self._engine_dropdown)
            self._voice_dropdown.pack(side=tk.LEFT, padx=(0, 12), after=self._voice_label)
        else:
            self._voice_label.pack_forget()
            self._voice_dropdown.pack_forget()
        
        def _task():
            try:
                # Execute hot swap with memory flush
                if hasattr(self._app, '_voice') and self._app._voice:
                    self._app._voice.switch_engine(new_engine, self._app._config)
                
                # Persist setting to config.yaml
                import yaml
                from tray.app import _CONFIG_PATH
                
                if _CONFIG_PATH.exists():
                    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                        c = yaml.safe_load(f) or {}
                    
                    if "voice" not in c:
                        c["voice"] = {}
                    c["voice"]["engine"] = new_engine
                    
                    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                        yaml.dump(c, f, default_flow_style=False, allow_unicode=True)
                
                self._app._root.after(0, lambda: self._add_system_message(f"🔄 Switched voice engine to: {selected_ui_name}", fg=_FG_SUCCESS))
            except Exception as e:
                self._app._root.after(0, lambda e=e: self._add_system_message(f"✗ Failed to load {selected_ui_name}: {e}", fg=_FG_ERROR))
                
        # Run in background to avoid blocking the main TK thread while model weights load into RAM
        threading.Thread(target=_task, daemon=True, name="engine-swap").start()

    def _on_confirm(self) -> None:
        if self._confirm_pending:
            event, result = self._confirm_pending
            self._app._bridge.resolve_confirm(event, result, confirmed=True)
            self._confirm_pending = None
        self._hide_confirm()
        self._add_system_message("✓ Action confirmed", fg=_FG_SUCCESS)

    def _on_deny(self) -> None:
        if self._confirm_pending:
            event, result = self._confirm_pending
            self._app._bridge.resolve_confirm(event, result, confirmed=False)
            self._confirm_pending = None
        self._hide_confirm()
        self._add_system_message("✗ Action denied", fg=_FG_ERROR)

    def _hide_confirm(self) -> None:
        self._confirm_bar.pack_forget()

    def _get_input_bar_widget(self):
        """Return the input bar frame so confirm bar can pack before it."""
        return self._input_bar

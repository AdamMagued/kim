"""
Kim Settings Window — edit config.yaml and .env values.

Opens as a Tkinter Toplevel with two tabs:
  • Config  — edits key fields in config.yaml
  • API Keys — edits the .env file (key=value pairs)

Saves atomically (write temp then rename) to avoid corruption.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from tkinter import messagebox, ttk
import tkinter as tk
from typing import Optional

import yaml

logger = logging.getLogger("kim.settings")

_BG = "#1e1e1e"
_FG = "#d4d4d4"
_ENTRY_BG = "#2d2d2d"
_BTN_BG = "#3c3c3c"

_PROVIDERS = ["browser", "claude", "openai", "gemini", "deepseek"]

# Fields shown in the Config tab.
# Each entry: (yaml_key_path, label, widget_type, options)
#   widget_type: "entry" | "combo" | "check" | "spinbox"
_CONFIG_FIELDS = [
    ("provider",           "Active Provider",       "combo",   _PROVIDERS),
    ("project_root",       "Project Root",          "entry",   None),
    ("max_iterations",     "Max Iterations",        "spinbox", (1, 100)),
    ("screenshot_scale",   "Screenshot Scale",      "entry",   None),
    ("preview_mode",       "Action Preview Mode",   "check",   None),
    ("max_tokens",         "Max LLM Tokens",        "spinbox", (256, 32768)),
    ("memory_max_messages","Memory Max Messages",   "spinbox", (10, 200)),
    ("relay.url",          "Relay Server URL",      "entry",   None),
    ("relay.poll_interval","Relay Poll Interval(s)","spinbox", (1, 60)),
    ("openai_base_url",    "OpenAI Base URL",       "entry",   None),
    ("openai_api_key_env", "OpenAI Key Env Var",    "entry",   None),
]

# .env key labels
_ENV_KEYS = [
    ("ANTHROPIC_API_KEY",   "Anthropic API Key"),
    ("OPENAI_API_KEY",      "OpenAI API Key"),
    ("GOOGLE_API_KEY",      "Google API Key"),
    ("DEEPSEEK_API_KEY",    "DeepSeek API Key"),
    ("RELAY_PC_API_KEY",    "Relay PC API Key"),
    ("RELAY_PHONE_API_KEY", "Relay Phone API Key"),
    ("RELAY_URL",           "Relay URL (.env)"),
]


def _get_nested(d: dict, dotted: str, default=None):
    keys = dotted.split(".")
    v = d
    for k in keys:
        if not isinstance(v, dict):
            return default
        v = v.get(k, default)
    return v


def _set_nested(d: dict, dotted: str, value) -> None:
    keys = dotted.split(".")
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def _atomic_write(path: Path, content: str) -> None:
    """Write to a temp file then rename to avoid partial writes."""
    dir_ = path.parent
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=dir_, delete=False, suffix=".tmp"
    ) as f:
        f.write(content)
        tmp = f.name
    os.replace(tmp, path)


def _parse_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def _write_env(path: Path, data: dict[str, str]) -> None:
    lines = []
    # Preserve any existing lines (comments, extra keys)
    existing_keys: set[str] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.split("=", 1)[0].strip()
                if k in data:
                    lines.append(f"{k}={data[k]}")
                    existing_keys.add(k)
                    continue
            lines.append(line)
    # Append new keys not already in file
    for k, v in data.items():
        if k not in existing_keys:
            lines.append(f"{k}={v}")
    _atomic_write(path, "\n".join(lines) + "\n")


class SettingsWindow(tk.Toplevel):
    """Settings editor for config.yaml and .env."""

    def __init__(
        self,
        parent: tk.Misc,
        config_path: Path,
        env_path: Path,
    ) -> None:
        super().__init__(parent)
        self._config_path = config_path
        self._env_path = env_path

        self.title("Kim — Settings")
        self.configure(bg=_BG)
        self.geometry("560x540")
        self.resizable(False, True)

        self._config: dict = {}
        self._env: dict[str, str] = {}
        self._load()
        self._build_ui()

    # ── load / save ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        if self._config_path.exists():
            with open(self._config_path, "r", encoding="utf-8") as f:
                self._config = yaml.safe_load(f) or {}
        self._env = _parse_env(self._env_path)

    def _save(self) -> None:
        try:
            self._collect_config()
            self._collect_env()
            _atomic_write(
                self._config_path,
                yaml.dump(self._config, default_flow_style=False, allow_unicode=True),
            )
            _write_env(self._env_path, self._env)
            messagebox.showinfo("Kim", "Settings saved.", parent=self)
            logger.info("Settings saved")
        except Exception as e:
            messagebox.showerror("Kim", f"Failed to save settings:\n{e}", parent=self)
            logger.error(f"Settings save error: {e}", exc_info=True)

    def _collect_config(self) -> None:
        for key, _label, wtype, _opts in _CONFIG_FIELDS:
            widget = self._config_widgets.get(key)
            if widget is None:
                continue
            if wtype == "check":
                value = bool(widget.get())
            elif wtype == "spinbox":
                try:
                    value = int(widget.get())
                except ValueError:
                    continue
            elif wtype == "entry":
                value = widget.get().strip()
                # Coerce numeric-looking strings
                if re.fullmatch(r"\d+(\.\d+)?", value):
                    value = float(value) if "." in value else int(value)
            else:  # combo
                value = widget.get().strip()
            _set_nested(self._config, key, value)

    def _collect_env(self) -> None:
        for key, _label in _ENV_KEYS:
            widget = self._env_widgets.get(key)
            if widget:
                self._env[key] = widget.get().strip()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self._config_widgets: dict = {}
        self._env_widgets: dict = {}

        config_frame = tk.Frame(notebook, bg=_BG)
        env_frame = tk.Frame(notebook, bg=_BG)
        notebook.add(config_frame, text="  Config  ")
        notebook.add(env_frame, text="  API Keys  ")

        self._build_config_tab(config_frame)
        self._build_env_tab(env_frame)

        # Save / Close buttons
        btn_row = tk.Frame(self, bg=_BG)
        btn_row.pack(fill=tk.X, padx=8, pady=(0, 8))
        tk.Button(
            btn_row, text="Save", bg="#007acc", fg="white",
            relief=tk.FLAT, padx=14, pady=4, command=self._save,
        ).pack(side=tk.RIGHT, padx=(4, 0))
        tk.Button(
            btn_row, text="Close", bg=_BTN_BG, fg=_FG,
            relief=tk.FLAT, padx=14, pady=4, command=self.destroy,
        ).pack(side=tk.RIGHT)

    def _build_config_tab(self, parent: tk.Frame) -> None:
        canvas = tk.Canvas(parent, bg=_BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        inner = tk.Frame(canvas, bg=_BG)
        window_id = canvas.create_window((0, 0), window=inner, anchor=tk.NW)

        def _on_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(window_id, width=canvas.winfo_width())

        inner.bind("<Configure>", _on_configure)
        canvas.bind("<Configure>", _on_configure)

        for row_idx, (key, label, wtype, opts) in enumerate(_CONFIG_FIELDS):
            tk.Label(
                inner, text=label + ":", bg=_BG, fg="#9cdcfe",
                font=("Consolas", 9), anchor=tk.W,
            ).grid(row=row_idx, column=0, sticky=tk.W, padx=(8, 4), pady=3)

            current = _get_nested(self._config, key, "")

            if wtype == "combo":
                var = tk.StringVar(value=str(current))
                w = ttk.Combobox(inner, textvariable=var, values=opts, width=28, state="readonly")
                w.set(str(current))
                self._config_widgets[key] = var
            elif wtype == "check":
                var = tk.BooleanVar(value=bool(current))
                w = tk.Checkbutton(
                    inner, variable=var, bg=_BG, fg=_FG,
                    selectcolor="#2d2d2d", activebackground=_BG,
                )
                self._config_widgets[key] = var
            elif wtype == "spinbox":
                lo, hi = opts
                var = tk.StringVar(value=str(current if current != "" else lo))
                w = tk.Spinbox(
                    inner, from_=lo, to=hi, textvariable=var,
                    bg=_ENTRY_BG, fg=_FG, insertbackground=_FG,
                    relief=tk.FLAT, width=10,
                )
                self._config_widgets[key] = var
            else:
                var = tk.StringVar(value=str(current) if current is not None else "")
                w = tk.Entry(
                    inner, textvariable=var, bg=_ENTRY_BG, fg=_FG,
                    insertbackground=_FG, relief=tk.FLAT, width=32,
                    font=("Consolas", 9),
                )
                self._config_widgets[key] = var

            w.grid(row=row_idx, column=1, sticky=tk.W, padx=(0, 8), pady=3)

    def _build_env_tab(self, parent: tk.Frame) -> None:
        for row_idx, (key, label) in enumerate(_ENV_KEYS):
            tk.Label(
                parent, text=label + ":", bg=_BG, fg="#9cdcfe",
                font=("Consolas", 9), anchor=tk.W,
            ).grid(row=row_idx, column=0, sticky=tk.W, padx=(8, 4), pady=6)

            current = self._env.get(key, "")
            var = tk.StringVar(value=current)
            show = "*" if "KEY" in key else ""
            w = tk.Entry(
                parent, textvariable=var, show=show,
                bg=_ENTRY_BG, fg=_FG, insertbackground=_FG,
                relief=tk.FLAT, width=36, font=("Consolas", 9),
            )
            w.grid(row=row_idx, column=1, sticky=tk.EW, padx=(0, 8), pady=6)
            self._env_widgets[key] = var

        # Toggle show/hide passwords
        tk.Button(
            parent, text="Show / Hide Keys",
            bg=_BTN_BG, fg=_FG, relief=tk.FLAT, padx=8,
            command=self._toggle_show_keys,
        ).grid(row=len(_ENV_KEYS), column=0, columnspan=2, pady=(12, 0), sticky=tk.W, padx=8)
        self._keys_hidden = True

    def _toggle_show_keys(self) -> None:
        self._keys_hidden = not self._keys_hidden
        show = "*" if self._keys_hidden else ""
        for key, _label in _ENV_KEYS:
            widget_var = self._env_widgets.get(key)
            if widget_var is None:
                continue
            # find the Entry widget in the tab frame
            for child in self.winfo_children():
                self._set_show_recursive(child, key, show)

    def _set_show_recursive(self, widget: tk.Widget, key: str, show: str) -> None:
        """Recursively search for Entry widgets bound to the env key variable."""
        if isinstance(widget, tk.Entry):
            try:
                var = self._env_widgets.get(key)
                if var and str(widget.cget("textvariable")) == str(var):
                    widget.config(show=show)
            except Exception:
                pass
        for child in widget.winfo_children():
            self._set_show_recursive(child, key, show)

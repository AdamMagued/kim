"""
Kim system-tray application.

Architecture
────────────
  • main thread    → Tkinter event loop (ControlPanel / task dialogs)
  • daemon thread  → pystray icon (run_detached)
  • daemon thread  → asyncio event loop (KimAgent tasks)

The three threads communicate through UIBridge (queue-based, no shared state).

Entry point:
    python -m tray.app
"""

import asyncio
import io
import logging
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog
from typing import Optional

# ── resolve project root ──────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
PROJECT_ROOT = _HERE.parent
sys.path.insert(0, str(PROJECT_ROOT))

import yaml
from dotenv import load_dotenv
from PIL import Image

import pystray
from pystray import MenuItem as Item

from orchestrator.agent import UIBridge, mcp_agent_context
from tray.voice import VoiceEngine, VoiceStatus

logger = logging.getLogger("kim.tray")

# ── constants ─────────────────────────────────────────────────────────────────
_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
_ENV_PATH = PROJECT_ROOT / ".env"
_ICON_SIZE = (64, 64)
_PROVIDERS = ["browser", "claude", "openai", "gemini", "deepseek"]

# Icon colours per agent state
_COLOUR_IDLE = (70, 130, 180)    # steel-blue
_COLOUR_RUNNING = (34, 139, 34)  # forest-green
_COLOUR_ERROR = (178, 34, 34)    # firebrick


def _make_icon_image(colour: tuple) -> Image.Image:
    """Return a solid-colour square PIL image for the tray icon."""
    img = Image.new("RGB", _ICON_SIZE, colour)
    return img


def _load_config() -> dict:
    load_dotenv(_ENV_PATH)
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


import tempfile

def _atomic_write_yaml(path: Path, data: dict) -> None:
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        os.unlink(tmp_path)
        raise
    os.replace(tmp_path, str(path))

def _save_provider(provider: str) -> None:
    """Persist the active provider to config.yaml."""
    config = _load_config()
    config["provider"] = provider
    _atomic_write_yaml(_CONFIG_PATH, config)


def _save_config(config: dict) -> None:
    """Persist the full config dict to config.yaml."""
    _atomic_write_yaml(_CONFIG_PATH, config)


# ── toaster (optional) ────────────────────────────────────────────────────────

def _toast(title: str, message: str) -> None:
    try:
        from win10toast import ToastNotifier
        ToastNotifier().show_toast(title, message[:80], duration=5, threaded=True)
    except Exception:
        pass  # non-fatal if win10toast is absent or fails


# ── async runner thread ───────────────────────────────────────────────────────

class _AsyncRunner:
    """Owns an asyncio event loop in a daemon thread."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._current_task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="kim-asyncio"
        )
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(self, coro) -> None:
        """Schedule a coroutine on the asyncio loop from any thread."""
        self._current_task = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return self._current_task

    def cancel_current(self) -> None:
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()

    def stop(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=2.0)


# ── main application ──────────────────────────────────────────────────────────

class KimApp:
    """Orchestrates the tray icon, control panel, and agent runner."""

    def __init__(self) -> None:
        self._config: dict = _load_config()
        self._bridge = UIBridge()
        self._runner = _AsyncRunner()
        self._agent_running = False
        self._active_provider: str = self._config.get("provider", "browser")
        self._active_account_id: Optional[int] = self._config.get("selected_account_id")
        self._accounts: list[dict] = []

        # Tkinter root (hidden — acts as event dispatcher)
        self._root = tk.Tk()
        self._root.withdraw()
        self._root.title("Kim")

        # Optional references to open windows
        self._control_panel = None  # tray.ui.ControlPanel
        self._settings_win = None   # tray.settings.SettingsWindow

        # Voice engine (constructor is cheap — no model loading here)
        self._voice = VoiceEngine(self._config)

        # pystray icon
        self._icon: Optional[pystray.Icon] = None

        # pynput hotkey listener
        self._hotkey_listener = None

        # Task submission queue (from hotkey / menu → tkinter thread)
        self._task_q: queue.Queue = queue.Queue()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Start everything and enter the Tkinter main loop."""
        self._runner.start()
        self._start_tray_icon()
        self._register_hotkey()

        # Show control panel immediately (splash-first)
        self._show_control_panel()

        # Wire voice status → UI and kick off background warm-up
        self._voice.set_status_callback(self._on_voice_status)
        self._voice.warm_up()

        # Initial account fetch
        self._fetch_accounts()

        # Poll for cross-thread events every 50 ms
        self._root.after(50, self._poll)
        # Periodically fetch accounts every 30 seconds
        self._root.after(30000, self._periodic_fetch_accounts)
        
        # Start relay polling if configured
        self._root.after(2000, self._poll_relay)
        
        try:
            self._root.mainloop()
        finally:
            self._cleanup()

    def _poll_relay(self) -> None:
        """Poll the remote relay server for new tasks."""
        if self._agent_running:
            # Don't poll if we're already busy
            self._root.after(2000, self._poll_relay)
            return

        relay_url = self._config.get("relay", {}).get("url")
        pc_key = os.environ.get("RELAY_PC_API_KEY")
        
        if not relay_url or not pc_key:
            # Relay not configured or missing key
            self._root.after(5000, self._poll_relay)
            return

        import httpx
        try:
            headers = {"x-api-key": pc_key}
            resp = httpx.get(f"{relay_url}/prompt/next", headers=headers, timeout=5.0)
            
            if resp.status_code == 200:
                item = resp.json()
                task_id = item.get("task_id")
                task_text = item.get("task")
                if task_id and task_text:
                    logger.info(f"Relay: Received remote task {task_id}: {task_text}")
                    self._root.after(0, lambda: self.submit_task(task_text, task_id=task_id))
            elif resp.status_code == 204:
                # No tasks in queue
                pass
            else:
                logger.debug(f"Relay poll status: {resp.status_code}")
        except Exception as e:
            logger.debug(f"Relay poll failed: {e}")

        # Poll again after interval (from config or default 2s)
        interval = self._config.get("relay", {}).get("poll_interval", 2)
        self._root.after(int(interval * 1000), self._poll_relay)

    def _fetch_accounts(self) -> None:
        """Fetch active accounts from kimTools API."""
        import httpx
        try:
            # kimTools is typically on 8046
            url = self._config.get("kimtools_url", "http://localhost:8046")
            resp = httpx.get(f"{url}/api/accounts/active", timeout=2.0)
            if resp.status_code == 200:
                self._accounts = resp.json()
                logger.info(f"Fetched {len(self._accounts)} accounts from kimTools")
                # Update menu if icon exists
                if self._icon:
                    self._icon.menu = self._build_menu()
                if self._control_panel and self._control_panel.winfo_exists():
                    self._control_panel.refresh_accounts()
        except Exception as e:
            logger.debug(f"Failed to fetch accounts: {e}")

    def _periodic_fetch_accounts(self) -> None:
        self._fetch_accounts()
        self._root.after(30000, self._periodic_fetch_accounts)

    def _on_voice_status(self, status: VoiceStatus, message: str) -> None:
        """Callback from VoiceEngine (may fire from any thread).
        Schedules a UI update on the Tkinter thread."""
        self._root.after(0, self._apply_voice_status, status, message)

    def _apply_voice_status(self, status: VoiceStatus, message: str) -> None:
        """Update the ControlPanel's voice status label (Tkinter thread)."""
        if self._control_panel and self._control_panel.winfo_exists():
            self._control_panel.set_voice_status(status, message)

    def _cleanup(self) -> None:
        if self._hotkey_listener:
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass
        self._runner.stop()

    # ── tray icon ─────────────────────────────────────────────────────────────

    def _start_tray_icon(self) -> None:
        self._icon = pystray.Icon(
            "Kim",
            icon=_make_icon_image(_COLOUR_IDLE),
            title="Kim Agent",
            menu=self._build_menu(),
        )
        t = threading.Thread(
            target=self._icon.run_detached, daemon=True, name="kim-tray"
        )
        t.start()

    def _build_menu(self) -> pystray.Menu:
        provider_items = [
            Item(
                p.capitalize(),
                self._make_provider_setter(p),
                checked=lambda _, p=p: self._active_provider == p,
                radio=True,
            )
            for p in _PROVIDERS
        ]

        account_items = [
            Item(
                "None / Local",
                self._make_account_setter(None),
                checked=lambda _: self._active_account_id is None,
                radio=True,
            )
        ]
        for acc in self._accounts:
            acc_id = acc["id"]
            label = f"{acc['label']} ({acc['provider']})"
            account_items.append(
                Item(
                    label,
                    self._make_account_setter(acc_id),
                    checked=lambda _, aid=acc_id: self._active_account_id == aid,
                    radio=True,
                )
            )

        return pystray.Menu(
            Item("Open Control Panel", self._open_control_panel),
            Item("Run Task…", self._prompt_task),
            pystray.Menu.SEPARATOR,
            Item(
                "Provider",
                pystray.Menu(*provider_items),
            ),
            Item(
                "Account",
                pystray.Menu(*account_items),
            ),
            Item(
                "Agent",
                pystray.Menu(
                    Item("Cancel current task", self._cancel_task),
                ),
            ),
            pystray.Menu.SEPARATOR,
            Item("Settings…", self._open_settings),
            Item("Quit", self._quit),
        )

    def _update_icon_colour(self, colour: tuple) -> None:
        if self._icon:
            self._icon.icon = _make_icon_image(colour)

    # ── hotkey ────────────────────────────────────────────────────────────────

    def _register_hotkey(self) -> None:
        """Register Ctrl+Alt+J using a plain Listener to avoid the macOS
        GlobalHotKeys bug where ``_on_press()`` crashes with:
            TypeError: GlobalHotKeys._on_press() missing 1 required
            positional argument: 'injected'
        A manual pressed-key set sidesteps the issue entirely.
        """
        try:
            from pynput import keyboard as pynput_keyboard

            _COMBO = {
                pynput_keyboard.Key.ctrl_l, pynput_keyboard.Key.ctrl_r,
                pynput_keyboard.Key.alt_l, pynput_keyboard.Key.alt_r,
            }
            _TARGET_CHAR = "j"
            _pressed: set = set()

            def _on_press(*args, **kwargs):
                # First positional arg is the key; extra 'injected' bool is absorbed by *args
                key = args[0] if args else kwargs.get("key")
                if key is None:
                    return
                _pressed.add(key)
                # Check: any ctrl + any alt + 'j'
                has_ctrl = _pressed & {pynput_keyboard.Key.ctrl_l, pynput_keyboard.Key.ctrl_r}
                has_alt  = _pressed & {pynput_keyboard.Key.alt_l, pynput_keyboard.Key.alt_r}
                try:
                    has_j = hasattr(key, "char") and key.char == _TARGET_CHAR
                except AttributeError:
                    has_j = False
                if has_ctrl and has_alt and has_j:
                    self._root.after(0, self._prompt_task)

            def _on_release(*args, **kwargs):
                key = args[0] if args else kwargs.get("key")
                _pressed.discard(key)

            self._hotkey_listener = pynput_keyboard.Listener(
                on_press=_on_press, on_release=_on_release,
            )
            self._hotkey_listener.daemon = True
            self._hotkey_listener.start()
            logger.info("Hotkey Ctrl+Alt+J registered (pynput Listener)")
        except Exception as e:
            logger.warning(f"Could not register hotkey: {e}")

    # ── menu callbacks (called from tray thread → schedule on tk thread) ──────

    def _open_control_panel(self, icon=None, item=None) -> None:
        self._root.after(0, self._show_control_panel)

    def _prompt_task(self, icon=None, item=None) -> None:
        self._root.after(0, self._ask_and_run_task)

    def _open_settings(self, icon=None, item=None) -> None:
        self._root.after(0, self._show_settings)

    def _cancel_task(self, icon=None, item=None) -> None:
        self._root.after(0, self._do_cancel)

    def _quit(self, icon=None, item=None) -> None:
        self._root.after(0, self._do_quit)

    def _make_provider_setter(self, provider: str):
        def _set(icon=None, item=None):
            self._root.after(0, lambda: self._set_provider(provider))
        return _set

    def _make_account_setter(self, account_id: Optional[int]):
        def _set(icon=None, item=None):
            self._root.after(0, lambda: self._set_account(account_id))
        return _set

    # ── tkinter-thread actions ────────────────────────────────────────────────

    def _show_control_panel(self) -> None:
        if self._control_panel and self._control_panel.winfo_exists():
            self._control_panel.lift()
            self._control_panel.focus_force()
            return
        from tray.ui import ControlPanel
        self._control_panel = ControlPanel(self._root, self)
        self._control_panel.protocol(
            "WM_DELETE_WINDOW", self._control_panel.withdraw
        )

    def _ask_and_run_task(self) -> None:
        task = simpledialog.askstring(
            "Kim — Run Task",
            "Enter task:",
            parent=self._root,
        )
        if task and task.strip():
            self.submit_task(task.strip())
            # Show control panel so user can watch progress
            self._show_control_panel()

    def _show_settings(self) -> None:
        if self._settings_win and self._settings_win.winfo_exists():
            self._settings_win.lift()
            self._settings_win.focus_force()
            return
        from tray.settings import SettingsWindow
        self._settings_win = SettingsWindow(self._root, _CONFIG_PATH, _ENV_PATH)

    def _set_provider(self, provider: str) -> None:
        self._active_provider = provider
        self._config["provider"] = provider
        _save_provider(provider)
        logger.info(f"Provider switched to {provider}")
        # Rebuild menu so radio button reflects new state
        if self._icon:
            self._icon.menu = self._build_menu()
        if self._control_panel and self._control_panel.winfo_exists():
            self._control_panel.refresh_status()

    def _set_account(self, account_id: Optional[int]) -> None:
        self._active_account_id = account_id
        self._config["selected_account_id"] = account_id
        _save_config(self._config)
        logger.info(f"Account switched to {account_id}")
        # Rebuild menu
        if self._icon:
            self._icon.menu = self._build_menu()
        if self._control_panel and self._control_panel.winfo_exists():
            self._control_panel.refresh_status()

    def _do_cancel(self) -> None:
        self._bridge.cancel()
        self._runner.cancel_current()
        self._agent_running = False
        self._update_icon_colour(_COLOUR_IDLE)
        logger.info("Task cancelled by user")

    def _do_quit(self) -> None:
        self._do_cancel()
        self._voice.shutdown()
        self._root.quit()

    # ── task submission ───────────────────────────────────────────────────────

    def submit_task(self, task: str, task_id: Optional[str] = None) -> None:
        """Called from tkinter thread to start an agent task."""
        if self._agent_running:
            if task_id:
                # If it's a relay task, we just skip it (it will stay in queue or be re-polled)
                return
            messagebox.showwarning(
                "Kim", "An agent task is already running.", parent=self._root
            )
            return

        self._config = _load_config()
        self._bridge.reset()
        self._bridge.preview_mode = self._config.get("preview_mode", False)
        self._agent_running = True
        self._update_icon_colour(_COLOUR_RUNNING)

        async def _run():
            try:
                async with mcp_agent_context(
                    self._config,
                    provider_name=self._active_provider,
                    ui_bridge=self._bridge,
                    voice_engine=self._voice,
                ) as agent:
                    result = await agent.run(task)
                self._root.after(0, self._on_task_done, result, True, task_id)
            except asyncio.CancelledError:
                self._root.after(0, self._on_task_done, "Cancelled", False, task_id)
            except Exception as e:
                logger.error(f"Agent error: {e}", exc_info=True)
                self._root.after(0, self._on_task_done, str(e), False, task_id)

        self._runner.submit(_run())

    def _on_task_done(self, result, success: bool, task_id: Optional[str] = None) -> None:
        # Extract a clean summary for voice (before stringifying the full result)
        if isinstance(result, dict):
            voice_summary = result.get("summary", "")
            screenshot = result.get("screenshot", "")
        else:
            voice_summary = str(result)
            screenshot = ""

        # Stringify for display / logging
        display = voice_summary if voice_summary else str(result)
        self._agent_running = False
        colour = _COLOUR_IDLE if success else _COLOUR_ERROR
        self._update_icon_colour(colour)

        # Upload result to relay if this was a remote task
        if task_id:
            self._report_relay_result(task_id, display, screenshot, success)

        title = "Kim — Task Complete" if success else "Kim — Task Failed"
        _toast(title, display)
        logger.info(f"Task finished (success={success}): {display[:120]}")

        # Speak result summary (emotion tags like <sigh> are preserved for Maya-1)
        if self._voice.enabled and voice_summary:
            prefix = "Task complete." if success else "Task failed."
            self._voice.speak_fire_and_forget(f"{prefix} {voice_summary[:200]}")

        if self._control_panel and self._control_panel.winfo_exists():
            self._control_panel.on_task_done(display, success)

    def _report_relay_result(self, task_id: str, summary: str, screenshot: str, success: bool) -> None:
        relay_url = self._config.get("relay", {}).get("url")
        pc_key = os.environ.get("RELAY_PC_API_KEY")
        if not relay_url or not pc_key:
            return

        import httpx
        def _target():
            try:
                headers = {"x-api-key": pc_key}
                payload = {
                    "task_id": task_id,
                    "summary": summary,
                    "screenshot": screenshot,
                    "success": success
                }
                httpx.post(f"{relay_url}/result", json=payload, headers=headers, timeout=10.0)
                logger.info(f"Relay: Reported result for {task_id}")
            except Exception as e:
                logger.warning(f"Relay: Failed to report result for {task_id}: {e}")
        
        threading.Thread(target=_target, daemon=True).start()

    # ── cross-thread poll ─────────────────────────────────────────────────────

    def _poll(self) -> None:
        """Called every 50 ms on the Tkinter thread to forward bridge events."""
        # ── Screenshot blink: hide/show window (high priority) ────────────
        while True:
            try:
                action, event = self._bridge._visibility_queue.get_nowait()
                if action == "hide":
                    self._root.withdraw()
                    if self._control_panel and self._control_panel.winfo_exists():
                        self._control_panel.withdraw()
                    self._root.update_idletasks()
                elif action == "show":
                    self._root.deiconify()
                    if self._control_panel and self._control_panel.winfo_exists():
                        self._control_panel.deiconify()
                event.set()
            except queue.Empty:
                break

        # Forward log messages to control panel
        if self._control_panel and self._control_panel.winfo_exists():
            while True:
                try:
                    level, msg = self._bridge.log_queue.get_nowait()
                    self._control_panel.append_log(level, msg)
                except queue.Empty:
                    break

            # Forward confirmation requests
            try:
                tool_name, args, event, result = self._bridge._confirm_queue.get_nowait()
                self._control_panel.show_confirm(tool_name, args, event, result)
            except queue.Empty:
                pass
        else:
            # Drain queues so they don't grow unbounded when panel is closed
            while not self._bridge.log_queue.empty():
                try:
                    self._bridge.log_queue.get_nowait()
                except queue.Empty:
                    break
            # Restore panel to show confirmation
            try:
                tool_name, args, event, result = self._bridge._confirm_queue.get_nowait()
                self._show_control_panel()
                self._control_panel.show_confirm(tool_name, args, event, result)
            except queue.Empty:
                pass

        self._root.after(50, self._poll)


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    app = KimApp()
    app.run()


if __name__ == "__main__":
    main()

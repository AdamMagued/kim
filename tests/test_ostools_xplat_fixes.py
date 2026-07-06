"""Regression tests for the cross-platform + behavioral OS-tool fixes.

One test class per audit finding:

  2.1 — Retina/DPI: annotated-screenshot grid coords were emitted in PHYSICAL
        framebuffer pixels while pyautogui clicks in LOGICAL points
  2.2 — multi-monitor: grid coords dropped the selected monitor's virtual-
        desktop left/top offset
  2.3 — click_ui was missing the privacy-pause gate and reused stale coords
        with no staleness check
  2.4 — get_windows hardcoded visible=true on macOS/Linux
  2.5 — scroll compared direction case-sensitively ("Up" scrolled DOWN)
  1.1 — code.py _minimal_env hardcoded a POSIX PATH and omitted SystemRoot/
        ComSpec on Windows
  1.2 — shell deny list missed cmd.exe erase/rd; redirect block missed
        drive-absolute (C:\\...) and UNC targets
  1.3 — sandbox PATH missed /usr/local/bin (and /snap/bin); translated Linux
        app launches now resolve to absolute paths
  1.4 — type_text on Linux without xclip/xsel raised an unhelpful error;
        now falls back to direct key events with a clear message
  1.5 — open_url used webbrowser.open with no result check; Linux now shells
        out to xdg-open and checks the exit code

Many of these bugs are Windows/Linux-only and cannot be exercised on the
macOS CI host: those tests assert the platform-branch LOGIC (env dict
contents, deny-list membership, coordinate math, generated script text)
rather than real OS behavior.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

# Make sure the repo root is importable regardless of cwd
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp_server import os_utils as os_utils_mod
from mcp_server.tools import code as code_mod
from mcp_server.tools import keyboard as keyboard_mod
from mcp_server.tools import mouse as mouse_mod
from mcp_server.tools import screen as screen_mod
from mcp_server.tools import shell as shell_mod
from mcp_server.tools import ui_observe as ui_mod
from mcp_server.tools import windows as windows_mod


def _real_pillow(testcase: unittest.TestCase):
    """Evict conftest's file-less PIL stub and import real Pillow +
    annotate_screenshot, or skip when Pillow genuinely isn't installed."""
    for mod_name in ("PIL.Image", "PIL"):
        mod = sys.modules.get(mod_name)
        if mod is not None and not getattr(mod, "__file__", None):
            del sys.modules[mod_name]
    try:
        from PIL import Image, ImageDraw  # noqa: F401

        if not (hasattr(ImageDraw, "Draw") and hasattr(Image, "new")):
            raise ImportError("stubbed PIL")
        from mcp_server.tools.screen_annotator import annotate_screenshot
    except Exception as exc:  # noqa: BLE001
        testcase.skipTest(f"real Pillow not importable: {exc}")
    return Image, annotate_screenshot


class _FakePyAutoGui(types.SimpleNamespace):
    """Recording stub for the lazily-imported pyautogui module."""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple] = []

    def click(self, **kwargs):
        self.calls.append(("click", kwargs))

    def scroll(self, amount, **kwargs):
        self.calls.append(("scroll", amount, kwargs))

    def write(self, text, **kwargs):
        self.calls.append(("write", text, kwargs))

    def hotkey(self, *keys):
        self.calls.append(("hotkey", keys))


# ---------------------------------------------------------------------------
# 2.1 — grid coords must be in the click (logical) space, not framebuffer px
# ---------------------------------------------------------------------------
class DpiGridCoordinateTest(unittest.TestCase):
    def test_click_space_geometry_uses_monitor_dict_not_image_size(self):
        # Retina-style: mss monitor dict is logical (1440x900) while the
        # grabbed image is physical (2880x1800). Click space == monitor dict.
        mon = {"width": 1440, "height": 900, "left": 0, "top": 0}
        w, h, left, top = screen_mod._click_space_geometry(mon, 2880, 1800)
        self.assertEqual((w, h), (1440, 900))
        self.assertEqual((left, top), (0, 0))

    def test_click_space_geometry_falls_back_to_image_size(self):
        w, h, _, _ = screen_mod._click_space_geometry({}, 800, 600)
        self.assertEqual((w, h), (800, 600))

    def test_grid_map_scaled_to_logical_bounds(self):
        Image, annotate_screenshot = _real_pillow(self)

        # Image already downscaled to 720x450; logical screen is 1440x900.
        img = Image.new("RGB", (720, 450))
        _, grid = annotate_screenshot(
            img, original_width=1440, original_height=900
        )
        self.assertEqual(len(grid), 100)
        for x, y in grid.values():
            self.assertLessEqual(x, 1440)
            self.assertLessEqual(y, 900)
        # Exact math for the A1 marker: inset 4% of 720/450 = (28, 18),
        # scale = 2.0 in both axes.
        self.assertEqual(grid["A1"], [56, 36])


# ---------------------------------------------------------------------------
# 2.2 — monitor left/top offset must be added to grid coords
# ---------------------------------------------------------------------------
class MultiMonitorOffsetTest(unittest.TestCase):
    def test_offsets_added_to_every_marker(self):
        Image, annotate_screenshot = _real_pillow(self)

        img = Image.new("RGB", (720, 450))
        _, base = annotate_screenshot(img, original_width=1440, original_height=900)
        _, shifted = annotate_screenshot(
            img,
            original_width=1440,
            original_height=900,
            offset_x=-1920,
            offset_y=120,
        )
        for label, (bx, by) in base.items():
            sx, sy = shifted[label]
            self.assertEqual(sx, bx - 1920, f"{label} x offset wrong")
            self.assertEqual(sy, by + 120, f"{label} y offset wrong")

    def test_take_annotated_screenshot_end_to_end(self):
        """Fake mss: physical 400x300 grab of a logical 200x150 monitor at
        virtual-desktop offset (100, 50). All grid coords must land inside
        the monitor's logical global rect."""
        _real_pillow(self)  # handle_take_annotated_screenshot needs real PIL
        mon = {"width": 200, "height": 150, "left": 100, "top": 50}
        rgb = b"\x7f" * (400 * 300 * 3)

        class _FakeShot:
            size = (400, 300)

        _FakeShot.rgb = rgb

        class _FakeSct:
            monitors = [
                {"width": 200, "height": 150, "left": 0, "top": 0},  # virtual
                mon,
            ]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def grab(self, m):
                assert m is mon
                return _FakeShot()

        fake_mss = types.SimpleNamespace(mss=lambda: _FakeSct())
        with patch.dict(sys.modules, {"mss": fake_mss}):
            with patch.object(screen_mod, "is_privacy_paused", return_value=False):
                raw = asyncio.run(
                    screen_mod.handle_take_annotated_screenshot({"monitor": 1})
                )
        result = json.loads(raw)
        self.assertNotIn("error", result)
        self.assertEqual(result["screen_width"], 200)
        self.assertEqual(result["screen_height"], 150)
        self.assertEqual(result["monitor_left"], 100)
        self.assertEqual(result["monitor_top"], 50)
        for x, y in result["grid"].values():
            self.assertGreaterEqual(x, 100)
            self.assertLessEqual(x, 300)
            self.assertGreaterEqual(y, 50)
            self.assertLessEqual(y, 200)


# ---------------------------------------------------------------------------
# 2.3 — click_ui: privacy gate + staleness check
# ---------------------------------------------------------------------------
class ClickUiPrivacyAndStalenessTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved_elements = dict(ui_mod._LAST_ELEMENTS)
        self._saved_ts = ui_mod._LAST_OBSERVE_TS

    def tearDown(self):
        ui_mod._LAST_ELEMENTS = self._saved_elements
        ui_mod._LAST_OBSERVE_TS = self._saved_ts

    def _element(self):
        return ui_mod.UIElement("e1", "AXButton", "OK", "", "", 10, 20, 30, 40)

    async def test_privacy_pause_blocks_click_ui(self):
        ui_mod._LAST_ELEMENTS = {"e1": self._element()}
        ui_mod._LAST_OBSERVE_TS = time.monotonic()
        fake = _FakePyAutoGui()
        with patch.dict(sys.modules, {"pyautogui": fake}):
            with patch.object(ui_mod, "is_privacy_paused", return_value=True):
                result = await ui_mod.handle_click_ui({"element_id": "e1"})
        self.assertEqual(result, ui_mod.PRIVACY_ERROR)
        self.assertEqual(fake.calls, [], "no click may fire while privacy paused")

    async def test_stale_observation_is_refused(self):
        ui_mod._LAST_ELEMENTS = {"e1": self._element()}
        ui_mod._LAST_OBSERVE_TS = (
            time.monotonic() - ui_mod._MAX_OBSERVATION_AGE_S - 60
        )
        fake = _FakePyAutoGui()
        with patch.dict(sys.modules, {"pyautogui": fake}):
            with patch.object(ui_mod, "is_privacy_paused", return_value=False):
                result = await ui_mod.handle_click_ui({"element_id": "e1"})
        self.assertIn("stale", result.lower())
        self.assertEqual(fake.calls, [])

    async def test_fresh_observation_clicks(self):
        ui_mod._LAST_ELEMENTS = {"e1": self._element()}
        ui_mod._LAST_OBSERVE_TS = time.monotonic()
        fake = _FakePyAutoGui()
        with patch.dict(sys.modules, {"pyautogui": fake}):
            with patch.object(ui_mod, "is_privacy_paused", return_value=False):
                result = await ui_mod.handle_click_ui({"element_id": "e1"})
        self.assertIn("Clicked e1", result)
        self.assertEqual(len(fake.calls), 1)


# ---------------------------------------------------------------------------
# 2.4 — get_windows must not fabricate visible=true
# ---------------------------------------------------------------------------
class WindowVisibilityHonestyTest(unittest.IsolatedAsyncioTestCase):
    def test_mac_script_queries_axminimized(self):
        script = windows_mod._MAC_LIST_WINDOWS_SCRIPT
        self.assertIn("AXMinimized", script)
        self.assertIn('visible=" & visFlag', script)
        self.assertNotIn('visible=true"', script)

    async def test_linux_listing_reports_unknown_visibility(self):
        sample = "0x01c00003  0 10   20   800 600 host Some Window Title"

        async def fake_run_cmd(cmd):
            return (0, sample, "")

        with patch.object(windows_mod, "check_tool_available", return_value=True):
            with patch.object(windows_mod, "_run_cmd", fake_run_cmd):
                out = await windows_mod._get_windows_linux()
        self.assertIn("visible=unknown", out)
        self.assertNotIn("visible=true", out)


# ---------------------------------------------------------------------------
# 2.5 — scroll direction must be case/whitespace-insensitive
# ---------------------------------------------------------------------------
class ScrollDirectionTest(unittest.IsolatedAsyncioTestCase):
    async def _scroll(self, direction):
        fake = _FakePyAutoGui()
        with patch.dict(sys.modules, {"pyautogui": fake}):
            with patch.object(mouse_mod, "is_privacy_paused", return_value=False):
                result = await mouse_mod.handle_scroll(
                    {"direction": direction, "clicks": 3}
                )
        return result, fake.calls

    async def test_uppercase_up_scrolls_up(self):
        result, calls = await self._scroll("UP ")
        self.assertEqual(calls[0][1], 3, f"'UP ' must scroll up, got {calls}")
        self.assertNotIn("ERROR", result)

    async def test_mixed_case_down_scrolls_down(self):
        _, calls = await self._scroll("Down")
        self.assertEqual(calls[0][1], -3)

    async def test_unknown_direction_is_an_error_not_a_down_scroll(self):
        result, calls = await self._scroll("left")
        self.assertIn("ERROR", result)
        self.assertEqual(calls, [])


# ---------------------------------------------------------------------------
# 1.1 — code.py minimal env must be Windows-appropriate on Windows
# ---------------------------------------------------------------------------
class CodeMinimalEnvWindowsTest(unittest.TestCase):
    def test_windows_branch_env(self):
        fake_environ = {
            "SystemRoot": r"C:\Windows",
            "ComSpec": r"C:\Windows\System32\cmd.exe",
            "PATHEXT": ".COM;.EXE;.BAT",
            "OPENAI_API_KEY": "sk-secret",
        }
        with patch.object(code_mod, "IS_WINDOWS", True):
            with patch.dict(code_mod.os.environ, fake_environ, clear=True):
                env = code_mod._minimal_env({"EXTRA": "1"})
        self.assertEqual(env["SystemRoot"], r"C:\Windows")
        self.assertIn(r"C:\Windows\System32", env["Path"])
        self.assertEqual(env["ComSpec"], r"C:\Windows\System32\cmd.exe")
        self.assertEqual(env["PATHEXT"], ".COM;.EXE;.BAT")
        self.assertIn("TEMP", env)
        self.assertIn("TMP", env)
        self.assertIn("USERPROFILE", env)
        self.assertEqual(env["EXTRA"], "1")
        # No POSIX-style PATH and no secret leakage
        self.assertNotIn("PATH", env)
        self.assertNotIn("OPENAI_API_KEY", env)

    def test_posix_branch_unchanged_and_secret_free(self):
        with patch.object(code_mod, "IS_WINDOWS", False):
            with patch.dict(code_mod.os.environ, {"OPENAI_API_KEY": "sk-x"}):
                env = code_mod._minimal_env()
        self.assertEqual(env["PATH"], code_mod._SANDBOX_PATH)
        self.assertNotIn("OPENAI_API_KEY", env)


# ---------------------------------------------------------------------------
# 1.2 — cmd.exe aliases + drive-absolute/UNC redirect targets
# ---------------------------------------------------------------------------
class ShellDenyGapsTest(unittest.TestCase):
    def test_erase_and_rd_are_denied(self):
        self.assertIn("erase", shell_mod._DENY_COMMANDS)
        self.assertIn("rd", shell_mod._DENY_COMMANDS)
        self.assertIsNotNone(shell_mod._check_blocked(r"rd /s /q C:\Users\x"))
        self.assertIsNotNone(shell_mod._check_blocked("erase important.txt"))

    def test_drive_absolute_redirect_blocked(self):
        for cmd in (
            "echo pwned > C:/Windows/System32/drivers/etc/hosts",
            r"echo pwned > C:\Windows\System32\drivers\etc\hosts",
            "echo pwned >C:/Windows/hosts",
            r"echo pwned > \\server\share\f.txt",
        ):
            self.assertIsNotNone(
                shell_mod._check_blocked(cmd), f"should block: {cmd!r}"
            )

    def test_relative_and_devnull_redirects_still_allowed(self):
        self.assertIsNone(shell_mod._check_blocked("printf probe > out.txt"))
        self.assertIsNone(shell_mod._check_blocked("echo hi > /dev/null"))

    def test_redirect_target_helper(self):
        blocked = shell_mod._is_blocked_redirect_target
        self.assertTrue(blocked("/etc/passwd"))
        self.assertTrue(blocked("C:/Windows/hosts"))
        self.assertTrue(blocked("C:Windowshosts"))  # shlex-munged backslashes
        self.assertTrue(blocked("\\\\server\\share"))
        self.assertTrue(blocked("../up.txt"))
        self.assertFalse(blocked("out.txt"))
        self.assertFalse(blocked("/dev/null"))


# ---------------------------------------------------------------------------
# 1.3 — sandbox PATH coverage + absolute-path app resolution on Linux
# ---------------------------------------------------------------------------
class SandboxPathAndLinuxResolveTest(unittest.TestCase):
    def test_sandbox_paths_include_usr_local_bin(self):
        self.assertIn("/usr/local/bin", shell_mod._SANDBOX_PATH)
        self.assertIn("/usr/local/bin", code_mod._SANDBOX_PATH)

    def test_linux_start_translation_resolves_absolute_path(self):
        with patch.object(os_utils_mod, "IS_MACOS", False), \
                patch.object(os_utils_mod, "IS_LINUX", True), \
                patch.object(
                    os_utils_mod.shutil, "which",
                    lambda name: f"/snap/bin/{name}",
                ):
            translated = os_utils_mod._translate_start_command("notepad")
            self.assertEqual(translated, "/snap/bin/gedit")
            exe = os_utils_mod._translate_exe_invocation("calc.exe", " --arg")
            self.assertEqual(exe, "/snap/bin/gnome-calculator --arg")
            fallback = os_utils_mod._translate_start_command("somefile.txt")
            self.assertTrue(fallback.startswith("/snap/bin/xdg-open "))

    def test_linux_resolution_falls_back_to_bare_name(self):
        with patch.object(os_utils_mod.shutil, "which", lambda name: None):
            self.assertEqual(os_utils_mod._resolve_linux_app("gedit"), "gedit")


# ---------------------------------------------------------------------------
# 1.4 — type_text clipboard failure falls back to direct typing
# ---------------------------------------------------------------------------
class TypeTextClipboardFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_to_direct_typing_with_clear_message(self):
        fake_gui = _FakePyAutoGui()

        class _NoClipboard(types.SimpleNamespace):
            @staticmethod
            def paste():
                raise RuntimeError("Pyperclip could not find a copy/paste mechanism")

            @staticmethod
            def copy(_text):
                raise RuntimeError("Pyperclip could not find a copy/paste mechanism")

        with patch.dict(
            sys.modules, {"pyautogui": fake_gui, "pyperclip": _NoClipboard()}
        ):
            with patch.object(keyboard_mod, "is_privacy_paused", return_value=False):
                result = await keyboard_mod.handle_type_text({"text": "hello"})
        self.assertNotIn("ERROR", result)
        self.assertIn("direct key events", result)
        self.assertIn("xclip", result)
        self.assertEqual(fake_gui.calls[0][:2], ("write", "hello"))

    async def test_both_paths_failing_yields_install_hint(self):
        class _BrokenGui(types.SimpleNamespace):
            @staticmethod
            def write(_text, **_kw):
                raise RuntimeError("no X display")

        class _NoClipboard(types.SimpleNamespace):
            @staticmethod
            def paste():
                raise RuntimeError("no mechanism")

            @staticmethod
            def copy(_text):
                raise RuntimeError("no mechanism")

        with patch.dict(
            sys.modules, {"pyautogui": _BrokenGui(), "pyperclip": _NoClipboard()}
        ):
            with patch.object(keyboard_mod, "is_privacy_paused", return_value=False):
                result = await keyboard_mod.handle_type_text({"text": "hello"})
        self.assertIn("ERROR", result)
        self.assertIn("xclip", result)


# ---------------------------------------------------------------------------
# 1.5 — open_url must surface launch failures
# ---------------------------------------------------------------------------
class OpenUrlFailureSurfacingTest(unittest.IsolatedAsyncioTestCase):
    async def test_linux_xdg_open_nonzero_exit_is_an_error(self):
        async def fake_run_cmd(cmd):
            assert cmd[0] == "xdg-open"
            return (4, "", "no method available for opening")

        with patch.object(windows_mod, "IS_LINUX", True), \
                patch.object(windows_mod, "check_tool_available", return_value=True), \
                patch.object(windows_mod, "_run_cmd", fake_run_cmd):
            result = await windows_mod.handle_open_url({"url": "https://x.test"})
        self.assertIn("ERROR", result)
        self.assertIn("exited with code 4", result)

    async def test_linux_xdg_open_success(self):
        async def fake_run_cmd(cmd):
            return (0, "", "")

        with patch.object(windows_mod, "IS_LINUX", True), \
                patch.object(windows_mod, "check_tool_available", return_value=True), \
                patch.object(windows_mod, "_run_cmd", fake_run_cmd):
            result = await windows_mod.handle_open_url({"url": "https://x.test"})
        self.assertIn("Opened URL", result)

    async def test_webbrowser_false_return_is_an_error(self):
        with patch.object(windows_mod, "IS_LINUX", False), \
                patch.object(windows_mod.webbrowser, "open", return_value=False):
            result = await windows_mod.handle_open_url({"url": "https://x.test"})
        self.assertIn("ERROR", result)

    async def test_scheme_check_still_enforced(self):
        result = await windows_mod.handle_open_url({"url": "file:///etc/passwd"})
        self.assertIn("not allowed", result)


if __name__ == "__main__":
    unittest.main()

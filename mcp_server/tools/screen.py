import base64
import ctypes
import io
import json
import logging
import os

from ..privacy import is_privacy_paused, PRIVACY_ERROR

logger = logging.getLogger(__name__)


def _click_space_geometry(
    monitor: dict, img_w: int, img_h: int
) -> tuple[int, int, int, int]:
    """Return (width, height, left, top) of a captured monitor in the CLICK
    coordinate space — the space pyautogui uses.

    2.1 (Retina/DPI): mss grabs the framebuffer in PHYSICAL pixels (e.g.
    2880x1800 on a 2x Retina display) while pyautogui clicks in LOGICAL
    points (1440x900).  The mss monitor dict, however, is reported in the
    same coordinate space pyautogui uses (CGDisplayBounds points on macOS;
    physical pixels on Windows/Linux where pyautogui is DPI-aware too), so
    the monitor dict — not the grabbed image size — defines the click space.

    2.2 (multi-monitor): the monitor's left/top offset in the global virtual
    desktop must be added to any coordinate meant for clicking, because the
    grabbed image is local to the monitor while pyautogui is global.
    """
    width = int(monitor.get("width", 0)) or img_w
    height = int(monitor.get("height", 0)) or img_h
    left = int(monitor.get("left", 0))
    top = int(monitor.get("top", 0))
    return (width, height, left, top)


async def handle_take_screenshot(args: dict) -> str:
    if is_privacy_paused():  # K9
        return PRIVACY_ERROR
    import mss
    from PIL import Image

    scale = float(args.get("scale", 0.75))
    monitor_index = int(args.get("monitor", 1))
    try:
        with mss.mss() as sct:
            monitors = sct.monitors
            if monitor_index >= len(monitors):
                monitor_index = 1
            screenshot = sct.grab(monitors[monitor_index])
            img = Image.frombytes("RGB", screenshot.size, screenshot.rgb)
        if scale != 1.0:
            new_size = (int(img.width * scale), int(img.height * scale))
            img = img.resize(new_size, Image.LANCZOS)  # type: ignore[attr-defined]
        buffer = io.BytesIO()
        img.save(buffer, format="PNG", optimize=True)
        b64 = base64.b64encode(buffer.getvalue()).decode()
        logger.info(f"take_screenshot: {img.size} scale={scale} ({len(b64)} b64 chars)")
        return f"data:image/png;base64,{b64}"
    except Exception as e:
        logger.error(f"take_screenshot failed: {e}", exc_info=True)
        return f"ERROR: {e}"


async def handle_get_screen_info(args: dict) -> str:
    import platform
    lines = []
    
    if platform.system() == "Windows":
        try:
            import ctypes
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            user32.SetProcessDPIAware()
            width = user32.GetSystemMetrics(0)
            height = user32.GetSystemMetrics(1)
            hdc = ctypes.windll.gdi32.CreateDCW("DISPLAY", None, None, None)  # type: ignore[attr-defined]
            dpi_x = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # type: ignore[attr-defined]
            dpi_y = ctypes.windll.gdi32.GetDeviceCaps(hdc, 90)  # type: ignore[attr-defined]
            ctypes.windll.gdi32.DeleteDC(hdc)  # type: ignore[attr-defined]
            lines.extend([
                f"Primary resolution: {width}x{height}",
                f"DPI: {dpi_x}x{dpi_y}",
            ])
        except Exception as e:
            logger.warning(f"Failed to get Windows-specific screen info: {e}")

    try:
        import mss
        with mss.mss() as sct:
            monitor_count = len(sct.monitors) - 1
            monitors_info = []
            for i, m in enumerate(sct.monitors[1:], 1):
                monitors_info.append(
                    f"  Monitor {i}: {m['width']}x{m['height']} at ({m['left']},{m['top']})"
                )
            
            if not lines and monitor_count > 0:
                m1 = sct.monitors[1]
                lines.append(f"Primary resolution: {m1['width']}x{m1['height']}")
                
            lines.extend([
                f"Monitor count: {monitor_count}",
                "Monitors:",
            ] + monitors_info)
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"get_screen_info failed: {e}", exc_info=True)
        return f"ERROR: {e}"


async def handle_take_annotated_screenshot(args: dict) -> str:
    """Capture the screen and overlay a visual ruler grid for precise clicking.

    Returns a JSON string with:
      - ``image``: base64-encoded annotated PNG (``data:image/png;base64,...``)
      - ``grid``: mapping of marker labels to real-screen ``[x, y]`` coordinates
      - ``screen_width`` / ``screen_height``: actual monitor dimensions
      - ``instructions``: guidance for the LLM on how to use the grid

    The grid markers are drawn ON the image only — the user's actual screen
    is never modified.
    """
    if is_privacy_paused():  # K9
        return PRIVACY_ERROR
    import mss
    from PIL import Image
    from mcp_server.tools.screen_annotator import annotate_screenshot

    scale = float(args.get("scale", 0.75))
    monitor_index = int(args.get("monitor", 1))
    grid_cols = int(args.get("grid_cols", 10))
    grid_rows = int(args.get("grid_rows", 10))

    try:
        with mss.mss() as sct:
            monitors = sct.monitors
            if monitor_index >= len(monitors):
                monitor_index = 1
            selected_monitor = dict(monitors[monitor_index])
            screenshot = sct.grab(monitors[monitor_index])
            img = Image.frombytes("RGB", screenshot.size, screenshot.rgb)

        # Click-space geometry (2.1 DPI + 2.2 multi-monitor): grid coords must
        # be expressed in pyautogui's logical/global coordinate space, NOT in
        # the physical pixel space of the grabbed framebuffer.
        click_w, click_h, mon_left, mon_top = _click_space_geometry(
            selected_monitor, img.width, img.height
        )

        # Scale down (same as take_screenshot)
        if scale != 1.0:
            new_size = (int(img.width * scale), int(img.height * scale))
            img = img.resize(new_size, Image.LANCZOS)  # type: ignore[attr-defined]

        # Draw the ruler grid
        annotated_img, grid_map = annotate_screenshot(
            img,
            grid_cols=grid_cols,
            grid_rows=grid_rows,
            original_width=click_w,
            original_height=click_h,
            offset_x=mon_left,
            offset_y=mon_top,
        )

        # Encode to base64
        buffer = io.BytesIO()
        annotated_img.save(buffer, format="PNG", optimize=True)
        b64 = base64.b64encode(buffer.getvalue()).decode()

        logger.info(
            f"take_annotated_screenshot: {annotated_img.size} scale={scale} "
            f"grid={grid_cols}x{grid_rows} ({len(b64)} b64 chars)"
        )

        result = {
            "image": f"data:image/png;base64,{b64}",
            "grid": grid_map,
            "screen_width": click_w,
            "screen_height": click_h,
            "monitor_left": mon_left,
            "monitor_top": mon_top,
            "instructions": (
                f"The screenshot has a grid of labeled markers (columns A-"
                f"{chr(ord('A') + grid_cols - 1)}, rows 1-{grid_rows}). "
                "Each marker label (e.g. 'A1', 'E5') maps to exact screen coordinates in the 'grid' field. "
                "To click a target: find the two nearest markers, note their coordinates from the grid, "
                "and interpolate to estimate the target's exact (x, y) position. "
                "Output the coordinates as integers for the 'click' tool."
            ),
        }
        return json.dumps(result)

    except Exception as e:
        logger.error(f"take_annotated_screenshot failed: {e}", exc_info=True)
        return json.dumps({"error": str(e)})

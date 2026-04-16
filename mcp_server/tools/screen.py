import base64
import ctypes
import io
import logging

logger = logging.getLogger(__name__)


async def handle_take_screenshot(args: dict) -> str:
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
            img = img.resize(new_size, Image.LANCZOS)
        buffer = io.BytesIO()
        img.save(buffer, format="PNG", optimize=True)
        b64 = base64.b64encode(buffer.getvalue()).decode()
        logger.info(f"take_screenshot: {img.size} scale={scale} ({len(b64)} b64 chars)")
        return f"data:image/png;base64,{b64}"
    except Exception as e:
        logger.error(f"take_screenshot failed: {e}", exc_info=True)
        return f"ERROR: {e}"


async def handle_get_screen_info(args: dict) -> str:
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDPIAware()
        width = user32.GetSystemMetrics(0)
        height = user32.GetSystemMetrics(1)
        hdc = ctypes.windll.gdi32.CreateDCW("DISPLAY", None, None, None)
        dpi_x = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)
        dpi_y = ctypes.windll.gdi32.GetDeviceCaps(hdc, 90)
        ctypes.windll.gdi32.DeleteDC(hdc)
        import mss
        with mss.mss() as sct:
            monitor_count = len(sct.monitors) - 1
            monitors_info = []
            for i, m in enumerate(sct.monitors[1:], 1):
                monitors_info.append(
                    f"  Monitor {i}: {m['width']}x{m['height']} at ({m['left']},{m['top']})"
                )
        lines = [
            f"Primary resolution: {width}x{height}",
            f"DPI: {dpi_x}x{dpi_y}",
            f"Monitor count: {monitor_count}",
            "Monitors:",
        ] + monitors_info
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"get_screen_info failed: {e}", exc_info=True)
        return f"ERROR: {e}"

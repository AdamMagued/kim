import logging
import webbrowser

logger = logging.getLogger(__name__)


async def handle_get_windows(args: dict) -> str:
    try:
        import pygetwindow as gw
        windows = gw.getAllWindows()
        lines = []
        for w in windows:
            if w.title.strip():
                lines.append(
                    f"title={w.title!r:50s}  pos=({w.left},{w.top})  "
                    f"size=({w.width}x{w.height})  visible={w.visible}"
                )
        logger.info(f"get_windows: {len(lines)} windows")
        return "\n".join(lines) if lines else "No windows found"
    except Exception as e:
        logger.error(f"get_windows failed: {e}", exc_info=True)
        return f"ERROR: {e}"


async def handle_focus_window(args: dict) -> str:
    title = str(args["title"])
    try:
        import pygetwindow as gw
        matches = gw.getWindowsWithTitle(title)
        if not matches:
            return f"ERROR: No window found with title containing '{title}'"
        win = matches[0]
        if win.isMinimized:
            win.restore()
        win.activate()
        logger.info(f"focus_window: {win.title!r}")
        return f"Focused window: {win.title!r}"
    except Exception as e:
        logger.error(f"focus_window failed: {e}", exc_info=True)
        return f"ERROR: {e}"


async def handle_resize_window(args: dict) -> str:
    title = str(args["title"])
    x = int(args.get("x", 0))
    y = int(args.get("y", 0))
    width = int(args.get("width", 800))
    height = int(args.get("height", 600))
    try:
        import pygetwindow as gw
        matches = gw.getWindowsWithTitle(title)
        if not matches:
            return f"ERROR: No window found with title containing '{title}'"
        win = matches[0]
        if win.isMinimized:
            win.restore()
        win.moveTo(x, y)
        win.resizeTo(width, height)
        logger.info(f"resize_window: {win.title!r} -> ({x},{y}) {width}x{height}")
        return f"Resized '{win.title}' to ({x},{y}) {width}x{height}"
    except Exception as e:
        logger.error(f"resize_window failed: {e}", exc_info=True)
        return f"ERROR: {e}"


async def handle_open_url(args: dict) -> str:
    url = str(args["url"])
    try:
        webbrowser.open(url)
        logger.info(f"open_url: {url}")
        return f"Opened URL in default browser: {url}"
    except Exception as e:
        logger.error(f"open_url failed: {e}", exc_info=True)
        return f"ERROR: {e}"

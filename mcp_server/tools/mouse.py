import asyncio
import logging

from ..privacy import is_privacy_paused, PRIVACY_ERROR

logger = logging.getLogger(__name__)

# Input clamps
_MAX_CLICKS = 10
_MAX_DURATION = 10.0
_MIN_DURATION = 0.0
_MAX_SCROLL_CLICKS = 50


async def handle_click(args: dict) -> str:
    if is_privacy_paused():  # K9
        return PRIVACY_ERROR
    import pyautogui
    x = int(args["x"])
    y = int(args["y"])
    button = str(args.get("button", "left"))
    clicks = max(1, min(int(args.get("clicks", 1)), _MAX_CLICKS))
    try:
        pyautogui.click(x=x, y=y, button=button, clicks=clicks, interval=0.1)
        logger.info(f"click: ({x},{y}) button={button} clicks={clicks}")
        return f"Clicked ({x},{y}) with {button} button x{clicks}"
    except Exception as e:
        logger.error(f"click failed: {e}", exc_info=True)
        return f"ERROR: {e}"


async def handle_double_click(args: dict) -> str:
    if is_privacy_paused():  # K9
        return PRIVACY_ERROR
    import pyautogui
    x = int(args["x"])
    y = int(args["y"])
    try:
        pyautogui.doubleClick(x=x, y=y)
        logger.info(f"double_click: ({x},{y})")
        return f"Double-clicked ({x},{y})"
    except Exception as e:
        logger.error(f"double_click failed: {e}", exc_info=True)
        return f"ERROR: {e}"


async def handle_right_click(args: dict) -> str:
    if is_privacy_paused():  # K9
        return PRIVACY_ERROR
    import pyautogui
    x = int(args["x"])
    y = int(args["y"])
    try:
        pyautogui.rightClick(x=x, y=y)
        logger.info(f"right_click: ({x},{y})")
        return f"Right-clicked ({x},{y})"
    except Exception as e:
        logger.error(f"right_click failed: {e}", exc_info=True)
        return f"ERROR: {e}"


async def handle_drag(args: dict) -> str:
    if is_privacy_paused():  # K9
        return PRIVACY_ERROR
    import pyautogui
    x1 = int(args["x1"])
    y1 = int(args["y1"])
    x2 = int(args["x2"])
    y2 = int(args["y2"])
    duration = max(_MIN_DURATION, min(float(args.get("duration", 0.5)), _MAX_DURATION))
    try:
        pyautogui.moveTo(x1, y1)
        await asyncio.sleep(0.1)
        pyautogui.dragTo(x2, y2, duration=duration, button="left")
        logger.info(f"drag: ({x1},{y1}) -> ({x2},{y2})")
        return f"Dragged from ({x1},{y1}) to ({x2},{y2})"
    except Exception as e:
        logger.error(f"drag failed: {e}", exc_info=True)
        return f"ERROR: {e}"


async def handle_scroll(args: dict) -> str:
    if is_privacy_paused():  # K9
        return PRIVACY_ERROR
    import pyautogui
    x = int(args.get("x", -1))
    y = int(args.get("y", -1))
    clicks = max(1, min(int(args.get("clicks", 3)), _MAX_SCROLL_CLICKS))
    # Normalize case/whitespace: "Up"/"UP " must scroll up, not silently down.
    direction = str(args.get("direction", "up")).strip().lower()
    if direction not in ("up", "down"):
        return f"ERROR: invalid scroll direction {direction!r}; use 'up' or 'down'"
    amount = clicks if direction == "up" else -clicks
    try:
        if x >= 0 and y >= 0:
            pyautogui.scroll(amount, x=x, y=y)
        else:
            pyautogui.scroll(amount)
        logger.info(f"scroll: ({x},{y}) direction={direction} clicks={clicks}")
        return f"Scrolled {direction} {clicks} clicks at ({x},{y})"
    except Exception as e:
        logger.error(f"scroll failed: {e}", exc_info=True)
        return f"ERROR: {e}"

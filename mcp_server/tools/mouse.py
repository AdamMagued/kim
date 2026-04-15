import asyncio
import logging

logger = logging.getLogger(__name__)


async def handle_click(args: dict) -> str:
    import pyautogui
    x = int(args["x"])
    y = int(args["y"])
    button = str(args.get("button", "left"))
    clicks = int(args.get("clicks", 1))
    try:
        pyautogui.click(x=x, y=y, button=button, clicks=clicks, interval=0.1)
        logger.info(f"click: ({x},{y}) button={button} clicks={clicks}")
        return f"Clicked ({x},{y}) with {button} button x{clicks}"
    except Exception as e:
        logger.error(f"click failed: {e}", exc_info=True)
        return f"ERROR: {e}"


async def handle_double_click(args: dict) -> str:
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
    import pyautogui
    x1 = int(args["x1"])
    y1 = int(args["y1"])
    x2 = int(args["x2"])
    y2 = int(args["y2"])
    duration = float(args.get("duration", 0.5))
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
    import pyautogui
    x = int(args.get("x", -1))
    y = int(args.get("y", -1))
    clicks = int(args.get("clicks", 3))
    direction = str(args.get("direction", "up"))
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

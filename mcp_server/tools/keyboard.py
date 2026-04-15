import logging

logger = logging.getLogger(__name__)


async def handle_type_text(args: dict) -> str:
    import pyautogui
    text = str(args["text"])
    interval = float(args.get("interval", 0.02))
    try:
        pyautogui.typewrite(text, interval=interval)
        logger.info(f"type_text: {len(text)} chars")
        return f"Typed {len(text)} characters"
    except Exception as e:
        logger.error(f"type_text failed: {e}", exc_info=True)
        return f"ERROR: {e}"


async def handle_hotkey(args: dict) -> str:
    import pyautogui
    keys = args["keys"]
    if isinstance(keys, str):
        keys = [k.strip() for k in keys.replace("+", ",").split(",")]
    try:
        pyautogui.hotkey(*keys)
        logger.info(f"hotkey: {keys}")
        return f"Pressed hotkey: {'+'.join(keys)}"
    except Exception as e:
        logger.error(f"hotkey failed: {e}", exc_info=True)
        return f"ERROR: {e}"


async def handle_key_press(args: dict) -> str:
    import pyautogui
    key = str(args["key"])
    presses = int(args.get("presses", 1))
    interval = float(args.get("interval", 0.1))
    try:
        pyautogui.press(key, presses=presses, interval=interval)
        logger.info(f"key_press: {key} x{presses}")
        return f"Pressed key '{key}' x{presses}"
    except Exception as e:
        logger.error(f"key_press failed: {e}", exc_info=True)
        return f"ERROR: {e}"

import logging

from ..privacy import is_privacy_paused, PRIVACY_ERROR

logger = logging.getLogger(__name__)

# Input clamps
_MAX_PRESSES = 50
_MIN_INTERVAL = 0.05
_MAX_INTERVAL = 5.0


async def handle_type_text(args: dict) -> str:
    if is_privacy_paused():  # K9
        return PRIVACY_ERROR
    import asyncio
    import pyautogui
    import pyperclip
    import sys
    text = str(args["text"])
    # L7: don't permanently clobber the user's clipboard (which may hold a
    # password) — save it and restore it after the paste lands.
    previous_clipboard: str | None = None
    try:
        previous_clipboard = pyperclip.paste()
    except Exception:  # noqa: BLE001 — clipboard read may fail; typing still works
        previous_clipboard = None
    try:
        pyperclip.copy(text)
        if sys.platform == "darwin":
            pyautogui.hotkey("command", "v")
        else:
            pyautogui.hotkey("ctrl", "v")
        logger.info(f"type_text: {len(text)} chars")
        return f"Typed {len(text)} characters (via clipboard)"
    except Exception as e:
        logger.error(f"type_text failed: {e}", exc_info=True)
        return f"ERROR: {e}"
    finally:
        if previous_clipboard is not None:
            # Give the target app a beat to consume the paste before the
            # clipboard flips back.
            await asyncio.sleep(0.5)
            try:
                pyperclip.copy(previous_clipboard)
            except Exception:  # noqa: BLE001
                pass


async def handle_hotkey(args: dict) -> str:
    if is_privacy_paused():  # K9
        return PRIVACY_ERROR
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
    if is_privacy_paused():  # K9
        return PRIVACY_ERROR
    import pyautogui
    key = str(args["key"])
    presses = max(1, min(int(args.get("presses", 1)), _MAX_PRESSES))
    interval = max(_MIN_INTERVAL, min(float(args.get("interval", 0.1)), _MAX_INTERVAL))
    try:
        pyautogui.press(key, presses=presses, interval=interval)
        logger.info(f"key_press: {key} x{presses}")
        return f"Pressed key '{key}' x{presses}"
    except Exception as e:
        logger.error(f"key_press failed: {e}", exc_info=True)
        return f"ERROR: {e}"

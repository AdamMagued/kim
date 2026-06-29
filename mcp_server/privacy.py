"""K9: privacy pause sentinel.

A local sentinel file (``~/.kim/privacy_pause``) toggled by the desktop app's
``set_privacy_pause`` Tauri command. While present, screen-capture tools refuse
to run and return a typed error so the agent informs the user rather than looping.

This is a local convenience flag, NOT a security boundary.
"""

from pathlib import Path

PRIVACY_SENTINEL = Path.home() / ".kim" / "privacy_pause"

# Typed error returned by capture tools while paused. JSON string so the agent
# can recognize it programmatically.
PRIVACY_ERROR = (
    '{"error": "privacy_pause", "message": "Privacy pause is on; screen capture '
    'is blocked. Tell the user it is paused instead of retrying."}'
)


def is_privacy_paused() -> bool:
    try:
        return PRIVACY_SENTINEL.exists()
    except Exception:
        # Fail closed: if we cannot determine whether privacy is paused,
        # treat it as paused so capture does not proceed unexpectedly.
        return True

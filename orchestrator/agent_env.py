"""
OS/platform detection extracted from orchestrator/agent.py.

_detect_os() returns (os_display_name, launch_example, path_style).
The three module constants are computed once at import time.
"""

import platform


def _detect_os() -> tuple[str, str, str]:
    """Return (os_display_name, launch_example, path_style)."""
    system = platform.system()
    if system == "Darwin":
        return (
            "macOS",
            "`open -a 'TextEdit'`",
            "POSIX paths (e.g. /Users/...)",
        )
    elif system == "Linux":
        return (
            "Linux",
            "`xdg-open` or `gedit`",
            "POSIX paths (e.g. /home/...)",
        )
    else:
        return (
            "Windows",
            "`start notepad.exe`",
            "Windows paths (e.g. C:\\...)",
        )


_OS_NAME, _LAUNCH_EXAMPLE, _PATH_STYLE = _detect_os()

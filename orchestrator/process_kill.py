"""Cross-platform process-tree kill for Codex subprocess cleanup.

Windows has no POSIX process groups: use an absolute taskkill.exe /T /F tree
kill with a ctypes TerminateProcess fallback. POSIX keeps killpg semantics.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path


def _kill_process_tree(pid: int) -> None:
    """Kill one Codex process tree without using POSIX APIs on Windows."""
    if sys.platform == "win32":
        system_root = os.environ.get("SYSTEMROOT") or os.environ.get("SystemRoot", r"C:\Windows")
        taskkill = str(Path(system_root) / "System32" / "taskkill.exe")
        try:
            result = subprocess.run(
                [taskkill, "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            if result.returncode in (0, 128):
                return
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)  # type: ignore[attr-defined]
            if handle:
                try:
                    ctypes.windll.kernel32.TerminateProcess(handle, 1)  # type: ignore[attr-defined]
                finally:
                    ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass
        return

    try:
        pgid = os.getpgid(pid)
    except OSError:
        return
    if pgid == pid:
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass

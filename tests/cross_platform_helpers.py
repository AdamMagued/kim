"""Cross-platform helpers for tests that need a real executable on disk."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def write_python_executable(directory: Path, name: str, source: str) -> Path:
    """Write a Python shebang script plus a PATHEXT-aware Windows launcher.

    POSIX executes the extensionless shebang script. Windows cannot do that via
    ``CreateProcess``, so it executes the sibling ``.cmd`` file instead. Both
    launch the same source file and forward the original argv unchanged.
    """
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text(f"#!{sys.executable}\n{source.lstrip()}", encoding="utf-8")
    script.chmod(0o755)

    launcher = directory / f"{name}.cmd"
    launcher.write_text(
        f'@"{sys.executable}" "%~dp0{name}" %*\r\n',
        encoding="utf-8",
    )
    return launcher if os.name == "nt" else script

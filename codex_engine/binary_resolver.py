"""Resolves the codex/kimcli backend binary path (#61).

Small, standalone module so ``codex_engine/engine.py`` — already at the
file-size gate's cap (see ``scripts/check_file_size_gate.py``) — does not
have to grow to gain the kimcli fallback.

Fallback chain (first match wins):
  1. ``CODEX_BIN`` env var, if non-empty (caller/user override; not verified
     to exist here — callers already handle a missing/broken override).
  2. ``~/.kim/bin/kimcli`` (scripts/install_kimcli.sh's install target), if
     it exists and is executable.
  3. ``kimcli`` resolved on PATH.
  4. ``codex`` resolved on PATH.
  5. ``"codex"`` — bare last resort, matching the pre-#61 default so a box
     with neither installed keeps failing the same way it always did
     (caller does the final existence check and error message).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def resolve_codex_binary() -> str:
    """Best-effort resolution of the codex/kimcli binary.

    Returns an absolute path when one of the concrete fallbacks matches, or
    the bare string ``"codex"`` when nothing does (current pre-#61
    behavior — callers ``shutil.which()`` this themselves and handle it not
    being found).
    """
    env_bin = os.environ.get("CODEX_BIN", "").strip()
    if env_bin:
        return env_bin

    kim_bin = Path.home() / ".kim" / "bin" / "kimcli"
    if kim_bin.exists() and os.access(kim_bin, os.X_OK):
        return str(kim_bin)

    found = shutil.which("kimcli")
    if found:
        return found

    found = shutil.which("codex")
    if found:
        return found

    return "codex"

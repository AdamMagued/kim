"""
Project-scoped persistent agent memory.

Provides two MCP tools:
    write_memory(key, value, cwd=None)  -- store a named finding
    read_memory(key=None, cwd=None)     -- retrieve one key or list all

Storage: kim_memory/<basename>-<hash8>.json under PROJECT_ROOT.
Each file is a plain JSON dict (key -> string value) so it is human-readable
and can be inspected or edited without tooling.

Scoping: the optional cwd parameter determines which project's memory is
accessed.  If omitted, PROJECT_ROOT is used.  The filename is derived from
the last path component of the resolved cwd plus the first 8 hex chars of its
MD5, giving readable and collision-resistant filenames:
    e.g.  kim-pro-a1b2c3d4.json
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from mcp_server.config import PROJECT_ROOT
from mcp_server.tools._errors import tool_error

logger = logging.getLogger(__name__)

_MEMORY_DIR = PROJECT_ROOT / "kim_memory"
_MAX_KEY_LEN = 256
_MAX_VALUE_LEN = 16_384


# -- Internal helpers ---------------------------------------------------------


def _memory_file(cwd: str | None) -> Path:
    """Return the JSON memory file path for the given cwd (or PROJECT_ROOT)."""
    if cwd:
        cwd = str(cwd).strip() or None
    resolved = Path(cwd).resolve() if cwd else PROJECT_ROOT
    basename = resolved.name or "root"
    # Sanitise: keep alphanumeric, replace everything else with '-'
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in basename)
    safe = safe.strip("-") or "project"
    digest = hashlib.md5(str(resolved).encode()).hexdigest()[:8]
    return _MEMORY_DIR / f"{safe}-{digest}.json"


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("memory: failed to read %s: %s", path, e)
        return {}


def _save(path: Path, data: dict) -> None:
    """Atomically write data to path using a sibling .tmp file."""
    _MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


# -- Tool handlers ------------------------------------------------------------


async def handle_write_memory(args: dict) -> str:
    key = str(args.get("key", "")).strip()
    value = str(args.get("value", ""))
    cwd = args.get("cwd") or None

    if not key:
        return tool_error("key is required")
    if len(key) > _MAX_KEY_LEN:
        return tool_error(f"key too long (max {_MAX_KEY_LEN} chars)")
    if len(value) > _MAX_VALUE_LEN:
        return tool_error(f"value too long (max {_MAX_VALUE_LEN} chars)")

    path = _memory_file(cwd)
    data = _load(path)
    data[key] = value
    try:
        _save(path, data)
    except Exception as e:
        logger.error("memory: write failed for %s: %s", path, e)
        return tool_error(f"could not persist memory: {e}")

    logger.info("memory: wrote key=%r to %s", key, path)
    return f"OK: stored {key!r} ({len(value)} chars) in {path.name}"


async def handle_read_memory(args: dict) -> str:
    key = str(args.get("key", "")).strip() or None
    cwd = args.get("cwd") or None

    path = _memory_file(cwd)
    data = _load(path)

    if not data:
        return f"(no memory entries for this project - file: {path.name})"

    if key is not None:
        if key not in data:
            all_keys = ", ".join(sorted(data.keys())) or "(none)"
            return f"NOT_FOUND: {key!r} - available keys: {all_keys}"
        return data[key]

    # List all entries
    lines = [f"Memory for {path.name} ({len(data)} entries):"]
    for k in sorted(data.keys()):
        v = data[k]
        preview = v[:120] + "..." if len(v) > 120 else v
        lines.append(f"  {k}: {preview!r}")
    return "\n".join(lines)

import logging
import os
from pathlib import Path

import aiofiles

from mcp_server.config import validate_path, PROJECT_ROOT

logger = logging.getLogger(__name__)


async def handle_read_file(args: dict) -> str:
    path = validate_path(args["path"])
    if not path.exists():
        return f"ERROR: File not found: {path}"
    if not path.is_file():
        return f"ERROR: Not a file: {path}"
    async with aiofiles.open(path, "r", encoding="utf-8", errors="replace") as f:
        content = await f.read()
    logger.info(f"read_file: {path} ({len(content)} chars)")
    return content


async def handle_write_file(args: dict) -> str:
    path = validate_path(args["path"])
    content = args["content"]
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write(content)
    logger.info(f"write_file: {path} ({len(content)} chars)")
    return f"Written {len(content)} chars to {path}"


async def handle_list_dir(args: dict) -> str:
    path = validate_path(args.get("path", str(PROJECT_ROOT)))
    recursive = bool(args.get("recursive", False))
    if not path.exists():
        return f"ERROR: Path not found: {path}"
    if not path.is_dir():
        return f"ERROR: Not a directory: {path}"
    entries = []
    if recursive:
        for root, dirs, files in os.walk(path):
            rel_root = Path(root).relative_to(path)
            for d in sorted(dirs):
                entries.append(f"[DIR]  {rel_root / d}")
            for fname in sorted(files):
                fpath = Path(root) / fname
                size = fpath.stat().st_size
                entries.append(f"[FILE] {rel_root / fname}  ({size} bytes)")
    else:
        for entry in sorted(path.iterdir(), key=lambda e: (e.is_file(), e.name)):
            if entry.is_dir():
                entries.append(f"[DIR]  {entry.name}/")
            else:
                entries.append(f"[FILE] {entry.name}  ({entry.stat().st_size} bytes)")
    logger.info(f"list_dir: {path} ({len(entries)} entries)")
    return "\n".join(entries) if entries else "(empty directory)"


async def handle_delete_file(args: dict) -> str:
    path = validate_path(args["path"])
    if not path.exists():
        return f"ERROR: File not found: {path}"
    if path.is_dir():
        return "ERROR: Use a shell command to delete directories; delete_file only removes files."
    path.unlink()
    logger.info(f"delete_file: {path}")
    return f"Deleted: {path}"

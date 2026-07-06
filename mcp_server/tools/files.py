import base64
import logging
import os
import re
from pathlib import Path

import aiofiles

from mcp_server.config import validate_path, PROJECT_ROOT
from mcp_server.checkpoints import backup_pre_image

# A data-URI is only treated as binary when it matches the WHOLE content
# (anchored prefix, base64 body, no trailing junk). A text file that merely
# starts with "data:...;base64," is written verbatim as text. (G3)
_DATA_URI_RE = re.compile(r"^data:[^,]*;base64,(.*)$", re.DOTALL)

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
    binary = bool(args.get("binary", False))
    backup_pre_image(path)  # K1: checkpoint pre-image before mutating
    path.parent.mkdir(parents=True, exist_ok=True)

    # Binary path: explicit `binary` flag, or a clean whole-content data-URI.
    match = _DATA_URI_RE.match(content)
    if binary or match is not None:
        if match is None:
            # Caller asked for binary but content is not a data:...;base64, URI.
            return (
                "ERROR: binary=True requires content to be a 'data:<type>;base64,<data>' "
                "URI; got non-data-URI content"
            )
        try:
            # validate=True rejects whitespace/prose after the base64 body, so a
            # text file that merely begins with the prefix falls through to text.
            binary_content = base64.b64decode(match.group(1), validate=True)
            async with aiofiles.open(path, "wb") as f:
                await f.write(binary_content)
            logger.info(f"write_file: {path} ({len(binary_content)} bytes)")
            return f"Written {len(binary_content)} bytes to {path}"
        except Exception as e:
            if binary:
                # Explicit request to decode failed — surface it, don't silently
                # write the data-URI string as text.
                logger.warning(f"Failed to decode base64 for {path}: {e}")
                return f"ERROR: failed to decode base64 content for {path}: {e}"
            logger.debug(f"Content starts with data-URI but is not clean base64; "
                         f"writing as text for {path}: {e}")
            # Fall through to text write.

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
            # Prune noisy directories in-place so os.walk doesn't descend into them
            dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "venv", ".venv", "__pycache__", ".next", ".nuxt"}]
            rel_root = Path(root).relative_to(path)
            for d in sorted(dirs):
                entries.append(f"[DIR]  {rel_root / d}")
            for fname in sorted(files):
                fpath = Path(root) / fname
                try:
                    size = fpath.stat().st_size
                except OSError:
                    size = 0
                entries.append(f"[FILE] {rel_root / fname}  ({size} bytes)")
            
            if len(entries) > 500:
                entries = entries[:500]
                entries.append("... (truncated at 500 items. Use find_files or search instead)")
                break
    else:
        for entry in sorted(path.iterdir(), key=lambda e: (e.is_file(), e.name)):
            if entry.is_dir():
                entries.append(f"[DIR]  {entry.name}/")
            else:
                # A broken symlink makes stat() raise; don't fail the whole
                # listing over one dangling entry (L1 — mirrors the guard the
                # recursive branch already has).
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                entries.append(f"[FILE] {entry.name}  ({size} bytes)")
    logger.info(f"list_dir: {path} ({len(entries)} entries)")
    return "\n".join(entries) if entries else "(empty directory)"


async def handle_delete_file(args: dict) -> str:
    path = validate_path(args["path"])
    if not path.exists():
        return f"ERROR: File not found: {path}"
    if path.is_dir():
        return "ERROR: Use a shell command to delete directories; delete_file only removes files."
    backup_pre_image(path)  # K1: checkpoint pre-image before delete
    path.unlink()
    logger.info(f"delete_file: {path}")
    return f"Deleted: {path}"

import base64
import logging
import os
import re
import stat
import tempfile
from pathlib import Path

import aiofiles

from mcp_server.config import validate_path, PROJECT_ROOT
from mcp_server.checkpoints import backup_pre_image
from mcp_server.tools._errors import tool_error

# A data-URI is only treated as binary when it matches the WHOLE content
# (anchored prefix, base64 body, no trailing junk). A text file that merely
# starts with "data:...;base64," is written verbatim as text. (G3)
_DATA_URI_RE = re.compile(r"^data:[^,]*;base64,(.*)$", re.DOTALL)

logger = logging.getLogger(__name__)


async def handle_read_file(args: dict) -> str:
    path = validate_path(args["path"])
    if not path.exists():
        return tool_error(f"File not found: {path}")
    if not path.is_file():
        return tool_error(f"Not a file: {path}")
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
            return tool_error(
                "binary=True requires content to be a 'data:<type>;base64,<data>' "
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
                return tool_error(f"failed to decode base64 content for {path}: {e}")
            logger.debug(f"Content starts with data-URI but is not clean base64; "
                         f"writing as text for {path}: {e}")
            # Fall through to text write.

    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write(content)
    logger.info(f"write_file: {path} ({len(content)} chars)")
    return f"Written {len(content)} chars to {path}"


def _line_context(content: str, index: int, match_len: int) -> str:
    """Return ~1-2 lines of context around content[index:index+match_len]."""
    lines = content.splitlines()
    if not lines:
        return ""
    start_line = content.count("\n", 0, index)
    end_line = content.count("\n", 0, index + match_len)
    lo = max(0, start_line - 1)
    hi = min(len(lines), end_line + 2)
    return "\n".join(lines[lo:hi])


class EditError(ValueError):
    """Raised by compute_edit_result when an edit_file call must be rejected."""


def compute_edit_result(
    content: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    expected_occurrences: object = None,
    *,
    label: str = "file",
) -> "tuple[str, int]":
    """Single source of truth for edit_file's validation + transform logic.

    Used by BOTH handle_edit_file (apply) and policy.build_approval_preview
    (approval-card diff) so the previewed change can never diverge from what
    apply actually does (D1). Pure string logic — no I/O.

    Returns (new_content, applied_count). Raises EditError with the
    user-facing message when the edit must be rejected (empty old_string,
    old==new, bad expected_occurrences, not found, ambiguous multi-match).
    ``label`` is only interpolated into error messages (typically the path).
    """
    if old_string == "":
        raise EditError(
            "old_string must not be empty — edit_file only modifies existing content "
            "that already exists in the file; use write_file to create a new file."
        )
    if old_string == new_string:
        raise EditError("old_string and new_string are identical — nothing to edit")

    if expected_occurrences is not None:
        try:
            expected_occurrences = int(expected_occurrences)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise EditError("expected_occurrences must be an integer")
        if expected_occurrences <= 0:
            raise EditError("expected_occurrences must be a positive integer")

    count = content.count(old_string)

    if count == 0:
        hint = ""
        normalized_old = " ".join(old_string.split())
        if normalized_old and " ".join(content.split()).find(normalized_old) != -1:
            hint = (
                " A whitespace-normalized match exists (differing indentation or line "
                "breaks) — re-copy old_string verbatim from the file's current content; "
                "this hint is informational only and is NOT applied automatically."
            )
        raise EditError(f"old_string not found in {label}.{hint}")

    if expected_occurrences is not None and count != expected_occurrences:
        raise EditError(
            f"old_string occurs {count} time(s) in {label}, but expected_occurrences="
            f"{expected_occurrences} — update expected_occurrences to match, or add more "
            "surrounding context to old_string to change the count"
        )

    if count > 1 and not replace_all and expected_occurrences is None:
        raise EditError(
            f"old_string occurs {count} times in {label} — it must be unique. Add more "
            "surrounding context to old_string to disambiguate, set replace_all=true to "
            "replace every occurrence, or pass expected_occurrences to confirm the count "
            "explicitly."
        )

    # NOTE: a verified expected_occurrences > 1 replaces ALL matches, exactly
    # like replace_all — the count check above already confirmed intent.
    if replace_all or count > 1:
        return content.replace(old_string, new_string), count
    return content.replace(old_string, new_string, 1), 1


async def handle_edit_file(args: dict) -> str:
    path = validate_path(args["path"])
    old_string = args.get("old_string")
    new_string = args.get("new_string")

    if old_string is None or new_string is None:
        return tool_error("edit_file requires both 'old_string' and 'new_string' arguments")
    old_string = str(old_string)
    new_string = str(new_string)

    if not path.exists():
        return tool_error(f"File not found: {path}")
    if not path.is_file():
        return tool_error(f"Not a file: {path}")

    # Read raw bytes and decode manually (no text-mode newline translation) so
    # CRLF/LF line endings outside the edited span are preserved byte-for-byte.
    async with aiofiles.open(path, "rb") as f:
        raw = await f.read()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        return tool_error(f"failed to decode {path} as UTF-8 text: {e}")

    try:
        new_content, applied = compute_edit_result(
            content,
            old_string,
            new_string,
            replace_all=bool(args.get("replace_all", False)),
            expected_occurrences=args.get("expected_occurrences"),
            label=str(path),
        )
    except EditError as e:
        return tool_error(str(e))

    first_index = content.find(old_string)

    backup_pre_image(path)  # K1: checkpoint pre-image before mutating

    # Preserve the original file's permission bits (D2): mkstemp creates the
    # tmp 0600 and os.replace keeps the tmp's metadata — without the chmod
    # below, a 0755 script would lose its exec bit after an edit.
    original_mode = stat.S_IMODE(os.stat(path).st_mode)

    # Atomic write: tmp file in the same directory, then os.replace.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(new_content.encode("utf-8"))
        os.chmod(tmp_name, original_mode)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    logger.info(f"edit_file: {path} ({applied} replacement(s))")
    plural = "s" if applied != 1 else ""
    context = _line_context(new_content, first_index, len(new_string))
    return (
        f"Edited {path}: replaced {applied} occurrence{plural} of old_string.\n"
        f"Context after edit:\n{context}"
    )


async def handle_list_dir(args: dict) -> str:
    path = validate_path(args.get("path", str(PROJECT_ROOT)))
    recursive = bool(args.get("recursive", False))
    if not path.exists():
        return tool_error(f"Path not found: {path}")
    if not path.is_dir():
        return tool_error(f"Not a directory: {path}")
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
        return tool_error(f"File not found: {path}")
    if path.is_dir():
        return tool_error("Use a shell command to delete directories; delete_file only removes files.")
    backup_pre_image(path)  # K1: checkpoint pre-image before delete
    path.unlink()
    logger.info(f"delete_file: {path}")
    return f"Deleted: {path}"

"""
KIM.md project context loader — mirrors Claw's CLAUDE.md discovery.

Walks from the current working directory upward to the filesystem root,
collecting instruction files at each level:

    KIM.md          — project-level instructions
    KIM.local.md    — local overrides (gitignored)
    .kim/KIM.md     — alternate location
    .kim/instructions.md — alternate name

Files are deduplicated by content hash and truncated to stay within a
token budget (4,000 chars per file, 12,000 total).

Usage:
    from orchestrator.context_loader import discover_instruction_files

    files = discover_instruction_files(Path.cwd())
    # [{"path": "/project/KIM.md", "content": "..."}]

    prompt_section = build_instruction_prompt(files)
    # "# Project instructions\n\n## KIM.md (scope: /project)\n..."
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MAX_CHARS_PER_FILE = 4_000
MAX_TOTAL_CHARS = 12_000

# File names to look for at each directory level
_INSTRUCTION_FILES = [
    "KIM.md",
    "KIM.local.md",
    Path(".kim") / "KIM.md",
    Path(".kim") / "instructions.md",
]


def discover_instruction_files(cwd: Optional[Path] = None) -> list[dict]:
    """
    Walk from ``cwd`` upward to root, collecting instruction files.

    Returns:
        List of ``{"path": str, "content": str}`` dicts, ordered from
        root-most to deepest (matching Claw's convention).
    """
    cwd = (cwd or Path.cwd()).resolve()

    # Build ancestor chain: [/, /Users, /Users/adam, ..., /Users/adam/project]
    directories: list[Path] = []
    cursor: Optional[Path] = cwd
    while cursor is not None:
        directories.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    directories.reverse()  # root first

    files: list[dict] = []
    seen_hashes: set[str] = set()

    for directory in directories:
        for candidate_name in _INSTRUCTION_FILES:
            candidate = directory / candidate_name
            try:
                if not candidate.is_file():
                    continue
                content = candidate.read_text(encoding="utf-8").strip()
                if not content:
                    continue

                # Dedup by content hash
                content_hash = hashlib.md5(
                    _normalize(content).encode()
                ).hexdigest()
                if content_hash in seen_hashes:
                    continue
                seen_hashes.add(content_hash)

                files.append({
                    "path": str(candidate),
                    "content": content,
                })
            except (OSError, UnicodeDecodeError) as e:
                logger.debug(f"Skipping {candidate}: {e}")

    if files:
        logger.info(
            f"Discovered {len(files)} instruction file(s): "
            + ", ".join(f["path"] for f in files)
        )
    return files


def build_instruction_prompt(files: list[dict]) -> str:
    """
    Render discovered instruction files into a system prompt section.

    Returns an empty string if no files were found.
    """
    if not files:
        return ""

    sections = ["# Project instructions"]
    remaining = MAX_TOTAL_CHARS

    for f in files:
        if remaining <= 0:
            sections.append(
                "_Additional instruction content omitted (budget exceeded)._"
            )
            break

        # Truncate individual file
        content = f["content"]
        limit = min(MAX_CHARS_PER_FILE, remaining)
        if len(content) > limit:
            content = content[:limit] + "\n\n[truncated]"

        remaining -= len(content)

        # Header: filename + scope (parent directory)
        path = Path(f["path"])
        filename = path.name
        scope = str(path.parent)
        sections.append(f"## {filename} (scope: {scope})")
        sections.append(content)

    return "\n\n".join(sections)


def _normalize(content: str) -> str:
    """Normalize whitespace for dedup hashing."""
    lines = []
    prev_blank = False
    for line in content.splitlines():
        stripped = line.rstrip()
        is_blank = not stripped
        if is_blank and prev_blank:
            continue
        lines.append(stripped)
        prev_blank = is_blank
    return "\n".join(lines).strip()

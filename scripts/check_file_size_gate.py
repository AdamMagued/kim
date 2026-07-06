#!/usr/bin/env python3
"""Q6 CI gate: block new/growing source files over 800 lines.

Usage: check_file_size_gate.py <base-ref>

Compares HEAD against <base-ref> and fails when:
  * a NEWLY ADDED source file exceeds MAX_LINES, or
  * a MODIFIED source file exceeds MAX_LINES *and grew* relative to base.

The "and grew" clause lets the pre-existing oversized files (agent.py,
subprocess.rs, useChatStream.ts, …) keep receiving fixes while they await
their scheduled decomposition (ROADMAP_TO_10 K2/Q2/Q3) — but they may not
get any bigger, and no new file may be born over the limit.

Generated files (events codegen) are exempt: their size is owned by the
schema, not by hand.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

MAX_LINES = 800
SOURCE_EXTS = {".py", ".rs", ".ts", ".tsx", ".js", ".jsx"}
EXEMPT_SUFFIXES = (".gen.ts", ".gen.rs", "_gen.py", ".d.ts", ".min.js")


def _line_count(text: str) -> int:
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def _base_line_count(base: str, path: str) -> int | None:
    proc = subprocess.run(
        ["git", "show", f"{base}:{path}"], capture_output=True, text=True
    )
    if proc.returncode != 0:
        return None
    return _line_count(proc.stdout)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_file_size_gate.py <base-ref>", file=sys.stderr)
        return 2
    base = sys.argv[1]

    diff = subprocess.run(
        ["git", "diff", "--name-status", "--diff-filter=AMR", f"{base}...HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    failures: list[str] = []
    for line in diff.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0], parts[-1]  # renames: last field is new path
        p = Path(path)
        if p.suffix not in SOURCE_EXTS or path.endswith(EXEMPT_SUFFIXES):
            continue
        if not p.exists():  # deleted/moved away since
            continue
        lines = _line_count(p.read_text(encoding="utf-8", errors="replace"))
        if lines <= MAX_LINES:
            continue
        if status.startswith("A"):
            failures.append(
                f"  {path}: NEW file with {lines} lines (max {MAX_LINES}) — split it"
            )
        else:  # M or R — allowed to exist over the limit, but not to grow
            base_lines = _base_line_count(base, path)
            if base_lines is None or lines > base_lines:
                grew = "" if base_lines is None else f" (was {base_lines})"
                failures.append(
                    f"  {path}: {lines} lines{grew} — over {MAX_LINES} and growing; "
                    "shrink it or extract the new code into a new module"
                )

    if failures:
        print(f"File-size gate FAILED (limit {MAX_LINES} lines for new/growing files):")
        print("\n".join(failures))
        print("\nSee docs/ROADMAP_TO_10.md Q6. Oversized legacy files may shrink or")
        print("stay the same size, but may not grow; new files must start under the limit.")
        return 1

    print(f"File-size gate OK (no new/growing source files over {MAX_LINES} lines).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

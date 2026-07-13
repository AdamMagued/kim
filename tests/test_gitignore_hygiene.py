"""Repo-hygiene guard (Operation Google-Level, Wave 0 / exit criterion G9).

Asserts that .gitignore actually covers the runtime/generated directories that
must never be committed. Uses `git check-ignore` (the authoritative matcher)
so nested-pattern and negation subtleties are evaluated exactly as git does.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Paths (relative to repo root) that must be ignored. Probe a file *inside*
# each dir too, so a `dir/`-only pattern that misses contents would fail.
MUST_IGNORE = [
    "venv/",
    "venv/bin/python",
    "logs/",
    "logs/kim_2026-01-01.jsonl",
    "graphify-out/",
    "graphify-out/graph.json",
    "kim_sessions/",
    "kim_sessions/2026-01-01/abc123.jsonl",
    "sessions/",
    "sessions/state.json",
    "Library/",  # macOS com.apple.python pyc-cache junk
]

# Tracked fixtures that a careless ignore pattern could swallow (negations
# in .gitignore must keep these visible to git).
MUST_NOT_IGNORE = [
    "tests/test_gitignore_hygiene.py",
    "requirements.txt",
]

_git = shutil.which("git")
pytestmark = pytest.mark.skipif(
    _git is None or not (REPO_ROOT / ".git").exists(),
    reason="requires a git checkout with git on PATH",
)


def _check_ignore(path: str) -> bool:
    """True if git would ignore `path` (exit 0), False if not (exit 1)."""
    proc = subprocess.run(
        [_git or "git", "check-ignore", "-q", "--", path],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    if proc.returncode not in (0, 1):  # 128 = fatal (e.g. not a repo)
        pytest.fail(
            f"git check-ignore failed for {path!r}: {proc.stderr.decode(errors='replace')}"
        )
    return proc.returncode == 0


@pytest.mark.parametrize("path", MUST_IGNORE)
def test_runtime_dirs_are_gitignored(path: str) -> None:
    assert _check_ignore(path), f".gitignore must cover {path!r} (G9)"


@pytest.mark.parametrize("path", MUST_NOT_IGNORE)
def test_tracked_files_are_not_ignored(path: str) -> None:
    assert not _check_ignore(path), f"{path!r} is tracked and must NOT be ignored"

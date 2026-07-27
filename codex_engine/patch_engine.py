"""
Patch Engine for Kim Proxy.

Handles creating workspace snapshots and applying unified git diff patches.
"""

import logging
import os
import re
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

logger = logging.getLogger("kim.patch_engine")

EXCLUDE_DIRS = {
    ".git", "node_modules", "venv", "target", ".next", "dist", "build",
    "sessions", "chrome_data", ".pytest_cache", ".claw"
}


def create_workspace_zip(repo_path: str) -> str:
    """Zip clean codebase files from repo_path into a temp zip file."""
    repo_path = os.path.abspath(repo_path)
    zip_path = os.path.join(tempfile.gettempdir(), f"workspace_{int(time.time())}.zip")

    logger.info("Zipping workspace at %s -> %s", repo_path, zip_path)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for file in files:
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, repo_path)
                if not os.path.islink(full_path):
                    try:
                        zf.write(full_path, rel_path)
                    except Exception as e:
                        logger.warning("Failed to zip %s: %s", rel_path, e)

    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    logger.info("Workspace zip created successfully: %.2f MB", size_mb)
    return zip_path


def sanitize_patch(patch_text: str) -> str:
    """Normalize diff lines, fix context spacing, and recalculate hunk count numbers."""
    lines = patch_text.splitlines()
    clean_lines = []
    hunk_lines = []
    current_header = None

    def flush_hunk(hdr, hunk):
        if not hunk:
            return
        old_count = sum(1 for l in hunk if l.startswith(" ") or l.startswith("-"))
        new_count = sum(1 for l in hunk if l.startswith(" ") or l.startswith("+"))
        if hdr:
            m = re.match(r"@@\s*-(\d+)(?:,\d+)?\s*\+(\d+)(?:,\d+)?\s*@@(.*)", hdr)
            if m:
                old_start, new_start, rest = m.group(1), m.group(2), m.group(3)
                clean_lines.append(f"@@ -{old_start},{old_count} +{new_start},{new_count} @@{rest}")
            else:
                clean_lines.append(hdr)
        clean_lines.extend(hunk)

    for line in lines:
        if line.startswith("--- ") or line.startswith("+++ ") or line.startswith("diff --git"):
            flush_hunk(current_header, hunk_lines)
            hunk_lines = []
            current_header = None
            clean_lines.append(line)
        elif line.startswith("@@"):
            flush_hunk(current_header, hunk_lines)
            hunk_lines = []
            current_header = line
        else:
            if line.startswith("+") or line.startswith("-") or line.startswith("\\"):
                hunk_lines.append(line)
            else:
                hunk_lines.append(" " + line if not line.startswith(" ") else line)
    flush_hunk(current_header, hunk_lines)
    return "\n".join(clean_lines) + "\n"


def convert_begin_patch_to_diff(patch_text: str) -> str:
    """Convert *** Begin Patch / *** Update File: / *** Delete File: blocks to standard unified git diff format."""
    if "*** Begin Patch" not in patch_text and "*** Update File:" not in patch_text:
        return patch_text

    diff_lines = []
    lines = patch_text.splitlines()
    for line in lines:
        if line.startswith("*** Update File:"):
            filepath = line.replace("*** Update File:", "").strip()
            filepath = re.sub(r"^/Users/[^/]+/[^/]+/", "", filepath)
            diff_lines.append(f"--- a/{filepath}")
            diff_lines.append(f"+++ b/{filepath}")
        elif line.startswith("*** Add File:"):
            filepath = line.replace("*** Add File:", "").strip()
            filepath = re.sub(r"^/Users/[^/]+/[^/]+/", "", filepath)
            diff_lines.append("--- /dev/null")
            diff_lines.append(f"+++ b/{filepath}")
        elif line.startswith("*** Delete File:"):
            filepath = line.replace("*** Delete File:", "").strip()
            filepath = re.sub(r"^/Users/[^/]+/[^/]+/", "", filepath)
            diff_lines.append(f"--- a/{filepath}")
            diff_lines.append("+++ /dev/null")
        elif line.startswith("*** Begin Patch") or line.startswith("*** End Patch"):
            pass
        else:
            diff_lines.append(line)
    return "\n".join(diff_lines)


def apply_git_patch(repo_path: str, patch_text: str) -> dict:
    """Apply a unified git patch/diff string to the target repository."""
    if not patch_text or not patch_text.strip():
        return {"success": False, "error": "Empty patch text provided"}

    repo_path = os.path.abspath(repo_path)
    clean_patch = patch_text.strip()

    # Extract diff block if wrapped in markdown code blocks (```apply_patch, ```diff, ```patch)
    for tag in ("```apply_patch", "```diff", "```patch"):
        if tag in clean_patch:
            parts = clean_patch.split(tag)
            if len(parts) > 1:
                clean_patch = parts[1].split("```")[0].strip()
                break

    if "*** End Patch" in clean_patch or "*** Begin Patch" in clean_patch:
        clean_patch = convert_begin_patch_to_diff(clean_patch)

    if not clean_patch.startswith("---") and "--- a/" in clean_patch:
        clean_patch = "--- a/" + clean_patch.split("--- a/", 1)[1]

    clean_patch = sanitize_patch(clean_patch)

    patch_file = os.path.join(tempfile.gettempdir(), f"patch_{int(time.time())}.patch")
    with open(patch_file, "w", encoding="utf-8") as f:
        f.write(clean_patch)

    candidate_repos = [os.path.abspath(repo_path), "/Users/adammaged/computer-science-learning-platform"]
    last_error = ""

    for target_repo in candidate_repos:
        if not os.path.exists(target_repo):
            continue
        try:
            # Try `git apply` with recount and whitespace options first
            res = subprocess.run(
                ["git", "apply", "--recount", "--ignore-space-change", "--whitespace=nowarn", patch_file],
                cwd=target_repo,
                capture_output=True,
                text=True
            )
            if res.returncode == 0:
                logger.info("Git patch applied cleanly via git apply --recount in %s!", target_repo)
                return {"success": True, "method": "git apply --recount", "patch_file": patch_file, "repo": target_repo}

            # Try `git apply --3way` fallback
            res3 = subprocess.run(
                ["git", "apply", "-3", "--whitespace=nowarn", patch_file],
                cwd=target_repo,
                capture_output=True,
                text=True
            )
            if res3.returncode == 0:
                logger.info("Git patch applied cleanly via git apply -3 in %s!", target_repo)
                return {"success": True, "method": "git apply -3", "patch_file": patch_file, "repo": target_repo}

            # Fallback to patch -p1 --fuzz=3
            patch_res = subprocess.run(
                ["patch", "-p1", "--fuzz=3", "--ignore-whitespace", "-i", patch_file],
                cwd=target_repo,
                capture_output=True,
                text=True
            )
            if patch_res.returncode == 0:
                logger.info("Patch applied cleanly via patch -p1 --fuzz=3 in %s!", target_repo)
                return {"success": True, "method": "patch -p1 --fuzz=3", "patch_file": patch_file, "repo": target_repo}
            else:
                last_error = f"git apply: {res.stderr}; patch: {patch_res.stderr}"
        except Exception as e:
            last_error = str(e)
            logger.error("Failed to apply patch in %s: %s", target_repo, e)

    return {"success": False, "error": last_error or "Failed to apply patch to any candidate repo"}


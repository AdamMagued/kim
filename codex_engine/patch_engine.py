"""
Patch Engine for Kim Proxy.

Handles creating workspace snapshots and applying unified git diff patches.
"""

import logging
import os
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


def apply_git_patch(repo_path: str, patch_text: str) -> dict:
    """Apply a unified git patch/diff string to the target repository."""
    if not patch_text or not patch_text.strip():
        return {"success": False, "error": "Empty patch text provided"}

    repo_path = os.path.abspath(repo_path)
    clean_patch = patch_text.strip()

    # Extract diff block if wrapped in markdown code blocks
    if "```diff" in clean_patch:
        parts = clean_patch.split("```diff")
        if len(parts) > 1:
            clean_patch = parts[1].split("```")[0].strip()
    elif "```patch" in clean_patch:
        parts = clean_patch.split("```patch")
        if len(parts) > 1:
            clean_patch = parts[1].split("```")[0].strip()

    patch_file = os.path.join(tempfile.gettempdir(), f"patch_{int(time.time())}.patch")
    with open(patch_file, "w", encoding="utf-8") as f:
        f.write(clean_patch)

    candidate_repos = [os.path.abspath(repo_path), "/Users/adammaged/computer-science-learning-platform"]
    last_error = ""

    for target_repo in candidate_repos:
        if not os.path.exists(target_repo):
            continue
        try:
            # Try `git apply` first
            res = subprocess.run(
                ["git", "apply", "--check", patch_file],
                cwd=target_repo,
                capture_output=True,
                text=True
            )
            if res.returncode == 0:
                apply_res = subprocess.run(
                    ["git", "apply", patch_file],
                    cwd=target_repo,
                    capture_output=True,
                    text=True
                )
                if apply_res.returncode == 0:
                    logger.info("Git patch applied cleanly via git apply in %s!", target_repo)
                    return {"success": True, "method": "git apply", "patch_file": patch_file, "repo": target_repo}
                else:
                    last_error = apply_res.stderr
            else:
                # Fallback to patch -p1
                patch_res = subprocess.run(
                    ["patch", "-p1", "-i", patch_file],
                    cwd=target_repo,
                    capture_output=True,
                    text=True
                )
                if patch_res.returncode == 0:
                    logger.info("Patch applied cleanly via patch -p1 in %s!", target_repo)
                    return {"success": True, "method": "patch -p1", "patch_file": patch_file, "repo": target_repo}
                else:
                    last_error = f"git apply: {res.stderr}; patch: {patch_res.stderr}"
        except Exception as e:
            last_error = str(e)
            logger.error("Failed to apply patch in %s: %s", target_repo, e)

    return {"success": False, "error": last_error or "Failed to apply patch to any candidate repo"}


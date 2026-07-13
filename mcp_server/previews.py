"""Human-readable approval previews for the HITL card.

Extracted from policy.py (file-size gate Q6). Security property: preview
rendering honors ONLY schema-declared, policy-validated path args and
re-applies validate_path, so a preview can never leak sandboxed content.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from mcp_server.config import validate_path

logger = logging.getLogger(__name__)


def build_approval_preview(name: str, args: dict) -> str:
    """Human-readable preview for the approval card."""
    args = args or {}
    try:
        if name in ("run_command", "background_start", "shell", "execute_command"):
            return str(args.get("command") or args.get("cmd") or "").strip()
        if name == "run_powershell":
            return str(args.get("script", "")).strip()[:400]
        if name == "background_cancel":
            job_id = str(args.get("job_id", "")).strip() or "(missing job id)"
            return f"Cancel background command {job_id}"
        if name == "edit_file":
            # D3: only the schema-declared (and policy-validated) "path" arg is
            # honored — never a "file_path" alias that _PATH_ARGS doesn't cover.
            # validate_path is re-applied here (mirroring the handler) so the
            # preview can never render content from a sandboxed/secret file.
            path = str(args.get("path") or "")
            try:
                p = validate_path(str(Path(path).expanduser())) if path.strip() else None
            except PermissionError:
                return ""  # denied path: render no preview at all
            old_string = str(args.get("old_string", ""))
            new_string = str(args.get("new_string", ""))
            old = ""
            try:
                if p is not None and p.is_file():
                    old = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                old = ""
            # D1: same function the handler applies — preview/apply divergence
            # is structurally impossible. A rejected edit previews as no change.
            from mcp_server.tools.files import EditError, compute_edit_result

            try:
                new, _count = compute_edit_result(
                    old,
                    old_string,
                    new_string,
                    replace_all=bool(args.get("replace_all", False)),
                    expected_occurrences=args.get("expected_occurrences"),
                    label=path or "file",
                )
            except EditError:
                new = old
            import difflib

            diff = list(difflib.unified_diff(
                old.splitlines(), new.splitlines(),
                fromfile=f"{path} (current)", tofile=f"{path} (new)", lineterm="",
            ))
            if len(diff) > 40:
                diff = diff[:40] + ["… (diff truncated)"]
            return "\n".join(diff) if diff else f"(no textual change to {path})"
        if name in ("write_file", "create_file"):
            # Same hardening as the edit_file branch above: only the
            # schema-declared (policy-validated) "path" arg is honored — never
            # a "file_path" alias _PATH_ARGS doesn't cover — and validate_path
            # is re-applied so the preview can never render a sandboxed file.
            path = str(args.get("path") or "")
            try:
                p = validate_path(str(Path(path).expanduser())) if path.strip() else None
            except PermissionError:
                return ""  # denied path: render no preview at all
            new = str(args.get("content", ""))
            old = ""
            try:
                if p is not None and p.is_file():
                    old = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                old = ""
            import difflib

            diff = list(difflib.unified_diff(
                old.splitlines(), new.splitlines(),
                fromfile=f"{path} (current)", tofile=f"{path} (new)", lineterm="",
            ))
            if len(diff) > 40:
                diff = diff[:40] + ["… (diff truncated)"]
            return "\n".join(diff) if diff else f"(no textual change to {path})"
        if name.startswith("web_") or name in ("navigate", "click_element", "open_url"):
            url = str(args.get("url") or args.get("href") or "")
            label = str(
                args.get("label") or args.get("selector") or args.get("element_id") or ""
            )
            parts = [url]
            if label:
                parts.append(f"→ {label}")
            return " ".join(x for x in parts if x).strip()
        if name in ("delete_file", "run_python", "run_node", "lint_file"):
            return str(args.get("path") or args.get("file") or args.get("code", ""))[:400]
        if name == "revert_changes":
            rid = str(args.get("run_id") or "").strip() or os.environ.get(
                "KIM_RUN_ID", ""
            ).strip() or "(current run)"
            return (
                f"Revert file changes from run {rid} — restores checkpointed "
                "pre-images and deletes files the run created (each first "
                "backed up to <path>.kim-revert.bak)."
            )
    except Exception as preview_err:
        logger.debug("build_approval_preview failed: %s", preview_err)
    return ""

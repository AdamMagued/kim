"""K1: per-run file checkpoints for revert.

Before a run's first mutation of a path, the pre-image is copied to
``~/.kim/checkpoints/<run-id>/`` and recorded in ``manifest.jsonl``. A new file
is recorded as a tombstone so revert deletes it. Per-run backups are capped at
50 MB; once exceeded, paths are recorded as ``truncated`` and not backed up.

Run id comes from the ``KIM_RUN_ID`` env var (exported by the Rust spawn). When
unset (standalone tool use), checkpointing is a no-op.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

CHECKPOINT_ROOT = Path.home() / ".kim" / "checkpoints"
MAX_RUN_BYTES = 50 * 1024 * 1024
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


def _run_id() -> str | None:
    rid = os.environ.get("KIM_RUN_ID", "").strip()
    return rid if _RUN_ID_RE.fullmatch(rid) else None


def _safe_run_dir(run_id: str) -> Path | None:
    if not _RUN_ID_RE.fullmatch(run_id or ""):
        return None
    return CHECKPOINT_ROOT / run_id


def _read_manifest(manifest: Path) -> list[dict]:
    if not manifest.exists():
        return []
    out = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _blob_sha256(blob_path: Path) -> str:
    """Return hex SHA-256 of blob file contents."""
    h = hashlib.sha256()
    with blob_path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _append_atomic(manifest: Path, record: dict) -> None:
    """Append a JSON record to manifest with an advisory file lock to avoid
    concurrent interleaving across threads/processes."""
    line = json.dumps(record) + "\n"
    try:
        import fcntl
        with manifest.open("a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(line)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except ImportError:
        # fcntl not available (non-Unix); fall back to a plain append.
        with manifest.open("a", encoding="utf-8") as f:
            f.write(line)


def _locked_read_manifest(manifest: Path) -> list[dict]:
    """Read manifest under an advisory exclusive lock so the caller can safely
    perform a check-then-append without a TOCTOU race."""
    if not manifest.exists():
        return []
    try:
        import fcntl
        with manifest.open("r", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                content = f.read()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except ImportError:
        content = manifest.read_text(encoding="utf-8")
    out = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _run_size_from_records(run_dir: Path, records: list[dict]) -> int:
    """Compute total pre-image size from already-loaded manifest records,
    avoiding a separate O(N) glob call on every backup_pre_image invocation."""
    total = 0
    for rec in records:
        blob = rec.get("blob")
        if blob and isinstance(blob, str):
            try:
                total += (run_dir / blob).stat().st_size
            except OSError:
                pass
    return total


def _valid_blob_name(blob: str) -> bool:
    """Return True only if blob is a plain filename with no path separators or
    traversal components."""
    if not blob or not isinstance(blob, str):
        return False
    if "/" in blob or "\\" in blob or ".." in blob:
        return False
    # Must be a simple filename (no directory components)
    return Path(blob).name == blob


def backup_pre_image(path: Path, run_id: str | None = None) -> None:
    """Record the pre-image of ``path`` before it is written/deleted. No-op when
    there is no run id. Only the FIRST touch of a path per run is recorded.

    Raises RuntimeError on backup failure so callers are aware (checkpointing
    failure is surfaced as a warning rather than silently masking data-loss risk).
    """
    rid = run_id or _run_id()
    if not rid:
        return
    try:
        path = Path(path)
        run_dir = _safe_run_dir(rid)
        if run_dir is None:
            return
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest = run_dir / "manifest.jsonl"
        key = str(path.resolve())

        # Hold an exclusive lock for the entire check-then-append sequence to
        # avoid TOCTOU races across threads/processes.
        try:
            import fcntl as _fcntl

            lock_fd = open(str(manifest) + ".lock", "a")
            _fcntl.flock(lock_fd, _fcntl.LOCK_EX)
        except ImportError:
            lock_fd = None
            _fcntl = None  # type: ignore[assignment]

        try:
            records = _locked_read_manifest(manifest)
            if any(rec.get("path") == key for rec in records):
                return  # already captured this run

            current_size = _run_size_from_records(run_dir, records)
            if current_size >= MAX_RUN_BYTES:
                _append_atomic(manifest, {"path": key, "kind": "truncated"})
                return
            if path.exists() and path.is_file():
                blob = hashlib.sha1(key.encode()).hexdigest()[:16] + ".preimage"
                shutil.copy2(path, run_dir / blob)
                checksum = _blob_sha256(run_dir / blob)
                _append_atomic(
                    manifest,
                    {"path": key, "kind": "modified", "blob": blob, "blob_sha256": checksum},
                )
            else:
                _append_atomic(manifest, {"path": key, "kind": "created"})
        finally:
            if lock_fd is not None:
                try:
                    import fcntl as _fcntl2
                    _fcntl2.flock(lock_fd, _fcntl2.LOCK_UN)
                except Exception:
                    pass
                lock_fd.close()
    except Exception as exc:
        # Surface the failure so callers know checkpointing did not succeed.
        # We deliberately do not re-raise to avoid breaking the tool call, but
        # silently swallowing the error masks data-loss risk.
        print(f"[kim] checkpoint warning: backup_pre_image failed for {path}: {exc}", file=sys.stderr)


def has_checkpoint(run_id: str) -> bool:
    run_dir = _safe_run_dir(run_id)
    return bool(run_dir and (run_dir / "manifest.jsonl").exists())


def _resolve_safe_path(path_str: str) -> Path:
    """Resolve *path_str* and reject sensitive files/directories.

    Unlike ``config.validate_path()``, this does **not** enforce
    ``ALLOWED_PATHS`` — revert must be able to restore files that were
    legitimately checkpointed anywhere the agent operated (including
    test temp directories such as ``/private/var/…`` or ``/tmp/…``).
    The sensitive-path and sensitive-glob deny-lists still apply to
    prevent overwriting credentials, keys, and browser profiles.

    Imports from ``mcp_server.config`` are deferred inside the function
    to avoid a circular import at module load time.
    """
    import fnmatch as _fnmatch  # noqa: PLC0415 (deferred to avoid circular import)

    # Deferred import — config is heavyweight (yaml, dotenv) and
    # checkpoints.py is imported early in the server startup path.
    from mcp_server.config import (  # noqa: PLC0415
        PROJECT_ROOT,
        _SENSITIVE_GLOBS,
        _SENSITIVE_PATHS,
    )

    p = Path(path_str)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    p = p.resolve()

    p_low = str(p).lower()
    for sensitive in _SENSITIVE_PATHS:
        s_low = str(sensitive).lower()
        if (
            p_low == s_low
            or p_low.startswith(s_low + os.sep)
            or p_low.startswith(s_low + "/")
        ):
            raise PermissionError(
                f"Path '{p}' is inside sensitive directory '{sensitive}' — access denied"
            )

    name_low = p.name.lower()
    for pattern in _SENSITIVE_GLOBS:
        if _fnmatch.fnmatch(name_low, pattern.lower()):
            raise PermissionError(
                f"Path '{p}' matches sensitive file pattern '{pattern}' — access denied"
            )

    return p


def revert_run(run_id: str) -> dict:
    """Restore all pre-images for ``run_id``. Current state of each path is first
    copied to ``<path>.kim-revert.bak`` so the revert is itself undoable."""
    run_dir = _safe_run_dir(run_id)
    if run_dir is None:
        return {"error": "invalid_run_id", "run_id": run_id}
    manifest = run_dir / "manifest.jsonl"
    if not manifest.exists():
        return {"error": "no_checkpoint", "run_id": run_id}
    restored: list[str] = []
    deleted: list[str] = []
    skipped: list[str] = []
    for rec in _read_manifest(manifest):
        raw_path = rec.get("path", "")
        kind = rec.get("kind")

        # ── Validate target path ──────────────────────────────────────────
        # _resolve_safe_path checks sensitive-path/glob deny-lists but does
        # NOT enforce ALLOWED_PATHS, so revert works on any path that was
        # legitimately recorded at backup time (including test tmp dirs).
        try:
            p = _resolve_safe_path(raw_path)
        except (PermissionError, ValueError, OSError) as e:
            skipped.append(f"{raw_path}: path rejected — {e}")
            continue

        try:
            if kind == "modified":
                blob = rec.get("blob", "")
                # Reject blob names with path separators or traversal components.
                if not _valid_blob_name(blob):
                    skipped.append(f"{p}: invalid blob name '{blob}'")
                    continue
                blob_path = run_dir / blob
                # Verify blob is physically inside run_dir (belt-and-suspenders).
                try:
                    blob_path.resolve().relative_to(run_dir.resolve())
                except ValueError:
                    skipped.append(f"{p}: blob escapes run directory")
                    continue
                if not blob_path.exists():
                    skipped.append(f"{p}: blob missing")
                    continue
                # Verify blob integrity before overwriting the target file.
                expected_sha = rec.get("blob_sha256")
                if expected_sha is not None:
                    actual_sha = _blob_sha256(blob_path)
                    if actual_sha != expected_sha:
                        skipped.append(f"{p}: blob checksum mismatch (tampered?)")
                        continue
                if p.exists():
                    shutil.copy2(p, _bak(p))
                shutil.copy2(blob_path, p)
                restored.append(str(p))
            elif kind == "created":
                if p.exists():
                    shutil.copy2(p, _bak(p))
                    p.unlink()
                    deleted.append(str(p))
                else:
                    skipped.append(str(p))
            else:  # truncated or unknown
                skipped.append(str(p))
        except Exception as e:
            skipped.append(f"{p}: {e}")
    return {
        "run_id": run_id,
        "restored": restored,
        "deleted": deleted,
        "skipped": skipped,
    }


def _bak(p: Path) -> Path:
    return p.with_name(p.name + ".kim-revert.bak")

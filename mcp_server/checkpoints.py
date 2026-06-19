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
import shutil
from pathlib import Path

CHECKPOINT_ROOT = Path.home() / ".kim" / "checkpoints"
MAX_RUN_BYTES = 50 * 1024 * 1024


def _run_id() -> str | None:
    rid = os.environ.get("KIM_RUN_ID", "").strip()
    return rid or None


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


def _append(manifest: Path, record: dict) -> None:
    with manifest.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _run_size(run_dir: Path) -> int:
    total = 0
    for p in run_dir.glob("*.preimage"):
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def backup_pre_image(path: Path, run_id: str | None = None) -> None:
    """Record the pre-image of ``path`` before it is written/deleted. No-op when
    there is no run id. Only the FIRST touch of a path per run is recorded."""
    rid = run_id or _run_id()
    if not rid:
        return
    try:
        path = Path(path)
        run_dir = CHECKPOINT_ROOT / rid
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest = run_dir / "manifest.jsonl"
        key = str(path.resolve())
        if any(rec.get("path") == key for rec in _read_manifest(manifest)):
            return  # already captured this run
        if _run_size(run_dir) >= MAX_RUN_BYTES:
            _append(manifest, {"path": key, "kind": "truncated"})
            return
        if path.exists() and path.is_file():
            blob = hashlib.sha1(key.encode()).hexdigest()[:16] + ".preimage"
            shutil.copy2(path, run_dir / blob)
            _append(manifest, {"path": key, "kind": "modified", "blob": blob})
        else:
            _append(manifest, {"path": key, "kind": "created"})  # tombstone
    except Exception:
        # Checkpointing must never break a tool call.
        pass


def has_checkpoint(run_id: str) -> bool:
    return (CHECKPOINT_ROOT / run_id / "manifest.jsonl").exists()


def revert_run(run_id: str) -> dict:
    """Restore all pre-images for ``run_id``. Current state of each path is first
    copied to ``<path>.kim-revert.bak`` so the revert is itself undoable."""
    run_dir = CHECKPOINT_ROOT / run_id
    manifest = run_dir / "manifest.jsonl"
    if not manifest.exists():
        return {"error": "no_checkpoint", "run_id": run_id}
    restored: list[str] = []
    deleted: list[str] = []
    skipped: list[str] = []
    for rec in _read_manifest(manifest):
        p = Path(rec.get("path", ""))
        kind = rec.get("kind")
        try:
            if kind == "modified":
                if p.exists():
                    shutil.copy2(p, _bak(p))
                shutil.copy2(run_dir / rec["blob"], p)
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

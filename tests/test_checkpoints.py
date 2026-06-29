"""K1: run checkpoint backup/restore round-trip."""

import hashlib
import importlib
import json
from pathlib import Path

import mcp_server.checkpoints as cp


def _isolate(monkeypatch, tmp_path, run_id="run-1"):
    monkeypatch.setattr(cp, "CHECKPOINT_ROOT", tmp_path / "checkpoints")
    monkeypatch.setenv("KIM_RUN_ID", run_id)


def test_modified_file_round_trip(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    f = tmp_path / "a.txt"
    f.write_text("original")
    cp.backup_pre_image(f)
    f.write_text("changed by agent")
    out = cp.revert_run("run-1")
    assert str(f) in out["restored"]
    assert f.read_text() == "original"
    # revert is itself undoable: current state saved to .kim-revert.bak
    assert (tmp_path / "a.txt.kim-revert.bak").read_text() == "changed by agent"


def test_new_file_is_deleted_on_revert(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    f = tmp_path / "new.txt"
    cp.backup_pre_image(f)  # tombstone — file does not exist yet
    f.write_text("agent created this")
    out = cp.revert_run("run-1")
    assert str(f) in out["deleted"]
    assert not f.exists()


def test_first_touch_only(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    f = tmp_path / "b.txt"
    f.write_text("v1")
    cp.backup_pre_image(f)
    f.write_text("v2")
    cp.backup_pre_image(f)  # second touch must NOT overwrite the pre-image
    f.write_text("v3")
    out = cp.revert_run("run-1")
    assert f.read_text() == "v1"
    assert str(f) in out["restored"]


def test_cap_records_truncated(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(cp, "MAX_RUN_BYTES", 10)
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 100)
    cp.backup_pre_image(big)  # under cap (run empty) → backed up
    other = tmp_path / "other.bin"
    other.write_bytes(b"y" * 100)
    cp.backup_pre_image(other)  # now over cap → truncated, not backed up
    manifest = (cp.CHECKPOINT_ROOT / "run-1" / "manifest.jsonl").read_text()
    assert "truncated" in manifest


def test_no_run_id_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(cp, "CHECKPOINT_ROOT", tmp_path / "checkpoints")
    monkeypatch.delenv("KIM_RUN_ID", raising=False)
    f = tmp_path / "c.txt"
    f.write_text("x")
    cp.backup_pre_image(f)
    assert not (tmp_path / "checkpoints").exists()


def test_invalid_run_id_cannot_escape_checkpoint_root(monkeypatch, tmp_path):
    monkeypatch.setattr(cp, "CHECKPOINT_ROOT", tmp_path / "checkpoints")
    monkeypatch.setenv("KIM_RUN_ID", "../escape")
    f = tmp_path / "d.txt"
    f.write_text("x")
    cp.backup_pre_image(f)
    assert not (tmp_path / "checkpoints").exists()
    assert not cp.has_checkpoint("../escape")
    assert cp.revert_run("../escape")["error"] == "invalid_run_id"


# ---------------------------------------------------------------------------
# Regression guards for internal helpers
# ---------------------------------------------------------------------------

def test_blob_sha256_matches_contents(tmp_path):
    """_blob_sha256 must equal hashlib.sha256 over the raw file bytes."""
    blob = tmp_path / "sample.bin"
    data = b"hello checkpoint world\x00\xff" * 4096  # > one chunk (65536 bytes)
    blob.write_bytes(data)

    expected = hashlib.sha256(data).hexdigest()
    assert cp._blob_sha256(blob) == expected


def test_blob_sha256_matches_contents_small(tmp_path):
    """_blob_sha256 works correctly for files smaller than one chunk."""
    blob = tmp_path / "tiny.txt"
    data = b"tiny"
    blob.write_bytes(data)

    expected = hashlib.sha256(data).hexdigest()
    assert cp._blob_sha256(blob) == expected


def test_run_size_from_records(tmp_path):
    """_run_size_from_records sums st_size of blobs referenced in records."""
    run_dir = tmp_path / "run-abc"
    run_dir.mkdir()

    blob1 = run_dir / "blob1.preimage"
    blob1.write_bytes(b"A" * 100)
    blob2 = run_dir / "blob2.preimage"
    blob2.write_bytes(b"B" * 250)

    records = [
        {"path": "/some/file.txt", "kind": "modified", "blob": "blob1.preimage"},
        {"path": "/other/file.txt", "kind": "modified", "blob": "blob2.preimage"},
    ]
    assert cp._run_size_from_records(run_dir, records) == 350


def test_run_size_from_records_skips_missing_blob(tmp_path):
    """_run_size_from_records skips records whose blob file does not exist."""
    run_dir = tmp_path / "run-xyz"
    run_dir.mkdir()

    blob = run_dir / "present.preimage"
    blob.write_bytes(b"X" * 60)

    records = [
        {"path": "/a.txt", "kind": "modified", "blob": "present.preimage"},
        {"path": "/b.txt", "kind": "modified", "blob": "missing.preimage"},
        {"path": "/c.txt", "kind": "created"},  # no blob key
    ]
    assert cp._run_size_from_records(run_dir, records) == 60


def test_run_size_from_records_empty(tmp_path):
    """_run_size_from_records returns 0 for an empty record list."""
    run_dir = tmp_path / "run-empty"
    run_dir.mkdir()
    assert cp._run_size_from_records(run_dir, []) == 0


def test_locked_read_manifest_missing_file(tmp_path):
    """_locked_read_manifest returns [] when the manifest file does not exist."""
    missing = tmp_path / "nonexistent" / "manifest.jsonl"
    assert cp._locked_read_manifest(missing) == []


def test_locked_read_manifest_parses_jsonl(tmp_path):
    """_locked_read_manifest parses valid JSONL lines and skips blank/corrupt ones."""
    manifest = tmp_path / "manifest.jsonl"
    rec1 = {"path": "/foo.txt", "kind": "modified", "blob": "abc.preimage"}
    rec2 = {"path": "/bar.txt", "kind": "created"}
    lines = (
        json.dumps(rec1) + "\n"
        + "\n"                          # blank line — must be skipped
        + "   \n"                       # whitespace-only — must be skipped
        + "not valid json{{{\n"         # corrupt line — must be skipped
        + json.dumps(rec2) + "\n"
    )
    manifest.write_text(lines, encoding="utf-8")

    result = cp._locked_read_manifest(manifest)
    assert len(result) == 2
    assert result[0] == rec1
    assert result[1] == rec2


def test_append_atomic_roundtrip(tmp_path):
    """_append_atomic followed by _locked_read_manifest yields the appended record."""
    manifest = tmp_path / "manifest.jsonl"
    record = {"path": "/tmp/test.txt", "kind": "modified", "blob": "dead.preimage", "blob_sha256": "cafebabe"}

    cp._append_atomic(manifest, record)

    result = cp._locked_read_manifest(manifest)
    assert len(result) == 1
    assert result[0] == record


def test_append_atomic_multiple_records(tmp_path):
    """Multiple _append_atomic calls accumulate all records in order."""
    manifest = tmp_path / "manifest.jsonl"
    records = [
        {"path": "/a.txt", "kind": "created"},
        {"path": "/b.txt", "kind": "modified", "blob": "x.preimage"},
        {"path": "/c.txt", "kind": "truncated"},
    ]
    for rec in records:
        cp._append_atomic(manifest, rec)

    result = cp._locked_read_manifest(manifest)
    assert result == records

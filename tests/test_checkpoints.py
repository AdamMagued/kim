"""K1: run checkpoint backup/restore round-trip."""

import importlib
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

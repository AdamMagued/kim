"""
Tests for the `revert_changes` MCP tool (codex-parity item 4).

Thin wrapper over mcp_server.checkpoints.revert_run — restores pre-image
checkpoints from the current (or given) run. Mirrors the isolation pattern
of tests/test_checkpoints.py (monkeypatched CHECKPOINT_ROOT + KIM_RUN_ID).
"""
from __future__ import annotations

import asyncio

import mcp_server.checkpoints as cp
from mcp_server.policy import build_approval_preview
from mcp_server.tool_registry import TIER_DISPATCH
from mcp_server.tools.files import handle_revert_changes
from orchestrator.tool_risk import classify_tool_risk


def _isolate(monkeypatch, tmp_path, run_id="run-t1"):
    monkeypatch.setattr(cp, "CHECKPOINT_ROOT", tmp_path / "checkpoints")
    monkeypatch.setenv("KIM_RUN_ID", run_id)


def _revert(**args) -> str:
    return asyncio.run(handle_revert_changes(args))


# ── Behavior ─────────────────────────────────────────────────────────────


def test_revert_restores_pre_image_content(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    f = tmp_path / "a.txt"
    f.write_text("original")
    cp.backup_pre_image(f)
    f.write_text("changed by agent")
    out = _revert()  # no run_id → current run from KIM_RUN_ID
    assert not out.startswith("ERROR")
    assert "1 file(s) restored" in out
    assert str(f) in out
    assert f.read_text() == "original"


def test_revert_deletes_created_file(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    f = tmp_path / "new.txt"
    cp.backup_pre_image(f)  # tombstone — file did not exist yet
    f.write_text("agent created this")
    out = _revert()
    assert "1 created file(s) deleted" in out
    assert not f.exists()


def test_explicit_run_id_arg(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, run_id="run-x")
    f = tmp_path / "b.txt"
    f.write_text("v1")
    cp.backup_pre_image(f)
    f.write_text("v2")
    monkeypatch.delenv("KIM_RUN_ID")
    out = _revert(run_id="run-x")
    assert f.read_text() == "v1"
    assert "run 'run-x'" in out


def test_no_checkpoint_is_honest_error(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, run_id="run-empty")
    out = _revert()
    assert out.startswith("ERROR:")
    assert "no checkpoint exists" in out
    assert "nothing recorded to revert" in out


def test_no_run_id_and_no_env_errors(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.delenv("KIM_RUN_ID")
    out = _revert()
    assert out.startswith("ERROR:")
    assert "nothing to revert" in out


def test_invalid_run_id_errors(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    out = _revert(run_id="../../etc")
    assert out.startswith("ERROR:")
    assert "invalid run_id" in out


def test_sensitive_manifest_path_skipped_not_restored(monkeypatch, tmp_path):
    """Sandbox: a manifest record pointing at a secret file is skipped by
    revert_run's _resolve_safe_path deny-lists, and reported as skipped."""
    _isolate(monkeypatch, tmp_path, run_id="run-s")
    run_dir = tmp_path / "checkpoints" / "run-s"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.jsonl").write_text(
        '{"path": "' + str(tmp_path / ".env") + '", "kind": "created"}\n',
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    out = _revert()
    assert "1 skipped" in out
    # The secret file must NOT have been touched.
    assert (tmp_path / ".env").read_text() == "SECRET=1"


# ── Wiring: tier, risk, preview ──────────────────────────────────────────


def test_tier_is_file_write():
    assert "revert_changes" in TIER_DISPATCH["file_write"]
    assert "revert_changes" not in TIER_DISPATCH["file_read"]


def test_risk_same_treatment_as_write_file():
    assert classify_tool_risk("revert_changes") == classify_tool_risk("write_file")


def test_approval_preview_mentions_run(monkeypatch):
    monkeypatch.delenv("KIM_RUN_ID", raising=False)
    out = build_approval_preview("revert_changes", {"run_id": "run-42"})
    assert "run-42" in out
    assert "Revert" in out


def test_approval_preview_defaults_to_current_run(monkeypatch):
    monkeypatch.delenv("KIM_RUN_ID", raising=False)
    out = build_approval_preview("revert_changes", {})
    assert "(current run)" in out

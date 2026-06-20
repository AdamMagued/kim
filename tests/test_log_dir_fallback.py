"""D3: resolve_log_dir always returns a writable directory."""

from pathlib import Path
from orchestrator.cli import resolve_log_dir


def test_resolve_log_dir_returns_writable_existing_dir():
    d = resolve_log_dir()
    assert isinstance(d, Path)
    assert d.is_dir()
    probe = d / ".d3_write_test"
    probe.write_text("x")
    probe.unlink()


def test_resolve_log_dir_falls_back_when_earlier_candidates_fail(monkeypatch):
    real_mkdir = Path.mkdir
    repo_logs = Path(__file__).resolve().parent.parent / "logs"

    def flaky_mkdir(self, *a, **k):
        # Simulate a read-only repo logs/ dir.
        if self == repo_logs:
            raise PermissionError("read-only")
        return real_mkdir(self, *a, **k)

    monkeypatch.setattr(Path, "mkdir", flaky_mkdir)
    d = resolve_log_dir()
    assert d != repo_logs
    assert d.is_dir()

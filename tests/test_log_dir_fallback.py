"""D3: resolve_log_dir always returns a writable directory.

All filesystem side-effects are confined to tmp_path: Path.home is
monkeypatched so the ~/.kim/logs fallback never touches the real home
directory (an earlier version of this file created ~/.kim/logs for real).
"""

from pathlib import Path

import pytest

from orchestrator.cli import resolve_log_dir


@pytest.fixture(autouse=True)
def _fake_home(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    return fake_home


def test_resolve_log_dir_returns_writable_existing_dir():
    d = resolve_log_dir()
    assert isinstance(d, Path)
    assert d.is_dir()
    probe = d / ".d3_write_test"
    probe.write_text("x")
    probe.unlink()


def test_resolve_log_dir_falls_back_when_earlier_candidates_fail(monkeypatch, _fake_home):
    real_mkdir = Path.mkdir
    repo_logs = Path(resolve_log_dir.__code__.co_filename).resolve().parent.parent / "logs"

    def flaky_mkdir(self, *a, **k):
        # Simulate a read-only repo logs/ dir.
        if self == repo_logs:
            raise PermissionError("read-only")
        return real_mkdir(self, *a, **k)

    monkeypatch.setattr(Path, "mkdir", flaky_mkdir)
    d = resolve_log_dir()
    assert d != repo_logs
    assert d.is_dir()
    # The fallback lands in the faked home (or tempdir) — never the real home.
    assert str(d).startswith(str(_fake_home)) or "kim-logs" in str(d)

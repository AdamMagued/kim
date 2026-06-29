"""
Tests for orchestrator.scheduled_runner.

Covers:
  - is_allowed_provider allowlist
  - find_interpreter: prefers venv > .venv > PATH
  - find_interpreter: falls back to sys.executable (absolute, PATH-hijack guard)
  - run_next_due_task: no due tasks
  - run_next_due_task: provider refused (allowlist violation)
  - run_next_due_task: dry_run
  - run_next_due_task: preflight failure -> error + no record_run
  - run_next_due_task: spawn failure (OSError) -> no record_run
  - run_next_due_task: successful launch + record_run advanced
  - run_next_due_task: uses resolved interpreter (not sys.executable by default)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from orchestrator.scheduled_runner import (
    RunDueResult,
    _build_subprocess_env,
    find_interpreter,
    is_allowed_provider,
    run_next_due_task,
)
from orchestrator.cron_store import CronStore


# ---------------------------------------------------------------------------
# is_allowed_provider
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("provider,expected", [
    (None, True),
    ("", True),
    ("  ", True),
    ("ollama", True),
    ("OLLAMA", True),
    ("ollama-cloud", True),
    ("Ollama-Cloud", True),
    ("browser", True),
    ("browser:gemini", True),
    ("browser:chatgpt", True),
    ("browser:claude", True),
    ("BROWSER:GEMINI", True),
    # Refused
    ("openai", False),
    ("openai-gpt4", False),
    ("gpt-5", False),
    ("gpt5.5", False),
    ("GPT-4o", False),
    ("claude", False),
    ("gemini", False),
    ("deepseek", False),
    ("anthropic", False),
    ("custom-llm", False),
])
def test_is_allowed_provider(provider, expected):
    assert is_allowed_provider(provider) is expected


# ---------------------------------------------------------------------------
# find_interpreter
# ---------------------------------------------------------------------------

def test_find_interpreter_prefers_venv(tmp_path):
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("#!/bin/sh\n")

    result = find_interpreter(tmp_path)
    assert result == str(tmp_path / "venv" / "bin" / "python")


def test_find_interpreter_prefers_venv_over_dot_venv(tmp_path):
    for name in ("venv", ".venv"):
        d = tmp_path / name / "bin"
        d.mkdir(parents=True)
        (d / "python").write_text("#!/bin/sh\n")

    result = find_interpreter(tmp_path)
    assert "/.venv/" not in result
    assert "/venv/" in result


def test_find_interpreter_dot_venv_fallback(tmp_path):
    d = tmp_path / ".venv" / "bin"
    d.mkdir(parents=True)
    (d / "python").write_text("#!/bin/sh\n")

    result = find_interpreter(tmp_path)
    assert result == str(tmp_path / ".venv" / "bin" / "python")


def test_find_interpreter_falls_back_to_path(tmp_path):
    # No venv dirs - should return python3/python from PATH or sys.executable.
    result = find_interpreter(tmp_path)
    assert result  # non-empty string


def test_find_interpreter_falls_back_to_sys_executable(tmp_path):
    """No venv present → must return sys.executable exactly (absolute path, PATH-hijack guard).

    find_interpreter must never return a bare name like 'python' or 'python3'
    that could be intercepted by a PATH-injected binary.  The only safe fallback
    is sys.executable, which CPython always sets to an absolute resolved path.
    """
    result = find_interpreter(tmp_path)
    assert result == sys.executable, (
        f"expected sys.executable ({sys.executable!r}), got {result!r}"
    )
    assert os.path.isabs(result), (
        f"fallback interpreter must be an absolute path (PATH-hijack guard), got {result!r}"
    )


def test_is_allowed_provider_allowlist():
    """Focused spot-check: canonical allowed values are accepted; unknown/None handled correctly.

    This consolidates the regression guard in one named function so that
    any change to _ALLOWED_RE or is_allowed_provider immediately surfaces here.
    """
    # Allowed: empty / None -> defaults to ollama
    assert is_allowed_provider(None) is True
    assert is_allowed_provider("") is True
    assert is_allowed_provider("   ") is True
    # Allowed: explicit supported providers
    assert is_allowed_provider("ollama") is True
    assert is_allowed_provider("OLLAMA") is True
    assert is_allowed_provider("ollama-cloud") is True
    assert is_allowed_provider("browser") is True
    assert is_allowed_provider("browser:chatgpt") is True
    assert is_allowed_provider("browser:gemini") is True
    # Rejected: cloud/commercial providers must never run scheduled tasks
    assert is_allowed_provider("openai") is False
    assert is_allowed_provider("claude") is False
    assert is_allowed_provider("gemini") is False
    assert is_allowed_provider("deepseek") is False
    assert is_allowed_provider("gpt-4") is False
    assert is_allowed_provider("gpt5.5") is False
    assert is_allowed_provider("anthropic") is False
    # Rejected: unknown/custom values
    assert is_allowed_provider("custom-llm") is False
    assert is_allowed_provider("my-provider") is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(tmp_path: Path) -> CronStore:
    return CronStore(store_file=tmp_path / "schedules.json")


def _add_due_task(store: CronStore, task_text="check disk", provider=None):
    """Add a task and record a run far enough in the past that it's always due."""
    t = store.add(task_text, "@hourly", provider=provider)
    past = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    store.record_run(t.id, ran_at=past)  # next_run_at = 2026-01-01T01:00
    return t


def _ok_preflight(*args, **kwargs):
    """Mock subprocess.run that simulates a passing preflight."""
    m = MagicMock()
    m.returncode = 0
    m.stderr = ""
    m.stdout = ""
    return m


def _fail_preflight(*args, **kwargs):
    """Mock subprocess.run that simulates a failing preflight."""
    m = MagicMock()
    m.returncode = 1
    m.stderr = "ModuleNotFoundError: No module named 'mcp'"
    m.stdout = ""
    return m


# ---------------------------------------------------------------------------
# run_next_due_task: no due tasks
# ---------------------------------------------------------------------------

def test_empty_store_returns_none(tmp_path):
    result = run_next_due_task(store_file=tmp_path / "schedules.json")
    assert result is None


def test_no_due_tasks_returns_none(tmp_path):
    result = run_next_due_task(
        store_file=tmp_path / "schedules.json",
        as_of=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    assert result is None


# ---------------------------------------------------------------------------
# run_next_due_task: provider refused
# ---------------------------------------------------------------------------

def test_forbidden_provider_skipped(tmp_path):
    store = _make_store(tmp_path)
    _add_due_task(store, provider="openai")
    result = run_next_due_task(store_file=tmp_path / "schedules.json")
    assert result is not None
    assert result.skipped is True
    assert result.launched is False
    assert "allowlist" in result.skip_reason


def test_forbidden_gpt_provider_skipped(tmp_path):
    store = _make_store(tmp_path)
    _add_due_task(store, provider="gpt-5.5")
    result = run_next_due_task(store_file=tmp_path / "schedules.json")
    assert result is not None
    assert result.skipped is True


def test_forbidden_claude_provider_skipped(tmp_path):
    store = _make_store(tmp_path)
    _add_due_task(store, provider="claude")
    result = run_next_due_task(store_file=tmp_path / "schedules.json")
    assert result is not None
    assert result.skipped is True


def test_forbidden_provider_does_not_advance_run_count(tmp_path):
    store = _make_store(tmp_path)
    t = _add_due_task(store, provider="openai")
    run_next_due_task(store_file=tmp_path / "schedules.json")
    reloaded = _make_store(tmp_path).get(t.id)
    assert reloaded is not None
    assert reloaded.run_count == 1  # unchanged from _add_due_task


# ---------------------------------------------------------------------------
# run_next_due_task: dry_run
# ---------------------------------------------------------------------------

def test_dry_run_skips_without_launch(tmp_path):
    store = _make_store(tmp_path)
    _add_due_task(store, provider="ollama")
    result = run_next_due_task(store_file=tmp_path / "schedules.json", dry_run=True)
    assert result is not None
    assert result.skipped is True
    assert result.skip_reason == "dry_run"
    assert result.launched is False
    assert result.recorded is False


def test_dry_run_does_not_advance_run_count(tmp_path):
    store = _make_store(tmp_path)
    t = _add_due_task(store, provider="ollama")
    before = _make_store(tmp_path).get(t.id).run_count
    run_next_due_task(store_file=tmp_path / "schedules.json", dry_run=True)
    assert _make_store(tmp_path).get(t.id).run_count == before


# ---------------------------------------------------------------------------
# run_next_due_task: preflight failure
# ---------------------------------------------------------------------------

def test_preflight_failure_returns_error(tmp_path):
    store = _make_store(tmp_path)
    _add_due_task(store, provider="ollama")
    with patch("orchestrator.scheduled_runner.subprocess.run", side_effect=_fail_preflight):
        result = run_next_due_task(
            store_file=tmp_path / "schedules.json",
            _interpreter_override="/fake/python",
        )
    assert result is not None
    assert result.launched is False
    assert result.recorded is False
    assert "preflight failed" in result.error
    assert "mcp" in result.error


def test_preflight_failure_does_not_record_run(tmp_path):
    store = _make_store(tmp_path)
    t = _add_due_task(store, provider="ollama")
    before = _make_store(tmp_path).get(t.id).run_count
    with patch("orchestrator.scheduled_runner.subprocess.run", side_effect=_fail_preflight):
        run_next_due_task(
            store_file=tmp_path / "schedules.json",
            _interpreter_override="/fake/python",
        )
    assert _make_store(tmp_path).get(t.id).run_count == before


def test_preflight_timeout_returns_error(tmp_path):
    store = _make_store(tmp_path)
    _add_due_task(store, provider="ollama")
    with patch(
        "orchestrator.scheduled_runner.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="python", timeout=10),
    ):
        result = run_next_due_task(
            store_file=tmp_path / "schedules.json",
            _interpreter_override="/fake/python",
        )
    assert result is not None
    assert "timed out" in result.error
    assert result.launched is False


def test_preflight_oserror_returns_error(tmp_path):
    store = _make_store(tmp_path)
    _add_due_task(store, provider="ollama")
    with patch(
        "orchestrator.scheduled_runner.subprocess.run",
        side_effect=OSError("no such interpreter"),
    ):
        result = run_next_due_task(
            store_file=tmp_path / "schedules.json",
            _interpreter_override="/nonexistent/python",
        )
    assert result is not None
    assert "preflight could not run" in result.error
    assert result.launched is False


# ---------------------------------------------------------------------------
# run_next_due_task: spawn failure
# ---------------------------------------------------------------------------

def test_spawn_failure_sets_error(tmp_path):
    store = _make_store(tmp_path)
    _add_due_task(store, provider="ollama")
    with patch("orchestrator.scheduled_runner.subprocess.run", side_effect=_ok_preflight), \
         patch("orchestrator.scheduled_runner.subprocess.Popen", side_effect=OSError("no such file")):
        result = run_next_due_task(
            store_file=tmp_path / "schedules.json",
            _interpreter_override="/fake/python",
        )
    assert result is not None
    assert result.launched is False
    assert "spawn failed" in result.error
    assert result.recorded is False


def test_spawn_failure_does_not_record_run(tmp_path):
    store = _make_store(tmp_path)
    t = _add_due_task(store, provider="ollama")
    before = _make_store(tmp_path).get(t.id).run_count
    with patch("orchestrator.scheduled_runner.subprocess.run", side_effect=_ok_preflight), \
         patch("orchestrator.scheduled_runner.subprocess.Popen", side_effect=OSError("err")):
        run_next_due_task(
            store_file=tmp_path / "schedules.json",
            _interpreter_override="/fake/python",
        )
    assert _make_store(tmp_path).get(t.id).run_count == before


# ---------------------------------------------------------------------------
# run_next_due_task: successful launch
# ---------------------------------------------------------------------------

def test_successful_launch_sets_launched_and_recorded(tmp_path):
    store = _make_store(tmp_path)
    _add_due_task(store, provider="ollama")
    with patch("orchestrator.scheduled_runner.subprocess.run", side_effect=_ok_preflight), \
         patch("orchestrator.scheduled_runner.subprocess.Popen", return_value=MagicMock()):
        result = run_next_due_task(
            store_file=tmp_path / "schedules.json",
            _interpreter_override="/fake/python",
        )
    assert result is not None
    assert result.launched is True
    assert result.recorded is True
    assert result.skipped is False
    assert result.error == ""


def test_successful_launch_advances_run_count(tmp_path):
    store = _make_store(tmp_path)
    t = _add_due_task(store, provider="ollama")
    before = _make_store(tmp_path).get(t.id).run_count
    with patch("orchestrator.scheduled_runner.subprocess.run", side_effect=_ok_preflight), \
         patch("orchestrator.scheduled_runner.subprocess.Popen", return_value=MagicMock()):
        run_next_due_task(
            store_file=tmp_path / "schedules.json",
            _interpreter_override="/fake/python",
        )
    assert _make_store(tmp_path).get(t.id).run_count == before + 1


def test_successful_launch_uses_resolved_interpreter(tmp_path):
    """Popen receives the injected interpreter, not sys.executable."""
    store = _make_store(tmp_path)
    _add_due_task(store, provider="ollama")
    captured = {}
    def fake_popen(args, **kwargs):
        captured["args"] = args
        return MagicMock()
    with patch("orchestrator.scheduled_runner.subprocess.run", side_effect=_ok_preflight), \
         patch("orchestrator.scheduled_runner.subprocess.Popen", side_effect=fake_popen):
        run_next_due_task(
            store_file=tmp_path / "schedules.json",
            _interpreter_override="/injected/python",
        )
    assert captured["args"][0] == "/injected/python"


def test_empty_provider_defaults_to_ollama(tmp_path):
    store = _make_store(tmp_path)
    _add_due_task(store, provider=None)
    captured = {}
    def fake_popen(args, **kwargs):
        captured["args"] = args
        return MagicMock()
    with patch("orchestrator.scheduled_runner.subprocess.run", side_effect=_ok_preflight), \
         patch("orchestrator.scheduled_runner.subprocess.Popen", side_effect=fake_popen):
        run_next_due_task(
            store_file=tmp_path / "schedules.json",
            _interpreter_override="/fake/python",
        )
    idx = captured["args"].index("--provider")
    assert captured["args"][idx + 1] == "ollama"


def test_browser_provider_allowed(tmp_path):
    store = _make_store(tmp_path)
    _add_due_task(store, provider="browser:gemini")
    with patch("orchestrator.scheduled_runner.subprocess.run", side_effect=_ok_preflight), \
         patch("orchestrator.scheduled_runner.subprocess.Popen", return_value=MagicMock()):
        result = run_next_due_task(
            store_file=tmp_path / "schedules.json",
            _interpreter_override="/fake/python",
        )
    assert result is not None
    assert result.launched is True


def test_preflight_receives_resolved_interpreter(tmp_path):
    """subprocess.run (preflight) uses the injected interpreter path."""
    store = _make_store(tmp_path)
    _add_due_task(store, provider="ollama")
    calls = []
    def fake_run(args, **kwargs):
        calls.append(args)
        m = MagicMock()
        m.returncode = 0
        m.stderr = m.stdout = ""
        return m
    with patch("orchestrator.scheduled_runner.subprocess.run", side_effect=fake_run), \
         patch("orchestrator.scheduled_runner.subprocess.Popen", return_value=MagicMock()):
        run_next_due_task(
            store_file=tmp_path / "schedules.json",
            _interpreter_override="/custom/python3",
        )
    assert calls[0][0] == "/custom/python3"


# ---------------------------------------------------------------------------
# RunDueResult.to_dict
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Queue-blocker regression: forbidden-provider task must not starve others
# ---------------------------------------------------------------------------

def test_forbidden_provider_does_not_block_allowed_task(tmp_path):
    """A forbidden-provider task must not permanently starve tasks behind it.

    Before the fix, run_next_due_task always processed due[0]. Because
    record_run is never called for a forbidden task, next_run_at never
    advances, so the forbidden task stays at the front of the queue forever
    and prevents any other task from running.

    After the fix, the runner skips forbidden tasks and executes the first
    allowed one.
    """
    store = _make_store(tmp_path)
    # Add forbidden task (openai) — becomes due[0] because it was created first.
    _add_due_task(store, task_text="forbidden task", provider="openai")
    # Add allowed task — also due, created slightly later.
    t_allowed = _add_due_task(store, task_text="allowed task", provider="ollama")

    captured = {}
    def fake_popen(args, **kwargs):
        captured["args"] = args
        return MagicMock()

    with patch("orchestrator.scheduled_runner.subprocess.run", side_effect=_ok_preflight), \
         patch("orchestrator.scheduled_runner.subprocess.Popen", side_effect=fake_popen):
        result = run_next_due_task(
            store_file=tmp_path / "schedules.json",
            _interpreter_override="/fake/python",
        )

    assert result is not None
    assert result.launched is True, f"expected launch, got: {result}"
    assert result.task_id == t_allowed.id, "should have launched the allowed task, not the forbidden one"
    # Allowed task's run_count must be advanced.
    assert _make_store(tmp_path).get(t_allowed.id).run_count == 2  # 1 from _add_due_task + 1 now
    # Forbidden task's run_count must NOT have changed.
    forbidden = store.list_tasks()[0] if store.list_tasks()[0].provider == "openai" else store.list_tasks()[1]
    # Re-load to see persisted state.
    all_tasks = {t.id: t for t in _make_store(tmp_path).list_tasks()}
    for t in all_tasks.values():
        if t.provider == "openai":
            assert t.run_count == 1  # unchanged from _add_due_task


def test_all_forbidden_returns_skip_result_for_first(tmp_path):
    """When all due tasks have forbidden providers, return the first as diagnostic."""
    store = _make_store(tmp_path)
    t1 = _add_due_task(store, task_text="first forbidden", provider="claude")
    _add_due_task(store, task_text="second forbidden", provider="gemini")

    result = run_next_due_task(store_file=tmp_path / "schedules.json")

    assert result is not None
    assert result.skipped is True
    assert result.launched is False
    assert "allowlist" in result.skip_reason
    # Neither task's run_count should advance.
    for t in _make_store(tmp_path).list_tasks():
        assert t.run_count == 1  # unchanged from _add_due_task


def test_run_due_result_to_dict():
    r = RunDueResult(task_id="abc", task_text="ping", launched=True, recorded=True)
    d = r.to_dict()
    assert d["task_id"] == "abc"
    assert d["task"] == "ping"
    assert d["launched"] is True
    assert d["recorded"] is True
    assert d["skipped"] is False
    assert d["skip_reason"] == ""
    assert d["error"] == ""


# ---------------------------------------------------------------------------
# _build_subprocess_env: PROJECT_ROOT override (env-scoping bug fix)
# ---------------------------------------------------------------------------

def test_build_subprocess_env_overrides_project_root(tmp_path):
    """PROJECT_ROOT must be forced to kim_root, not inherited from parent env."""
    stale_root = "/stale/project/root"
    with patch.dict("os.environ", {"PROJECT_ROOT": stale_root}, clear=False):
        env = _build_subprocess_env(tmp_path)
    assert env["PROJECT_ROOT"] == str(tmp_path)
    assert env["PROJECT_ROOT"] != stale_root


def test_build_subprocess_env_sets_project_root_when_absent(tmp_path):
    """PROJECT_ROOT is set to kim_root even when absent from parent env."""
    stripped = {k: v for k, v in __import__("os").environ.items() if k != "PROJECT_ROOT"}
    with patch.dict("os.environ", stripped, clear=True):
        env = _build_subprocess_env(tmp_path)
    assert env["PROJECT_ROOT"] == str(tmp_path)


def test_build_subprocess_env_sets_pythonpath(tmp_path):
    env = _build_subprocess_env(tmp_path)
    assert env["PYTHONPATH"] == str(tmp_path)


def test_build_subprocess_env_overrides_inherited_pythonpath(tmp_path):
    with patch.dict("os.environ", {"PYTHONPATH": "/old/path"}, clear=False):
        env = _build_subprocess_env(tmp_path)
    assert env["PYTHONPATH"] == str(tmp_path)


def test_preflight_receives_correct_project_root(tmp_path):
    """preflight env has PROJECT_ROOT == kim_root even if os.environ has a stale value."""
    store = _make_store(tmp_path)
    _add_due_task(store, provider="ollama")
    captured_envs = []

    def fake_run(args, **kwargs):
        captured_envs.append(kwargs.get("env", {}))
        m = MagicMock()
        m.returncode = 0
        m.stderr = m.stdout = ""
        return m

    stale_root = "/stale/root"
    with patch.dict("os.environ", {"PROJECT_ROOT": stale_root}, clear=False):
        with patch("orchestrator.scheduled_runner.subprocess.run", side_effect=fake_run), \
             patch("orchestrator.scheduled_runner.subprocess.Popen", return_value=MagicMock()):
            run_next_due_task(
                store_file=tmp_path / "schedules.json",
                kim_root=tmp_path,
                _interpreter_override="/fake/python",
            )

    assert captured_envs, "preflight was not called"
    assert captured_envs[0]["PROJECT_ROOT"] == str(tmp_path)
    assert captured_envs[0]["PROJECT_ROOT"] != stale_root


def test_popen_receives_correct_project_root(tmp_path):
    """Popen env has PROJECT_ROOT == kim_root even if os.environ has a stale value."""
    store = _make_store(tmp_path)
    _add_due_task(store, provider="ollama")
    captured_kwargs = {}

    def fake_popen(args, **kwargs):
        captured_kwargs.update(kwargs)
        return MagicMock()

    stale_root = "/stale/root"
    with patch.dict("os.environ", {"PROJECT_ROOT": stale_root}, clear=False):
        with patch("orchestrator.scheduled_runner.subprocess.run", side_effect=_ok_preflight), \
             patch("orchestrator.scheduled_runner.subprocess.Popen", side_effect=fake_popen):
            run_next_due_task(
                store_file=tmp_path / "schedules.json",
                kim_root=tmp_path,
                _interpreter_override="/fake/python",
            )

    assert captured_kwargs.get("env", {}).get("PROJECT_ROOT") == str(tmp_path)
    assert captured_kwargs["env"]["PROJECT_ROOT"] != stale_root


def test_popen_pythonpath_is_kim_root(tmp_path):
    """Popen env PYTHONPATH is set to kim_root regardless of parent env."""
    store = _make_store(tmp_path)
    _add_due_task(store, provider="ollama")
    captured_kwargs = {}

    def fake_popen(args, **kwargs):
        captured_kwargs.update(kwargs)
        return MagicMock()

    with patch.dict("os.environ", {"PYTHONPATH": "/old/pythonpath"}, clear=False):
        with patch("orchestrator.scheduled_runner.subprocess.run", side_effect=_ok_preflight), \
             patch("orchestrator.scheduled_runner.subprocess.Popen", side_effect=fake_popen):
            run_next_due_task(
                store_file=tmp_path / "schedules.json",
                kim_root=tmp_path,
                _interpreter_override="/fake/python",
            )

    assert captured_kwargs["env"]["PYTHONPATH"] == str(tmp_path)


# ---------------------------------------------------------------------------
# Per-run log file diagnostics
# ---------------------------------------------------------------------------

def test_successful_launch_creates_log_file(tmp_path):
    """On successful launch, a log file must exist under logs/scheduled_runs/."""
    store = _make_store(tmp_path)
    _add_due_task(store, provider="ollama")

    with patch("orchestrator.scheduled_runner.subprocess.run", side_effect=_ok_preflight), \
         patch("orchestrator.scheduled_runner.subprocess.Popen", return_value=MagicMock()):
        result = run_next_due_task(
            store_file=tmp_path / "schedules.json",
            kim_root=tmp_path,
            _interpreter_override="/fake/python",
        )

    assert result is not None
    assert result.launched is True
    assert result.log_file, "log_file path should be non-empty after launch"
    log_path = Path(result.log_file)
    assert log_path.exists(), f"log file must be created: {log_path}"
    assert log_path.parent == tmp_path / "logs" / "scheduled_runs"


def test_successful_launch_log_file_in_to_dict(tmp_path):
    """log_file is included in RunDueResult.to_dict() after a successful launch."""
    store = _make_store(tmp_path)
    _add_due_task(store, provider="ollama")

    with patch("orchestrator.scheduled_runner.subprocess.run", side_effect=_ok_preflight), \
         patch("orchestrator.scheduled_runner.subprocess.Popen", return_value=MagicMock()):
        result = run_next_due_task(
            store_file=tmp_path / "schedules.json",
            kim_root=tmp_path,
            _interpreter_override="/fake/python",
        )

    assert result is not None
    d = result.to_dict()
    assert "log_file" in d
    assert d["log_file"] == result.log_file
    assert d["log_file"]  # non-empty


def test_preflight_failure_does_not_create_log_file(tmp_path):
    """On preflight failure, no log file should be created."""
    store = _make_store(tmp_path)
    _add_due_task(store, provider="ollama")

    with patch("orchestrator.scheduled_runner.subprocess.run", side_effect=_fail_preflight):
        result = run_next_due_task(
            store_file=tmp_path / "schedules.json",
            kim_root=tmp_path,
            _interpreter_override="/fake/python",
        )

    assert result is not None
    assert result.launched is False
    assert result.log_file == ""
    log_dir = tmp_path / "logs" / "scheduled_runs"
    files = list(log_dir.glob("*.log")) if log_dir.exists() else []
    assert files == [], f"no log files should exist after preflight failure, found: {files}"


def test_spawn_failure_does_not_leave_log_file_as_launched(tmp_path):
    """On Popen OSError, launched must be False and log_file empty."""
    store = _make_store(tmp_path)
    _add_due_task(store, provider="ollama")

    with patch("orchestrator.scheduled_runner.subprocess.run", side_effect=_ok_preflight), \
         patch("orchestrator.scheduled_runner.subprocess.Popen", side_effect=OSError("bad exec")):
        result = run_next_due_task(
            store_file=tmp_path / "schedules.json",
            kim_root=tmp_path,
            _interpreter_override="/fake/python",
        )

    assert result is not None
    assert result.launched is False
    assert result.log_file == ""
    log_dir = tmp_path / "logs" / "scheduled_runs"
    files = list(log_dir.glob("*.log")) if log_dir.exists() else []
    assert files == [], f"spawn failure should remove the pre-created log file, found: {files}"


def test_record_run_timeout_returns_error_after_launch(tmp_path):
    """If schedule advancement cannot acquire the store lock, return a structured error."""
    store = _make_store(tmp_path)
    _add_due_task(store, provider="ollama")

    with patch("orchestrator.scheduled_runner.subprocess.run", side_effect=_ok_preflight), \
         patch("orchestrator.scheduled_runner.subprocess.Popen", return_value=MagicMock()), \
         patch("orchestrator.scheduled_runner.CronStore.record_run", side_effect=TimeoutError("lock busy")):
        result = run_next_due_task(
            store_file=tmp_path / "schedules.json",
            kim_root=tmp_path,
            _interpreter_override="/fake/python",
        )

    assert result is not None
    assert result.launched is True
    assert result.recorded is False
    assert result.log_file
    assert "record_run failed" in result.error
    assert "lock busy" in result.error


def test_log_file_name_contains_task_id(tmp_path):
    """Log filename must embed the task ID for traceability."""
    store = _make_store(tmp_path)
    t = _add_due_task(store, provider="ollama")

    with patch("orchestrator.scheduled_runner.subprocess.run", side_effect=_ok_preflight), \
         patch("orchestrator.scheduled_runner.subprocess.Popen", return_value=MagicMock()):
        result = run_next_due_task(
            store_file=tmp_path / "schedules.json",
            kim_root=tmp_path,
            _interpreter_override="/fake/python",
        )

    assert result is not None
    assert result.log_file
    # Task IDs can contain hyphens/underscores; safe_id replaces other chars with _
    safe_prefix = re.sub(r"[^\w-]", "_", t.id)
    assert safe_prefix in Path(result.log_file).name


def test_popen_receives_log_file_not_devnull(tmp_path):
    """Popen must NOT receive DEVNULL for stdout/stderr — it should get a real file."""
    import subprocess as real_subprocess

    store = _make_store(tmp_path)
    _add_due_task(store, provider="ollama")
    captured_kwargs = {}

    def fake_popen(args, **kwargs):
        captured_kwargs.update(kwargs)
        return MagicMock()

    with patch("orchestrator.scheduled_runner.subprocess.run", side_effect=_ok_preflight), \
         patch("orchestrator.scheduled_runner.subprocess.Popen", side_effect=fake_popen):
        run_next_due_task(
            store_file=tmp_path / "schedules.json",
            kim_root=tmp_path,
            _interpreter_override="/fake/python",
        )

    assert "stdout" in captured_kwargs
    assert captured_kwargs["stdout"] is not real_subprocess.DEVNULL
    assert captured_kwargs["stderr"] is not real_subprocess.DEVNULL
    # The file handle should be a real IO object (already closed after Popen)
    assert hasattr(captured_kwargs["stdout"], "name"), "expected a file handle with a .name"

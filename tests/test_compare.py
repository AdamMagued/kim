"""
Tests for orchestrator/compare.py — provider comparison harness.

All tests use a fake session factory so no real MCP server is spawned.
The fake factory yields a minimal agent-like object whose .run() returns
a fixed result dict.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from orchestrator.compare import _run_one_provider, _save_comparison, compare_providers


# ---------------------------------------------------------------------------
# Fake session factory helpers
# ---------------------------------------------------------------------------

def _fake_factory(result_map: dict):
    """
    Return a session factory that yields fake agents.

    result_map: {provider_name: run_result_dict}
    If the provider is not in the map the agent raises RuntimeError.
    """
    @asynccontextmanager
    async def _factory(config, provider_name):
        class _FakeAgent:
            async def run(self, task: str) -> dict:
                if provider_name not in result_map:
                    raise RuntimeError(f"provider {provider_name!r} not configured in test")
                return result_map[provider_name]
        yield _FakeAgent()

    return _factory


def _success_factory(providers=None, summary="done"):
    """Factory that reports success for all providers (or a specific set)."""
    return _fake_factory({
        p: {"success": True, "summary": summary, "termination": "task_complete"}
        for p in (providers or ["ollama", "claude", "browser"])
    })


def _fail_factory(provider_name: str, message: str = "provider error"):
    """Factory that raises for one provider."""
    @asynccontextmanager
    async def _factory(config, name):
        if name == provider_name:
            raise RuntimeError(message)
        class _FakeAgent:
            async def run(self, task):
                return {"success": True, "summary": "ok", "termination": "task_complete"}
        yield _FakeAgent()
    return _factory


def _timeout_factory(provider_name: str, delay: float = 999.0):
    """Factory that simulates a provider that never finishes."""
    @asynccontextmanager
    async def _factory(config, name):
        if name == provider_name:
            class _SlowAgent:
                async def run(self, task):
                    await asyncio.sleep(delay)
                    return {"success": True, "summary": "late", "termination": "task_complete"}
            yield _SlowAgent()
        else:
            class _FakeAgent:
                async def run(self, task):
                    return {"success": True, "summary": "ok", "termination": "task_complete"}
            yield _FakeAgent()
    return _factory


# ---------------------------------------------------------------------------
# _run_one_provider unit tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_one_success():
    factory = _success_factory(["ollama"], summary="disk usage: 42%")
    result = await _run_one_provider("check disk", "ollama", {}, 30.0, factory)
    assert result["provider"] == "ollama"
    assert result["success"] is True
    assert result["summary"] == "disk usage: 42%"
    assert result["termination"] == "task_complete"
    assert result["error"] is None
    assert result["duration_seconds"] >= 0.0


@pytest.mark.asyncio
async def test_run_one_provider_exception_captured():
    factory = _fail_factory("ollama", "API key missing")
    result = await _run_one_provider("check disk", "ollama", {}, 30.0, factory)
    assert result["success"] is False
    assert "API key missing" in result["error"]
    assert result["duration_seconds"] >= 0.0


@pytest.mark.asyncio
async def test_run_one_provider_timeout_reported():
    factory = _timeout_factory("slow-provider", delay=999.0)
    result = await _run_one_provider("check disk", "slow-provider", {}, 0.05, factory)
    assert result["success"] is False
    assert "timed out" in result["error"]


@pytest.mark.asyncio
async def test_run_one_provider_timeout_on_factory_startup():
    """Timeout must fire even when the factory (MCP server startup) hangs, not just agent.run().

    This guards against the regression where only agent.run() was wrapped in
    wait_for — a hung factory __aenter__ would freeze indefinitely.
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _hanging_factory(config, provider_name):
        # Simulate an MCP server that hangs at stdio handshake and never yields
        await asyncio.sleep(999.0)
        yield None  # unreachable

    result = await _run_one_provider("task", "hung-provider", {}, 0.05, _hanging_factory)
    assert result["success"] is False
    assert "timed out" in result["error"]
    assert result["duration_seconds"] < 5.0  # must not have waited the full 999s


@pytest.mark.asyncio
async def test_run_one_always_sets_duration():
    """duration_seconds is set even when the provider raises immediately."""
    factory = _fail_factory("ollama")
    result = await _run_one_provider("task", "ollama", {}, 30.0, factory)
    assert isinstance(result["duration_seconds"], float)
    assert result["duration_seconds"] >= 0.0


# ---------------------------------------------------------------------------
# compare_providers — result ordering and structure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compare_returns_results_in_input_order(tmp_path):
    factory = _success_factory(["a", "b", "c"])
    results, _ = await compare_providers(
        task="ping",
        providers=["c", "a", "b"],
        config={},
        save_dir=tmp_path,
        _session_factory=factory,
    )
    assert [r["provider"] for r in results] == ["c", "a", "b"]


@pytest.mark.asyncio
async def test_compare_partial_failure_does_not_abort(tmp_path):
    """Failure on one provider must not prevent subsequent providers from running."""
    @asynccontextmanager
    async def _mixed_factory(config, name):
        if name == "bad-provider":
            raise RuntimeError("no creds")
        class _FakeAgent:
            async def run(self, task):
                return {"success": True, "summary": "ok", "termination": "task_complete"}
        yield _FakeAgent()

    results, _ = await compare_providers(
        task="do something",
        providers=["good1", "bad-provider", "good2"],
        config={},
        save_dir=tmp_path,
        _session_factory=_mixed_factory,
    )
    assert len(results) == 3
    assert results[0]["success"] is True
    assert results[1]["success"] is False
    assert "no creds" in results[1]["error"]
    assert results[2]["success"] is True


@pytest.mark.asyncio
async def test_compare_result_dict_keys(tmp_path):
    factory = _success_factory(["ollama"])
    results, _ = await compare_providers(
        task="test",
        providers=["ollama"],
        config={},
        save_dir=tmp_path,
        _session_factory=factory,
    )
    expected_keys = {"provider", "success", "summary", "termination", "duration_seconds", "error"}
    assert expected_keys.issubset(set(results[0].keys()))


# ---------------------------------------------------------------------------
# compare_providers — input validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compare_raises_on_empty_task():
    with pytest.raises(ValueError, match="task must not be empty"):
        await compare_providers(task="", providers=["ollama"], config={})


@pytest.mark.asyncio
async def test_compare_raises_on_empty_providers():
    with pytest.raises(ValueError, match="providers list must not be empty"):
        await compare_providers(task="do it", providers=[], config={})


@pytest.mark.asyncio
async def test_compare_raises_on_blank_provider_name():
    with pytest.raises(ValueError, match="provider names must not be empty"):
        await compare_providers(task="do it", providers=["ollama", "   "], config={})


@pytest.mark.asyncio
async def test_compare_raises_on_duplicate_providers():
    with pytest.raises(ValueError, match="duplicate providers"):
        await compare_providers(task="do it", providers=["ollama", "OLLAMA"], config={})


@pytest.mark.asyncio
async def test_compare_raises_on_too_many_providers():
    with pytest.raises(ValueError, match="at most 8 providers"):
        await compare_providers(
            task="do it",
            providers=[f"p{i}" for i in range(9)],
            config={},
        )


@pytest.mark.asyncio
async def test_compare_raises_on_bad_timeout():
    with pytest.raises(ValueError, match="timeout_seconds must be >= 1"):
        await compare_providers(
            task="do it",
            providers=["ollama"],
            config={},
            timeout_seconds=0,
        )


# ---------------------------------------------------------------------------
# compare_providers — result persistence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compare_saves_json_file(tmp_path):
    factory = _success_factory(["ollama", "claude"])
    _, saved_path = await compare_providers(
        task="archive logs",
        providers=["ollama", "claude"],
        config={},
        timeout_seconds=30.0,
        save_dir=tmp_path,
        _session_factory=factory,
    )
    assert saved_path is not None
    assert saved_path.exists()
    files = list(tmp_path.glob("compare_*.json"))
    assert len(files) == 1

    data = json.loads(files[0].read_text())
    assert data["task"] == "archive logs"
    assert len(data["results"]) == 2
    assert data["results"][0]["provider"] == "ollama"
    assert data["results"][1]["provider"] == "claude"
    assert "started_at" in data
    assert "finished_at" in data
    assert data["providers"] == ["ollama", "claude"]
    assert data["timeout_seconds"] == 30.0


@pytest.mark.asyncio
async def test_compare_saved_metadata_fields(tmp_path):
    """saved JSON has providers list, timeout_seconds, finished_at >= started_at."""
    factory = _success_factory(["ollama", "browser"])
    await compare_providers(
        task="rotate logs",
        providers=["ollama", "browser"],
        config={},
        timeout_seconds=45.0,
        save_dir=tmp_path,
        _session_factory=factory,
    )
    data = json.loads(list(tmp_path.glob("compare_*.json"))[0].read_text())
    assert data["providers"] == ["ollama", "browser"]
    assert data["timeout_seconds"] == 45.0
    assert data["finished_at"] >= data["started_at"]


@pytest.mark.asyncio
async def test_compare_skip_save_with_devnull(tmp_path):
    factory = _success_factory(["ollama"])
    results, saved_path = await compare_providers(
        task="task",
        providers=["ollama"],
        config={},
        save_dir=Path(os.devnull),
        _session_factory=factory,
    )
    # os.devnull path → no file created (save suppressed) and saved_path is None
    assert len(results) == 1
    assert saved_path is None


@pytest.mark.asyncio
async def test_compare_save_failure_does_not_raise(monkeypatch, tmp_path):
    """Persistence failure must not abort the comparison results."""
    import orchestrator.compare as _cmod

    def _boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(_cmod, "_save_comparison", _boom)
    factory = _success_factory(["ollama"])
    results, saved_path = await compare_providers(
        task="task",
        providers=["ollama"],
        config={},
        save_dir=tmp_path,
        _session_factory=factory,
    )
    assert results[0]["success"] is True
    assert saved_path is None  # save failed → written_path stays None


# ---------------------------------------------------------------------------
# _save_comparison — collision safety
# ---------------------------------------------------------------------------

def _make_comparison(task: str, ts: str) -> dict:
    return {
        "started_at": ts,
        "finished_at": ts,
        "task": task,
        "providers": ["p"],
        "timeout_seconds": 30,
        "results": [{"provider": "p", "success": True}],
    }


def test_save_collision_same_second_produces_two_files(tmp_path):
    """Two _save_comparison calls with identical started_at yield two distinct files."""
    ts = "2026-06-06T10:00:00+00:00"
    path1 = _save_comparison(_make_comparison("task-A", ts), tmp_path)
    path2 = _save_comparison(_make_comparison("task-B", ts), tmp_path)

    assert path1 != path2
    assert path1.exists()
    assert path2.exists()

    data1 = json.loads(path1.read_text())
    data2 = json.loads(path2.read_text())
    assert data1["task"] == "task-A"
    assert data2["task"] == "task-B"


def test_save_collision_deterministic_suffix_naming(tmp_path):
    """Suffix numbering follows the _2, _3 convention and is deterministic."""
    ts = "2026-06-06T10:00:00+00:00"
    p1 = _save_comparison(_make_comparison("A", ts), tmp_path)
    p2 = _save_comparison(_make_comparison("B", ts), tmp_path)
    p3 = _save_comparison(_make_comparison("C", ts), tmp_path)

    assert p1.name == "compare_2026-06-06T10-00-00.json"
    assert p2.name == "compare_2026-06-06T10-00-00_2.json"
    assert p3.name == "compare_2026-06-06T10-00-00_3.json"

    tasks = {json.loads(p.read_text())["task"] for p in (p1, p2, p3)}
    assert tasks == {"A", "B", "C"}


def test_save_no_tmp_files_left_after_success(tmp_path):
    """No *.json.tmp files remain after a successful save."""
    ts = "2026-06-06T11:00:00+00:00"
    _save_comparison(_make_comparison("X", ts), tmp_path)
    _save_comparison(_make_comparison("Y", ts), tmp_path)
    assert list(tmp_path.glob("*.json.tmp")) == []


def test_save_partial_file_cleaned_up_on_write_failure(tmp_path, monkeypatch):
    """If writing fails after exclusive create, the claimed file is removed."""
    import orchestrator.compare as _cmod
    from pathlib import Path as _Path

    _real_open = _Path.open

    def _patched_open(self, mode="r", **kwargs):
        fh = _real_open(self, mode, **kwargs)
        if "x" in str(mode):
            class _FailWriter:
                def write(self, data):
                    raise OSError("disk full")
                def close(self):
                    fh.close()
            return _FailWriter()
        return fh

    monkeypatch.setattr(_cmod.Path, "open", _patched_open)
    ts = "2026-06-06T12:00:00+00:00"
    with pytest.raises(OSError, match="disk full"):
        _save_comparison(_make_comparison("fail", ts), tmp_path)

    assert list(tmp_path.glob("*.json")) == []
    assert list(tmp_path.glob("*.json.tmp")) == []


def test_save_atomic_race_falls_back_to_suffix(tmp_path, monkeypatch):
    """FileExistsError on open('x') (simulated concurrent claim) causes _2 fallback."""
    import orchestrator.compare as _cmod
    from pathlib import Path as _Path

    _real_open = _Path.open
    first_x_call = [True]

    def _patched_open(self, mode="r", **kwargs):
        if "x" in str(mode) and first_x_call[0]:
            first_x_call[0] = False
            raise FileExistsError("claimed by concurrent process")
        return _real_open(self, mode, **kwargs)

    monkeypatch.setattr(_cmod.Path, "open", _patched_open)
    ts = "2026-06-06T10:00:00+00:00"
    path = _save_comparison(_make_comparison("race-winner", ts), tmp_path)

    assert path.name == "compare_2026-06-06T10-00-00_2.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["task"] == "race-winner"

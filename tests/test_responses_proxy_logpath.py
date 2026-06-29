"""
Regression tests for the log-path setup in cli/src/responses_proxy.py.

Guards two behaviors:
1. log_path_uses_tempdir_not_hardcoded — the log path is built via
   tempfile.gettempdir() (honoring KIM_LOG) rather than the literal
   string '/tmp/kim_proxy.log', so the module does not crash on Windows.
2. import_succeeds — executing the module's log-path setup lines does not
   raise FileNotFoundError when /tmp is unavailable (gettempdir patched to
   a caller-supplied directory).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import mock_open, patch

import pytest

# ---------------------------------------------------------------------------
# Locate source under test
# ---------------------------------------------------------------------------

_SOURCE = Path(__file__).resolve().parent.parent / "cli" / "src" / "responses_proxy.py"


def _read_source() -> str:
    return _SOURCE.read_text(encoding="utf-8")


def _log_path_line(source: str) -> str:
    """Return the source line that assigns _log_path."""
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("_log_path"):
            return stripped
    raise AssertionError("_log_path assignment not found in responses_proxy.py")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_log_path_uses_tempdir_not_hardcoded():
    """
    The _log_path assignment must use tempfile.gettempdir(), not a hardcoded
    '/tmp/kim_proxy.log' literal, so the module works on non-POSIX systems.

    Also verifies that KIM_LOG env-var is checked first (override path).
    """
    source = _read_source()
    line = _log_path_line(source)

    # Must call tempfile.gettempdir() — the portable approach
    assert "tempfile.gettempdir()" in line, (
        "_log_path must be built with tempfile.gettempdir(), "
        f"got: {line!r}"
    )

    # Must NOT fall back to the old hardcoded literal '/tmp/kim_proxy.log'
    assert '"/tmp/kim_proxy.log"' not in line, (
        "_log_path must not hardcode '/tmp/kim_proxy.log' as a string literal"
    )
    assert "'/tmp/kim_proxy.log'" not in line, (
        "_log_path must not hardcode '/tmp/kim_proxy.log' as a string literal"
    )

    # KIM_LOG env-var must be the override mechanism
    assert '"KIM_LOG"' in line or "'KIM_LOG'" in line, (
        "_log_path must honour the KIM_LOG environment variable override"
    )


def test_log_path_uses_tempdir_not_hardcoded_behavioral(tmp_path, monkeypatch):
    """
    Behavioral companion: exec the actual _log_path line under a patched
    gettempdir and confirm the resulting path sits inside the fake dir, not /tmp.
    """
    monkeypatch.delenv("KIM_LOG", raising=False)

    source = _read_source()
    line = _log_path_line(source)

    fake_dir = str(tmp_path)
    ns: dict = {"os": os, "tempfile": tempfile}

    with patch("tempfile.gettempdir", return_value=fake_dir):
        exec(line, ns)  # noqa: S102

    log_path: str = ns["_log_path"]
    assert log_path.startswith(fake_dir), (
        f"Expected log path inside {fake_dir!r}, got {log_path!r}. "
        "This means gettempdir() patch was not honoured — "
        "the path may still be hardcoded."
    )
    assert log_path.endswith("kim_proxy.log"), (
        f"Expected filename 'kim_proxy.log', got {log_path!r}"
    )


def test_import_succeeds(tmp_path, monkeypatch):
    """
    Executing the module's log-path setup must not raise FileNotFoundError
    when /tmp is unavailable.  We patch tempfile.gettempdir to return a
    caller-controlled directory and stub out the open() call so no real file
    system side-effect is needed.
    """
    monkeypatch.delenv("KIM_LOG", raising=False)

    source = _read_source()
    log_path_line = _log_path_line(source)

    # Also find the _LOG = open(...) line — it must be present and patched
    open_line = None
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("_LOG") and "open(" in stripped:
            open_line = stripped
            break

    fake_dir = str(tmp_path)
    ns: dict = {"os": os, "tempfile": tempfile}

    # Patch gettempdir so /tmp is "unavailable" (redirected to tmp_path)
    with patch("tempfile.gettempdir", return_value=fake_dir):
        # Must not raise when executing the log-path assignment
        try:
            exec(log_path_line, ns)  # noqa: S102
        except FileNotFoundError as exc:
            pytest.fail(
                f"Log-path setup raised FileNotFoundError with gettempdir patched: {exc}"
            )

        # Simulate the subsequent open() with a mock; must not raise either
        if open_line is not None:
            with patch("builtins.open", mock_open()):
                try:
                    exec(open_line, {**ns, "open": open})  # noqa: S102
                except FileNotFoundError as exc:
                    pytest.fail(
                        f"open(_log_path) raised FileNotFoundError "
                        f"with gettempdir patched to {fake_dir!r}: {exc}"
                    )

    # Sanity: the resolved path must be within the fake dir
    assert ns["_log_path"].startswith(fake_dir), (
        f"_log_path {ns['_log_path']!r} is not inside the patched dir {fake_dir!r}"
    )


def test_kim_log_env_override(tmp_path, monkeypatch):
    """
    When KIM_LOG is set, _log_path must equal that value.

    Note: Python evaluates the default argument of os.environ.get() eagerly,
    so tempfile.gettempdir() is called regardless of whether KIM_LOG is set.
    The important invariant is that the *result* comes from KIM_LOG, not from
    gettempdir().  We verify this by patching gettempdir to return a sentinel
    string and asserting the sentinel does NOT appear in the final path while
    custom_log does.
    """
    custom_log = str(tmp_path / "custom_kim.log")
    monkeypatch.setenv("KIM_LOG", custom_log)

    source = _read_source()
    line = _log_path_line(source)

    ns: dict = {"os": os, "tempfile": tempfile}

    sentinel = "/SENTINEL_SHOULD_NOT_APPEAR_IN_LOG_PATH"
    with patch("tempfile.gettempdir", return_value=sentinel):
        exec(line, ns)  # noqa: S102

    assert ns["_log_path"] == custom_log, (
        f"Expected _log_path == {custom_log!r}, got {ns['_log_path']!r}"
    )
    assert sentinel not in ns["_log_path"], (
        f"_log_path contains the gettempdir sentinel {sentinel!r}; "
        "KIM_LOG override is not taking effect"
    )

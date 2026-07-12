"""
Smoke tests for kimctl's bridge-facing commands: status, chats, show, send,
cancel, browser.

These commands previously had zero test coverage. Two layers, mirroring the
style of test_kimctl_trace.py / test_kimctl_compare.py:
  - Parser wiring: build_parser().parse_args([...]) matches what each cmd_*
    handler expects (dest names, defaults).
  - Handler smoke tests: cmd_* is called with a hand-crafted Namespace.
    Commands that hit the bridge (status/send/cancel/browser) monkeypatch
    kimctl.__main__._bridge_request with a fake httpx.Response-like object
    so no real HTTP call or running Kim instance is required. Commands that
    read local session files (chats/show) use KIM_SESSIONS_DIR pointed at a
    temp dir, same pattern as test_kimctl_trace.py.
"""
from __future__ import annotations

import json
import os
import tempfile
from argparse import Namespace
from pathlib import Path

import pytest

from kimctl.__main__ import (
    _read_home_bridge_token,
    _resolve_bridge,
    build_parser,
    cmd_browser,
    cmd_cancel,
    cmd_chats,
    cmd_show,
    cmd_status,
)


# ---------------------------------------------------------------------------
# Fake bridge response / request
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Minimal stand-in for httpx.Response — only .json() and .text are used."""

    def __init__(self, payload: dict):
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _fake_bridge(payload: dict):
    """Return a _bridge_request replacement that always returns `payload`."""
    def _fn(method, endpoint, **kwargs):
        return _FakeResponse(payload)
    return _fn


def _raising_bridge(exc: Exception):
    """Return a _bridge_request replacement that always raises `exc`."""
    def _fn(method, endpoint, **kwargs):
        raise exc
    return _fn


def _write_jsonl(path: Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, separators=(",", ":")) for r in records) + "\n",
        encoding="utf-8",
    )


def _with_sessions_dir(sessions_dir: Path, fn, *args, **kwargs):
    """Run fn with KIM_SESSIONS_DIR pointed at sessions_dir (same pattern as
    test_kimctl_trace.py's _run_trace)."""
    old = os.environ.get("KIM_SESSIONS_DIR")
    os.environ["KIM_SESSIONS_DIR"] = str(sessions_dir)
    try:
        return fn(*args, **kwargs)
    finally:
        if old is None:
            os.environ.pop("KIM_SESSIONS_DIR", None)
        else:
            os.environ["KIM_SESSIONS_DIR"] = old


# ---------------------------------------------------------------------------
# Parser wiring
# ---------------------------------------------------------------------------

def test_status_parser_registered():
    p = build_parser()
    args = p.parse_args(["status"])
    assert args.command == "status"
    assert args.json is False


def test_chats_parser_registered():
    p = build_parser()
    args = p.parse_args(["chats", "--json"])
    assert args.command == "chats"
    assert args.json is True


def test_show_parser_registered():
    p = build_parser()
    args = p.parse_args(["show", "abc123", "--last", "5"])
    assert args.command == "show"
    assert args.session_id == "abc123"
    assert args.last == 5


def test_send_parser_defaults():
    p = build_parser()
    args = p.parse_args(["send", "do the thing"])
    assert args.command == "send"
    assert args.task == "do the thing"
    assert args.timeout == 300
    assert args.detach is False
    assert args.session is None
    assert args.provider is None


def test_send_parser_explicit_timeout_zero():
    """--timeout 0 must survive argparse as an explicit 0, not fall back to 300."""
    p = build_parser()
    args = p.parse_args(["send", "task", "--timeout", "0"])
    assert args.timeout == 0


def test_cancel_parser_registered():
    p = build_parser()
    args = p.parse_args(["cancel", "--json"])
    assert args.command == "cancel"
    assert args.json is True


def test_browser_parser_registered():
    p = build_parser()
    args = p.parse_args(["browser", "click", "#submit"])
    assert args.command == "browser"
    assert args.browser_action == "click"
    assert args.selector == "#submit"


def test_browser_parser_rejects_unknown_action():
    p = build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["browser", "teleport"])


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------

def test_status_json_output(monkeypatch, capsys):
    monkeypatch.setattr(
        "kimctl.__main__._bridge_request",
        _fake_bridge({
            "has_running_task": True,
            "browser_visible": False,
            "active_session_id": "sess01",
        }),
    )
    cmd_status(Namespace(json=True))
    data = json.loads(capsys.readouterr().out)
    assert data["has_running_task"] is True
    assert data["active_session_id"] == "sess01"


def test_status_human_readable_output(monkeypatch, capsys):
    monkeypatch.setattr(
        "kimctl.__main__._bridge_request",
        _fake_bridge({
            "has_running_task": False,
            "browser_visible": True,
            "active_session_id": None,
        }),
    )
    cmd_status(Namespace(json=False))
    out = capsys.readouterr().out
    assert "Kim Status:" in out
    assert "Running task:" in out
    assert "Browser:" in out


def test_status_transport_error_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(
        "kimctl.__main__._bridge_request",
        _raising_bridge(ConnectionError("bridge down")),
    )
    with pytest.raises(SystemExit) as exc_info:
        cmd_status(Namespace(json=False))
    assert exc_info.value.code != 0
    assert "Error connecting to Kim bridge" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_chats (local file reads — no bridge)
# ---------------------------------------------------------------------------

def test_chats_json_lists_sessions(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_jsonl(base / "2026-06-05" / "sess01.jsonl", [
            {"role": "user", "content": "hello"},
        ])
        _with_sessions_dir(base, cmd_chats, Namespace(json=True))
        data = json.loads(capsys.readouterr().out)

    assert len(data) == 1
    assert data[0]["id"] == "sess01"


def test_chats_human_readable_empty(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "empty"
        _with_sessions_dir(base, cmd_chats, Namespace(json=False))
        out = capsys.readouterr().out
    assert "No sessions found." in out


# ---------------------------------------------------------------------------
# cmd_show (local file reads — no bridge)
# ---------------------------------------------------------------------------

def test_show_json_returns_messages(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_jsonl(base / "2026-06-05" / "sess02.jsonl", [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello back"},
        ])
        _with_sessions_dir(
            base, cmd_show, Namespace(session_id="sess02", last=None, json=True)
        )
        data = json.loads(capsys.readouterr().out)

    assert len(data) == 2
    assert data[0]["role"] == "user"


def test_show_missing_session_exits_nonzero(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        with pytest.raises(SystemExit) as exc_info:
            _with_sessions_dir(
                base, cmd_show,
                Namespace(session_id="ghost", last=None, json=False),
            )
    assert exc_info.value.code != 0
    assert "not found" in capsys.readouterr().err


def test_show_last_n_truncates_messages(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_jsonl(base / "2026-06-05" / "sess03.jsonl", [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ])
        _with_sessions_dir(
            base, cmd_show, Namespace(session_id="sess03", last=1, json=True)
        )
        data = json.loads(capsys.readouterr().out)

    assert len(data) == 1
    assert data[0]["content"] == "three"


# ---------------------------------------------------------------------------
# cmd_cancel
# ---------------------------------------------------------------------------

def test_cancel_json_ok(monkeypatch, capsys):
    monkeypatch.setattr(
        "kimctl.__main__._bridge_request",
        _fake_bridge({"ok": True, "message": "Cancelled"}),
    )
    cmd_cancel(Namespace(json=True))
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True


def test_cancel_human_readable_failure(monkeypatch, capsys):
    monkeypatch.setattr(
        "kimctl.__main__._bridge_request",
        _fake_bridge({"ok": False, "message": "No task running"}),
    )
    cmd_cancel(Namespace(json=False))
    out = capsys.readouterr().out
    assert "No task running" in out
    assert "❌" in out  # ❌


def test_cancel_transport_error_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(
        "kimctl.__main__._bridge_request",
        _raising_bridge(ConnectionError("bridge down")),
    )
    with pytest.raises(SystemExit) as exc_info:
        cmd_cancel(Namespace(json=False))
    assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# cmd_browser
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", ["show", "hide", "new-chat"])
def test_browser_actions_without_selector(monkeypatch, capsys, action):
    seen = {}

    def _fn(method, endpoint, **kwargs):
        seen["endpoint"] = endpoint
        return _FakeResponse({"ok": True})

    monkeypatch.setattr("kimctl.__main__._bridge_request", _fn)
    cmd_browser(Namespace(browser_action=action, selector=None, json=True))
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert seen["endpoint"] == f"/v1/browser/{action}"


def test_browser_click_requires_selector(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cmd_browser(Namespace(browser_action="click", selector=None, json=False))
    assert exc_info.value.code != 0
    assert "selector" in capsys.readouterr().err.lower()


def test_browser_click_with_selector_sends_it(monkeypatch, capsys):
    seen = {}

    def _fn(method, endpoint, **kwargs):
        seen["endpoint"] = endpoint
        seen["json"] = kwargs.get("json")
        return _FakeResponse({"ok": True})

    monkeypatch.setattr("kimctl.__main__._bridge_request", _fn)
    cmd_browser(Namespace(browser_action="click", selector="#go", json=True))
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert seen["endpoint"] == "/v1/browser/click"
    assert seen["json"] == {"selector": "#go"}


def test_browser_human_readable_error(monkeypatch, capsys):
    monkeypatch.setattr(
        "kimctl.__main__._bridge_request",
        _fake_bridge({"ok": False, "error": "not connected"}),
    )
    cmd_browser(Namespace(browser_action="show", selector=None, json=False))
    out = capsys.readouterr().out
    assert "not connected" in out


# ---------------------------------------------------------------------------
# F-E-13: kimctl must read the desktop's ~/.kim/bridge_token pairing file.
# ---------------------------------------------------------------------------

def _clear_bridge_env(monkeypatch):
    for var in (
        "KIM_WEBVIEW_BRIDGE_URL",
        "KIM_WEBVIEW_BRIDGE_TOKEN",
        "KIM_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def test_read_home_bridge_token_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _read_home_bridge_token() == ""


def test_resolve_bridge_reads_home_token(monkeypatch, tmp_path):
    _clear_bridge_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    kim_dir = tmp_path / ".kim"
    kim_dir.mkdir()
    (kim_dir / "bridge_token").write_text("home-tok-123\n", encoding="utf-8")

    _, token = _resolve_bridge()
    assert token == "home-tok-123"


def test_resolve_bridge_home_token_yields_to_env(monkeypatch, tmp_path):
    """Mirror cli/src/provider/bridge.rs: an explicit KIM_API_KEY env override
    wins over the ~/.kim/bridge_token pairing file."""
    _clear_bridge_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    kim_dir = tmp_path / ".kim"
    kim_dir.mkdir()
    (kim_dir / "bridge_token").write_text("home-tok", encoding="utf-8")
    monkeypatch.setenv("KIM_API_KEY", "env-key-wins")

    _, token = _resolve_bridge()
    assert token == "env-key-wins"

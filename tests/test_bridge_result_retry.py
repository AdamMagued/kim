"""F-B-11: after a delivered /v1/send, a transient /v1/result GET failure must
retry (re-polling req_id is idempotent) instead of abandoning the answer."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from orchestrator.providers.browser import bridge_client as bc


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeClient:
    def __init__(self, get_script, get_calls, **_kw):
        self._get_script = get_script
        self._get_calls = get_calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def post(self, url, headers=None, json=None):
        if url.endswith("/v1/send"):
            return _FakeResp(200, {"req_id": "r1", "sent_confirmed": True, "site": "claude"})
        return _FakeResp(404, None)

    async def get(self, url, headers=None):
        self._get_calls.append(url)
        behavior = self._get_script.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


def _run_with_script(monkeypatch, get_script):
    get_calls: list[str] = []

    def _factory(**kw):
        return _FakeClient(get_script, get_calls, **kw)

    monkeypatch.setattr(bc.httpx, "AsyncClient", _factory)
    monkeypatch.setattr(bc.asyncio, "sleep", _no_sleep)

    result = asyncio.run(bc.complete_via_webview_bridge(
        bridge_url="http://127.0.0.1:1",
        bridge_token="tok",
        preferred_site="claude",
        model_tier=None,
        gemini_authuser=None,
        prompt="hi",
        attachments=[],
        completion_hash="[END_OF_RESPONSE_x]",
        known_tools=None,
    ))
    return result, get_calls


async def _no_sleep(_s):
    return None


def test_transient_result_error_is_retried_then_succeeds(monkeypatch):
    ok = _FakeResp(200, {"ok": True, "response": "TASK_COMPLETE: hi [END_OF_RESPONSE_x]"})
    script = [httpx.ConnectError("connection reset"), ok]
    result, get_calls = _run_with_script(monkeypatch, script)
    # Re-polled the SAME req_id (idempotent) rather than abandoning the answer.
    assert len(get_calls) == 2
    assert all(u.endswith("/v1/result/r1") for u in get_calls)
    assert result["type"] == "text"
    assert "TASK_COMPLETE" in result["content"]


def test_result_timeout_is_terminal_not_retried(monkeypatch):
    # A genuine long-poll timeout (window elapsed) must not spin — one attempt.
    script = [httpx.ReadTimeout("timed out")]
    result, get_calls = _run_with_script(monkeypatch, script)
    assert len(get_calls) == 1
    assert "timeout" in result["content"].lower()


def test_persistent_transport_error_gives_up_after_retries(monkeypatch):
    script = [httpx.ConnectError("reset")] * 3
    result, get_calls = _run_with_script(monkeypatch, script)
    assert len(get_calls) == 3  # exhausted the retry budget
    assert "result poll failed" in result["content"].lower()

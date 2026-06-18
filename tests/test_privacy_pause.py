"""K9: privacy pause blocks screen-capture tools with a typed error."""

import asyncio
import json

import mcp_server.privacy as privacy
from mcp_server.tools.screen import handle_take_screenshot


def test_paused_screenshot_returns_typed_error(monkeypatch):
    monkeypatch.setattr(privacy, "is_privacy_paused", lambda: True)
    # screen.py imported the symbol directly — patch there too.
    monkeypatch.setattr("mcp_server.tools.screen.is_privacy_paused", lambda: True)
    out = asyncio.run(handle_take_screenshot({}))
    parsed = json.loads(out)
    assert parsed["error"] == "privacy_pause"
    assert "Privacy pause" in parsed["message"]


def test_sentinel_detection(tmp_path, monkeypatch):
    sentinel = tmp_path / "privacy_pause"
    monkeypatch.setattr(privacy, "PRIVACY_SENTINEL", sentinel)
    assert privacy.is_privacy_paused() is False
    sentinel.write_text("1")
    assert privacy.is_privacy_paused() is True

"""Tests for FakeProvider and KIM_FAKE=1 env wiring."""
import os
import pytest


def test_fake_provider_returns_scripted_responses():
    from orchestrator.providers.fake import FakeProvider
    provider = FakeProvider(responses=[
        {"type": "tool_call", "tool": "take_screenshot", "args": {}},
        {"type": "text", "content": "TASK_COMPLETE: done"},
    ])
    import asyncio
    r1 = asyncio.run(provider.complete([], [], "system"))
    assert r1["type"] == "tool_call"
    assert r1["tool"] == "take_screenshot"

    r2 = asyncio.run(provider.complete([], [], "system"))
    assert r2["type"] == "text"
    assert "TASK_COMPLETE" in r2["content"]


def test_fake_provider_loops_on_last_response():
    from orchestrator.providers.fake import FakeProvider
    provider = FakeProvider(responses=[
        {"type": "text", "content": "TASK_COMPLETE: only"},
    ])
    import asyncio
    for _ in range(3):
        r = asyncio.run(provider.complete([], [], ""))
        assert "TASK_COMPLETE" in r["content"]


def test_create_provider_fake_name():
    from orchestrator.providers.base import create_provider
    p = create_provider("fake", {})
    from orchestrator.providers.fake import FakeProvider
    assert isinstance(p, FakeProvider)


def test_kim_fake_env_overrides_provider(monkeypatch):
    monkeypatch.setenv("KIM_FAKE", "1")
    from orchestrator.providers.base import create_provider
    from orchestrator.providers.fake import FakeProvider
    p = create_provider("claude", {})
    assert isinstance(p, FakeProvider)


def test_kim_fake_env_zero_does_not_override(monkeypatch):
    monkeypatch.setenv("KIM_FAKE", "0")
    from orchestrator.providers.base import create_provider
    from orchestrator.providers.fake import FakeProvider
    p = create_provider("fake", {})
    assert isinstance(p, FakeProvider)

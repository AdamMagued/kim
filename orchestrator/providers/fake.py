"""
FakeProvider — scripted offline provider for KIM_FAKE=1 mode and tests.

Returns a one-shot TASK_COMPLETE response after optionally emitting one
tool call (take_screenshot by default) to exercise the full agent loop.

Usage:
    KIM_FAKE=1 python -m orchestrator.agent --task "test task"

Or in tests:
    from orchestrator.providers.fake import FakeProvider
    provider = FakeProvider(responses=[
        {"type": "tool_call", "tool": "take_screenshot", "args": {}},
        {"type": "text", "content": "TASK_COMPLETE: done"},
    ])
"""

from __future__ import annotations

from orchestrator.providers.base import BaseProvider


class FakeProvider(BaseProvider):
    """Returns scripted responses in order; loops on the last one."""

    native_tool_calling: bool = False
    lean_system_prompt: bool = False

    def __init__(self, responses: list[dict] | None = None) -> None:
        self._responses = responses or [
            {"type": "tool_call", "tool": "take_screenshot", "args": {}},
            {"type": "text", "content": "TASK_COMPLETE: Fake run complete. No real tools or APIs used."},
        ]
        self._index = 0

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
    ) -> dict:
        response = self._responses[min(self._index, len(self._responses) - 1)]
        self._index += 1
        return response

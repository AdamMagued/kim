"""V-3a: Golden-transcript seam test.

Runs KimAgent end-to-end with FakeProvider + a mocked MCP session and
captures every stdout line the agent emits.  Verifies that each line has
the correct shape for its event type, then writes/compares a canonical
fixture at tests/fixtures/golden_transcript.json.

The fixture is the seam: if the Python agent changes what it emits, the
fixture update makes the change explicit and forces a matching update to
the Vitest golden test (agentProtocol.test.ts).
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "golden_transcript.json"

# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_mcp_tool(name: str):
    t = MagicMock()
    t.name = name
    t.description = f"Mock {name}"
    t.inputSchema = {"type": "object", "properties": {}}
    return t


def _make_tool_result(text: str):
    content_item = MagicMock()
    content_item.text = text
    result = MagicMock()
    result.content = [content_item]
    return result


def _build_agent():
    """Build a KimAgent wired for offline golden-transcript capture."""
    # Ensure heavy runtime stubs so imports don't fail.
    import types as _types
    for mod in ("mss", "pynput", "pynput.mouse", "pynput.keyboard",
                "pyautogui", "PIL", "PIL.Image", "sounddevice", "soundfile",
                "kokoro", "pygetwindow"):
        if mod not in sys.modules:
            stub = _types.ModuleType(mod)
            sys.modules[mod] = stub

    from orchestrator.providers.fake import FakeProvider
    from orchestrator.context_meter import ContextMeter
    from orchestrator.memory import ConversationMemory
    from orchestrator.interaction_policy import InteractionPolicy
    from orchestrator.agent import KimAgent

    fake = FakeProvider(responses=[
        {"type": "tool_call", "tool": "take_screenshot", "args": {}},
        {"type": "text", "content": "TASK_COMPLETE: Golden run complete."},
    ])

    agent = object.__new__(KimAgent)
    agent.config = {
        "max_iterations": 10,
        "screenshot_scale": 0.75,
        "memory_max_messages": 40,
        "memory_keep_screenshots": 4,
        "context_budget_tokens": 100_000,
    }
    agent.provider = fake
    agent.max_iterations = 10
    agent.screenshot_scale = 0.75
    agent.memory = ConversationMemory(max_messages=40, keep_screenshots=4)
    agent._ui_bridge = None
    agent._session_store = MagicMock()
    agent._resume_session_id = None
    agent._screenshot_hashes = []
    agent._recent_action_sigs = []
    agent._tools = []
    agent._total_tokens = {"input": 0, "output": 0}
    agent._last_plan_signature = ""
    agent._last_step_signature = ""
    agent._last_done_signature = ""
    agent._current_plan_steps = []
    agent._current_step_index = 0
    agent._clear_chat_on_next_call = False
    agent._max_retries = 3
    agent._retry_base_delay = 1.0
    agent._retry_max_delay = 60.0
    agent._hitl_risk_threshold = None
    agent._interaction_policy = InteractionPolicy(block_high_risk=False)
    # Use a real ContextMeter so emitted JSON has proper integer fields.
    agent._context_meter = ContextMeter(budget=100_000)

    # Mock async MCP session
    list_result = MagicMock()
    list_result.tools = [
        _make_mcp_tool("take_screenshot"),
        _make_mcp_tool("run_command"),
    ]
    session = AsyncMock()
    session.list_tools = AsyncMock(return_value=list_result)
    session.call_tool = AsyncMock(return_value=_make_tool_result("SCREENSHOT_B64_MOCK"))
    agent.session = session

    return agent


def _capture_run(task: str = "test task for golden transcript") -> list[str]:
    """Run the agent, capture stdout, return list of non-empty lines."""
    agent = _build_agent()
    buf = io.StringIO()
    # Patch asyncio.sleep to skip the 0.45s screenshot settle delay.
    with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        with redirect_stdout(buf):
            asyncio.run(agent.run(task))
    return [ln for ln in buf.getvalue().splitlines() if ln.strip()]


# ── Shape validators ───────────────────────────────────────────────────────────

_TYPED_JSON_REQUIRED_KEYS: dict[str, list[str]] = {
    "status": ["message"],
    "context": ["cumulative_input", "budget", "phase", "percent",
                "last_input", "last_output", "source", "estimate"],
    "stats": ["input", "output", "total"],
    "usage": [],  # keys vary by provider
    "ui_screenshot_flash": [],
    "ui_show": [],
    "plan": ["steps"],
    "step": ["n", "data"],
    "done": ["n"],
    "run_done": ["termination", "success"],
    "run_failed": ["reason", "recoverable", "suggestion"],
    "provider_error": ["code", "retryable"],
    "rate_limited": ["delay", "attempt", "max_retries"],
    "hitl_approval_request": ["tool", "risk", "reason"],
    "hitl_approval_result": ["tool", "approved"],
    "tool": ["name", "args"],
    "answer": ["text"],
    "diff": ["path", "added", "removed"],
    "activity": ["kind", "text"],
}

_LEGACY_TEXT_PREFIXES = ("[TOOL] ", "[SUCCESS]", "[FAILED]")


def _assert_line_shape(line: str) -> None:
    """Assert that a protocol line has the expected shape for its type."""
    if line.startswith("{"):
        parsed = json.loads(line)
        assert "type" in parsed, f"JSON line missing 'type': {line}"
        event_type = parsed["type"]
        required = _TYPED_JSON_REQUIRED_KEYS.get(event_type)
        assert required is not None, f"Unknown event type '{event_type}': {line}"
        for key in required:
            assert key in parsed, f"'{event_type}' missing key '{key}': {line}"
    elif line.startswith("[TOOL] "):
        # Legacy tool call: [TOOL] toolName({...})
        assert "(" in line, f"[TOOL] line missing '(': {line}"
    elif line.startswith("[SUCCESS]") or line.startswith("[FAILED]"):
        pass  # These are valid success/failure markers
    else:
        pytest.fail(f"Unexpected protocol line format: {line!r}")


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestGoldenTranscriptCapture:
    """Run the agent and verify the captured stdout matches protocol expectations."""

    def test_golden_transcript_runs_without_error(self):
        """Agent completes successfully with FakeProvider + mocked session."""
        lines = _capture_run()
        assert len(lines) > 0, "Agent emitted no stdout lines"

    def test_golden_transcript_first_line_is_status(self):
        """First stdout line must be the 'Kim is working' status event."""
        lines = _capture_run()
        first = json.loads(lines[0])
        assert first["type"] == "status"
        assert "message" in first
        assert first["message"].strip()

    def test_golden_transcript_contains_context_events(self):
        """At least two context snapshot events (start of run + post-LLM-call)."""
        lines = _capture_run()
        context_lines = [ln for ln in lines if '"type":"context"' in ln or '"type": "context"' in ln]
        assert len(context_lines) >= 2, (
            f"Expected >=2 context events, got {len(context_lines)}. Lines: {lines}"
        )

    def test_golden_transcript_contains_tool_call(self):
        """Schema-generated tool event must be present for take_screenshot."""
        lines = _capture_run()
        tool_lines = [
            json.loads(line)
            for line in lines
            if line.startswith("{") and json.loads(line).get("type") == "tool"
        ]
        assert len(tool_lines) == 1
        assert tool_lines[0]["name"] == "take_screenshot"

    def test_golden_transcript_contains_ui_events(self):
        """Screenshot flash and show events must bracket the tool call."""
        lines = _capture_run()
        tool_idx = next(i for i, line in enumerate(lines) if '"type":"tool"' in line)
        # screenshot_flash must appear after the typed tool event (pre-execution signal)
        flash_indices = [i for i, ln in enumerate(lines) if '"type":"ui_screenshot_flash"' in ln]
        show_indices = [i for i, ln in enumerate(lines) if '"type":"ui_show"' in ln]
        assert flash_indices, "Expected ui_screenshot_flash event"
        assert show_indices, "Expected ui_show event"
        # flash comes after the tool event, show comes after flash
        assert any(idx > tool_idx for idx in flash_indices), "flash_idx should be > tool_idx"
        assert show_indices[0] > flash_indices[0], "ui_show should come after ui_screenshot_flash"

    def test_golden_transcript_all_lines_valid_shape(self):
        """Every emitted line must have valid protocol shape."""
        lines = _capture_run()
        for line in lines:
            _assert_line_shape(line)

    def test_golden_transcript_no_run_failed_on_success(self):
        """Successful run must not emit a run_failed event."""
        lines = _capture_run()
        run_failed_lines = [ln for ln in lines if '"type":"run_failed"' in ln]
        assert len(run_failed_lines) == 0, f"Unexpected run_failed: {run_failed_lines}"


class TestGoldenTranscriptFixture:
    """Write the golden fixture on first run; compare types on subsequent runs."""

    def test_fixture_types_match_or_write(self):
        """Capture transcript, write fixture if missing, else compare event types."""
        lines = _capture_run()

        # Classify each line
        def classify(line: str) -> str:
            if line.startswith("{"):
                try:
                    return json.loads(line)["type"]
                except (json.JSONDecodeError, KeyError):
                    return "unknown_json"
            if line.startswith("[TOOL] "):
                return "legacy_tool"
            if line.startswith("[SUCCESS]"):
                return "legacy_success"
            return "unknown"

        current_types = [classify(ln) for ln in lines]

        if not FIXTURE_PATH.exists():
            # A missing golden must FAIL, not silently regenerate-and-pass:
            # a deleted/gitignored fixture previously yielded green CI with
            # zero comparison. Regeneration is explicit via env flag.
            if os.environ.get("KIM_REGEN_GOLDEN") == "1":
                FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
                FIXTURE_PATH.write_text(
                    json.dumps({"lines": lines, "types": current_types}, indent=2)
                )
                return  # Explicit regeneration requested
            pytest.fail(
                f"Golden fixture missing: {FIXTURE_PATH}. "
                "If this is intentional (protocol change), regenerate with "
                "KIM_REGEN_GOLDEN=1 pytest tests/test_v3_golden_transcript.py "
                "and commit the fixture."
            )

        fixture = json.loads(FIXTURE_PATH.read_text())
        saved_types = fixture.get("types", [])
        assert current_types == saved_types, (
            f"Protocol drift detected!\n"
            f"  Saved types:   {saved_types}\n"
            f"  Current types: {current_types}\n"
            f"  To update, delete {FIXTURE_PATH} and re-run."
        )


class TestRunFailedEmission:
    """run_failed event is emitted for non-success terminations."""

    def test_run_failed_emitted_for_max_iterations(self):
        """Agent hitting max_iterations emits run_failed."""
        from orchestrator.providers.fake import FakeProvider
        from orchestrator.context_meter import ContextMeter
        from orchestrator.memory import ConversationMemory
        from orchestrator.interaction_policy import InteractionPolicy
        from orchestrator.agent import KimAgent

        # Provider that always returns empty text (no TASK_COMPLETE → loop until max)
        fake = FakeProvider(responses=[
            {"type": "text", "content": "thinking…"},
        ])
        agent = object.__new__(KimAgent)
        agent.config = {"max_iterations": 2, "screenshot_scale": 0.75,
                        "memory_max_messages": 40, "memory_keep_screenshots": 4,
                        "context_budget_tokens": 100_000}
        agent.provider = fake
        agent.max_iterations = 2
        agent.screenshot_scale = 0.75
        agent.memory = ConversationMemory(max_messages=40, keep_screenshots=4)
        agent._ui_bridge = None
        agent._session_store = MagicMock()
        agent._resume_session_id = None
        agent._screenshot_hashes = []
        agent._recent_action_sigs = []
        agent._tools = [{"name": "take_screenshot", "description": "s", "parameters": {}}]
        agent._total_tokens = {"input": 0, "output": 0}
        agent._last_plan_signature = ""
        agent._last_step_signature = ""
        agent._last_done_signature = ""
        agent._current_plan_steps = []
        agent._current_step_index = 0
        agent._clear_chat_on_next_call = False
        agent._max_retries = 1
        agent._retry_base_delay = 0.01
        agent._retry_max_delay = 0.01
        agent._hitl_risk_threshold = None
        agent._interaction_policy = InteractionPolicy(block_high_risk=False)
        agent._context_meter = ContextMeter(budget=100_000)

        list_result = MagicMock()
        list_result.tools = [_make_mcp_tool("take_screenshot")]
        session = AsyncMock()
        session.list_tools = AsyncMock(return_value=list_result)
        agent.session = session

        buf = io.StringIO()
        with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
            with redirect_stdout(buf):
                result = asyncio.run(agent.run("loop forever"))

        assert not result.get("success"), "Expected failure"
        lines = buf.getvalue().splitlines()
        failed_lines = [ln for ln in lines if '"type":"run_failed"' in ln]
        assert failed_lines, f"Expected run_failed event, got lines: {lines}"
        evt = json.loads(failed_lines[0])
        assert "reason" in evt
        assert "recoverable" in evt
        assert "suggestion" in evt


class TestProviderErrorEmission:
    """provider_error event is emitted when all retries are exhausted."""

    def test_provider_error_emitted_on_failure(self):
        """Provider raising immediately emits provider_error + run_failed."""
        from orchestrator.context_meter import ContextMeter
        from orchestrator.memory import ConversationMemory
        from orchestrator.interaction_policy import InteractionPolicy
        from orchestrator.agent import KimAgent
        from orchestrator.providers.base import BaseProvider

        class AlwaysFailProvider(BaseProvider):
            async def complete(self, messages, tools, system):
                raise PermissionError("bad api key")

        agent = object.__new__(KimAgent)
        agent.config = {"max_iterations": 5, "screenshot_scale": 0.75,
                        "memory_max_messages": 40, "memory_keep_screenshots": 4,
                        "context_budget_tokens": 100_000}
        agent.provider = AlwaysFailProvider()
        agent.max_iterations = 5
        agent.screenshot_scale = 0.75
        agent.memory = ConversationMemory(max_messages=40, keep_screenshots=4)
        agent._ui_bridge = None
        agent._session_store = MagicMock()
        agent._resume_session_id = None
        agent._screenshot_hashes = []
        agent._recent_action_sigs = []
        agent._tools = [{"name": "take_screenshot", "description": "s", "parameters": {}}]
        agent._total_tokens = {"input": 0, "output": 0}
        agent._last_plan_signature = ""
        agent._last_step_signature = ""
        agent._last_done_signature = ""
        agent._current_plan_steps = []
        agent._current_step_index = 0
        agent._clear_chat_on_next_call = False
        agent._max_retries = 1
        agent._retry_base_delay = 0.01
        agent._retry_max_delay = 0.01
        agent._hitl_risk_threshold = None
        agent._interaction_policy = InteractionPolicy(block_high_risk=False)
        agent._context_meter = ContextMeter(budget=100_000)

        list_result = MagicMock()
        list_result.tools = [_make_mcp_tool("take_screenshot")]
        session = AsyncMock()
        session.list_tools = AsyncMock(return_value=list_result)
        agent.session = session

        buf = io.StringIO()
        with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
            with redirect_stdout(buf):
                asyncio.run(agent.run("fail task"))

        lines = buf.getvalue().splitlines()
        error_lines = [ln for ln in lines if '"type":"provider_error"' in ln]
        assert error_lines, f"Expected provider_error event, lines: {lines}"
        evt = json.loads(error_lines[0])
        assert evt["code"] == "auth"
        assert evt["retryable"] is False

"""
Prompt-render invariant tests.

Every system-prompt f-string template must render without KeyError for any
task string. A stray unescaped `{var}` in a prompt becomes a runtime failure
mid-task, far from the source. These tests catch it at commit time.

Heavy modules (mcp, mss, pynput, …) are stubbed because the test environment
doesn't install them. Only orchestrator.agent is exercised.
"""
from __future__ import annotations

import re
import sys
import types
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Stubs for heavy runtime deps not installed in the test venv
# ---------------------------------------------------------------------------

def _stub(name: str, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


for _mod in (
    "mss", "pynput", "pynput.mouse", "pynput.keyboard",
    "pyautogui", "PIL", "PIL.Image", "sounddevice", "soundfile",
    "kokoro", "pygetwindow",
):
    if _mod not in sys.modules:
        sys.modules[_mod] = _stub(_mod)

# Only stub mcp if the real package isn't already installed. If mcp is a real
# package (installed in the venv), use it — it satisfies orchestrator.agent's
# imports AND allows mcp_server.tool_registry to work in the same process.
try:
    import mcp as _real_mcp  # noqa: F401 — just check if it's importable
    import mcp.client.stdio as _real_mcp_stdio  # noqa: F401
except ImportError:
    # Real mcp not available — create minimal stubs
    for _mod in ("mcp", "mcp.client", "mcp.client.stdio"):
        if _mod not in sys.modules:
            sys.modules[_mod] = _stub(_mod)
    _mcp = sys.modules["mcp"]
    _mcp.ClientSession = MagicMock
    _mcp.StdioServerParameters = MagicMock
    sys.modules["mcp.client.stdio"].stdio_client = MagicMock()
else:
    # Real mcp is available; just ensure the specific names agent.py needs exist
    import mcp as _mcp_pkg
    if not hasattr(_mcp_pkg, "ClientSession"):
        _mcp_pkg.ClientSession = MagicMock
    if not hasattr(_mcp_pkg, "StdioServerParameters"):
        _mcp_pkg.StdioServerParameters = MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(lean: bool = False):
    """Build a minimal KimAgent-like object for prompt rendering tests."""
    from orchestrator.providers.fake import FakeProvider
    from conftest import make_test_agent
    provider = FakeProvider()
    if lean:
        provider.lean_system_prompt = True
    return make_test_agent(provider=provider)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPromptRender:
    """Every system-prompt template renders without KeyError for any input."""

    def test_standard_system_prompt_renders(self):
        agent = _make_agent(lean=False)
        prompt = agent._build_system_prompt("open Safari and take a screenshot")
        assert "Kim" in prompt
        assert "TASK_COMPLETE" in prompt

    def test_browser_new_chat_does_not_inject_other_session_summaries(self):
        from orchestrator.providers.fake import FakeProvider
        from conftest import make_test_agent

        BrowserProvider = type("BrowserProvider", (FakeProvider,), {})
        agent = make_test_agent(provider=BrowserProvider())
        summaries = [{"date": "2026-07-07", "summary": "secret old chat"}]
        with patch(
            "orchestrator.agent.SessionStore.recent_summaries",
            return_value=summaries,
        ) as recent_summaries:
            prompt = agent._build_system_prompt("brand new task")

        assert "secret old chat" not in prompt
        assert "# Recent context" not in prompt
        recent_summaries.assert_not_called()

    def test_non_browser_provider_keeps_recent_context_memory(self):
        agent = _make_agent(lean=False)
        summaries = [{"date": "2026-07-07", "summary": "useful old task"}]
        with patch(
            "orchestrator.agent.SessionStore.recent_summaries",
            return_value=summaries,
        ):
            prompt = agent._build_system_prompt("new API task")

        assert "# Recent context" in prompt
        assert "useful old task" in prompt

    def test_lean_system_prompt_renders(self):
        agent = _make_agent(lean=True)
        prompt = agent._build_system_prompt("list all open windows")
        assert "Kim" in prompt
        assert "TASK_COMPLETE" in prompt

    def test_prompt_contains_task(self):
        agent = _make_agent(lean=False)
        task = "take a screenshot of my desktop"
        prompt = agent._build_system_prompt(task)
        assert task in prompt

    def test_prompt_has_no_unescaped_template_vars(self):
        """Detect leftover `{word}` template vars that would blow up as f-string errors."""
        agent = _make_agent(lean=False)
        prompt = agent._build_system_prompt("do something")
        # After rendering, bare {word} patterns that look like template vars (not JSON)
        # indicate an accidentally unescaped brace.
        suspicious = re.findall(r'(?<!\{)\{(?!\{)([^{}]{1,30})\}(?!\})', prompt)
        template_vars = [m for m in suspicious if re.match(r'^[a-zA-Z_][a-zA-Z_0-9]*$', m)]
        assert template_vars == [], (
            f"Possible unescaped template vars in system prompt: {template_vars}\n"
            "Escape with {{ }} in f-strings — see orchestrator/CLAUDE.md"
        )

    def test_standard_prompt_renders_with_special_chars_in_task(self):
        """Tasks with braces or special chars must not break prompt rendering."""
        agent = _make_agent(lean=False)
        # A task containing braces should not crash the f-string
        prompt = agent._build_system_prompt("find {config} file and read it")
        assert "TASK_COMPLETE" in prompt

    def test_lean_prompt_renders_with_special_chars_in_task(self):
        agent = _make_agent(lean=True)
        prompt = agent._build_system_prompt("search for {pattern} in ~/Documents")
        assert "TASK_COMPLETE" in prompt

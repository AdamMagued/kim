"""
Static contract tests for desktop Chat/Code session isolation.

The desktop React app does not currently have a JS unit-test runner wired into
the Python test matrix, so these tests guard the high-risk state transitions by
checking the relevant App/ChatView source contracts directly.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "desktop" / "src" / "App.tsx"
CHAT_VIEW_PATH = ROOT / "desktop" / "src" / "components" / "ChatView.tsx"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_tab_switch_clears_pending_chat_selection():
    """A Chat-tab pending selection must not fire after the user moves to Code.

    Regression: a completed Chat run left `pendingSelectSessionId` set. A later
    Code task refreshed sessions, the pending Chat selection resolved, and the
    UI snapped back to the previous Chat conversation.
    """
    src = _read(APP_PATH)
    match = re.search(r"function\s+handleTabChange\s*\([^)]*\)\s*\{(?P<body>.*?)\n\s*\}", src, re.DOTALL)
    assert match, "handleTabChange not found"
    assert "setPendingSelectSessionId(null)" in match.group("body")


def test_pending_selection_effect_is_chat_tab_only():
    src = _read(APP_PATH)
    assert "if (activeTab !== 'chat')" in src
    assert "setPendingSelectSessionId(null)" in src
    assert re.search(
        r"useEffect\(\(\)\s*=>\s*\{[\s\S]*pendingSelectSessionId[\s\S]*activeTab\s*!==\s*'chat'[\s\S]*\},\s*\[kimSessions,\s*pendingSelectSessionId,\s*activeTab\]\)",
        src,
    ), "pendingSelectSessionId effect must depend on activeTab and clear outside Chat"


def test_code_completion_refuses_cross_project_session_switch():
    src = _read(APP_PATH)
    required = [
        "activeTab === 'code'",
        "completedSession.session_type === 'codex'",
        "completedSession.project_path !== activeProjectPath",
        "return;",
    ]
    for snippet in required:
        assert snippet in src, f"missing Code completion guard snippet: {snippet}"


def test_empty_state_waits_for_live_history_and_activity():
    """The Code chat must keep showing the live run while disk reload catches up."""
    src = _read(CHAT_VIEW_PATH)
    assert "messages.length === 0 && liveHistory.length === 0 && activity.length === 0" in src


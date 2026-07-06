"""
Static contract tests for desktop Chat/Code session isolation.

These guard high-risk App/ChatView state transitions by matching the exact
guard blocks with ANCHORED, contiguous regexes.  Earlier versions asserted
that snippets like ``"activeTab === 'code'"`` and ``"return;"`` appeared
*anywhere* in App.tsx — a mutation test showed the guard could be disabled
(``if (false && activeTab === 'code' ...)``) with every test still green,
because the substrings were satisfied by unrelated code.

The regexes below require the full condition sequence to open the ``if (``
directly (no ``false &&`` prefix) and the ``return;`` to live inside that
same block, so that specific mutation class now fails.

NOTE: the durable fix is porting these to Vitest component tests (the desktop
test runner exists and runs in CI); tracked as follow-up.
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
    body = match.group("body")
    assert "setPendingSelectSessionId(null)" in body
    # The clear must not be guarded by a constant-false condition.
    assert not re.search(r"if\s*\(\s*false\b[^)]*\)\s*\{[^}]*setPendingSelectSessionId\(null\)", body)


def test_pending_selection_effect_is_chat_tab_only():
    src = _read(APP_PATH)
    assert re.search(
        r"useEffect\(\(\)\s*=>\s*\{[\s\S]*?"
        r"if\s*\(\s*activeTab\s*!==\s*'chat'\s*\)\s*\{[\s\S]*?setPendingSelectSessionId\(null\)[\s\S]*?\}"
        r"[\s\S]*?\},\s*\[kimSessions,\s*pendingSelectSessionId,\s*activeTab\]\)",
        src,
    ), "pendingSelectSessionId effect must depend on activeTab and clear outside Chat"


def test_code_completion_refuses_cross_project_session_switch():
    """The full guard condition must open the if( directly and return inside
    the same block — a `false &&` prefix or a removed return must fail here."""
    src = _read(APP_PATH)
    guard = re.search(
        r"if\s*\(\s*"
        r"activeTab === 'code' &&\s*"
        r"completedSession\.session_type === 'codex' &&\s*"
        r"completedSession\.project_path !== activeProjectPath\s*"
        r"\)\s*\{(?P<body>[^}]*)\}",
        src,
    )
    assert guard, (
        "cross-project Code completion guard not found (or its condition was "
        "altered/disabled) in App.tsx handleTaskDone"
    )
    assert "return;" in guard.group("body"), (
        "cross-project guard no longer early-returns — a foreign-project Codex "
        "completion would replace the current chat"
    )


def test_empty_state_waits_for_live_history_and_activity():
    """The Code chat must keep showing the live run while disk reload catches up."""
    src = _read(CHAT_VIEW_PATH)
    match = re.search(
        r"(?P<prefix>.{20})messages\.length === 0 && liveHistory\.length === 0 && activity\.length === 0",
        src,
        re.DOTALL,
    )
    assert match, "empty-state condition not found in ChatView.tsx"
    # Guard must not be short-circuited into dead code.
    assert "false &&" not in match.group("prefix"), (
        "empty-state condition is disabled by a constant-false prefix"
    )

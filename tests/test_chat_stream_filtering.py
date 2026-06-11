"""
Contract tests for the stream-filtering rules shared between the orchestrator
(producer) and the desktop frontend (consumer).

The frontend lives in desktop/src/components/chat/utils.ts but its
HIDDEN_SUBSTRINGS / HIDDEN_REGEX lists are simple enough that we can pull
them in via plain text scraping and assert the producer-side rules don't
emit lines that would slip past them.

These tests prevent two specific regressions that already happened:

1. `InteractionPolicy` used to log a multi-line WARN payload that included
   internal `Last observe_ui generation: 0; known UI IDs: 0; last UI observe
   dirty: False.` text. The middle lines fell under 80 chars and didn't match
   any filter, so they rendered as raw "thoughts" in the Thinking panel.

2. External tools (codex/claw) inheriting stdin from a TTY occasionally
   printed `receiving additional input from stdin` to stderr. That line
   leaked into chat output as a status item.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from orchestrator.interaction_policy import InteractionPolicy

ROOT = Path(__file__).resolve().parents[1]
UTILS_PATH = ROOT / "desktop" / "src" / "components" / "chat" / "utils.ts"


def _load_utils_source() -> str:
    if not UTILS_PATH.exists():
        pytest.skip(f"frontend utils.ts not present at {UTILS_PATH}")
    return UTILS_PATH.read_text(encoding="utf-8")


def _hidden_substrings() -> list[str]:
    src = _load_utils_source()
    match = re.search(
        r"export const HIDDEN_SUBSTRINGS\s*=\s*\[(.*?)\];",
        src,
        re.DOTALL,
    )
    assert match, "HIDDEN_SUBSTRINGS not found in chat/utils.ts"
    body = "\n".join(line.split("//", 1)[0] for line in match.group(1).splitlines())
    # Pull all 'literal' / "literal" entries. Use a tiny scanner instead of a
    # regex so double-quoted strings can contain single quotes and vice versa
    # (for example "title='" and 'title="').
    entries: list[str] = []
    i = 0
    while i < len(body):
        quote = body[i]
        if quote not in {"'", '"'}:
            i += 1
            continue
        j = i + 1
        chars: list[str] = []
        while j < len(body):
            ch = body[j]
            if ch == "\\" and j + 1 < len(body):
                chars.append(body[j + 1])
                j += 2
                continue
            if ch == quote:
                break
            chars.append(ch)
            j += 1
        if j < len(body) and body[j] == quote:
            entries.append("".join(chars))
            i = j + 1
        else:
            i += 1
    return entries


def _hidden_regex_patterns() -> list[re.Pattern[str]]:
    src = _load_utils_source()
    match = re.search(
        r"export const HIDDEN_REGEX:\s*RegExp\[\]\s*=\s*\[(.*?)\];",
        src,
        re.DOTALL,
    )
    assert match, "HIDDEN_REGEX not found in chat/utils.ts"
    body = match.group(1)
    patterns: list[re.Pattern[str]] = []
    # Lines like   /pattern/flags,   (multi-line entries supported).
    for raw in re.finditer(r"^\s*/((?:\\.|[^/\n])+)/([gimsuy]*)\s*,?\s*(?://.*)?$", body, re.MULTILINE):
        body_str, flags = raw.group(1), raw.group(2)
        py_flags = 0
        if "i" in flags:
            py_flags |= re.IGNORECASE
        try:
            patterns.append(re.compile(body_str, py_flags))
        except re.error:
            # JavaScript regex with constructs Python can't compile — skip.
            continue
    return patterns


def _line_is_hidden(line: str) -> bool:
    """Mirror of `isNoiseLine` in chat/utils.ts for the producer-side check.

    The TS implementation lowercases the line for substring comparison and
    matches regex patterns as-is. This helper exists so we can prove that the
    Python logger output (the producer side) actually filters out under the
    real frontend rules.
    """
    if "[STATUS]" in line or "[ANSWER]" in line:
        return False
    if "[SUCCESS]" in line or "[FAILED]" in line or "[ERROR]" in line:
        return False
    if line.startswith("[TOOL]"):
        return False
    payload = line[len("[err] "):].lstrip() if line.startswith("[err]") else line
    lower = payload.lower()
    for sub in _hidden_substrings():
        if sub.lower() in lower:
            return True
    for pattern in _hidden_regex_patterns():
        if pattern.search(payload):
            return True
    return False


# ── Bug 1: observe_ui diagnostics must not leak into chat reasoning ───────


def test_policy_decision_message_is_single_line():
    """`InteractionPolicy._message` must return a single line so that
    streaming each log line to the UI never breaks the payload into multiple
    "thought" items.
    """
    policy = InteractionPolicy()
    decision = policy.before_tool("web_click", {"element_id": "w1"})
    assert decision.message, "policy must return a blocking message"
    assert "\n" not in decision.message, decision.message


def test_policy_decision_message_has_no_internal_counters():
    """The previous payload exposed internal counter state to the LLM and the
    user. Counters now live only on the policy instance.
    """
    policy = InteractionPolicy()
    decision = policy.before_tool("web_click", {"element_id": "w1"})
    forbidden = [
        "Last observe_ui generation:",
        "Last web_observe generation:",
        "known UI IDs:",
        "known web IDs:",
        "last UI observe dirty:",
        "last observe dirty:",
    ]
    for snippet in forbidden:
        assert snippet not in decision.message, (
            f"Internal counter '{snippet}' leaked into policy message: "
            f"{decision.message!r}"
        )


def test_hidden_substrings_cover_legacy_policy_diagnostics():
    """If any old observe_ui diagnostic format ever leaks through (e.g. via
    a third-party logger that wraps our format), the frontend must still
    silently drop it.
    """
    samples = [
        "Last observe_ui generation: 0; known UI IDs: 0; last UI observe dirty: False.",
        "Last web_observe generation: 2; known web IDs: 4; last observe dirty: True.",
        "Suggested next action: Call web_observe again before resolving another element.",
        "POLICY_WARNING: Some reason",
        "POLICY_BLOCK: Some reason",
    ]
    for sample in samples:
        assert _line_is_hidden(sample), (
            f"Expected frontend to hide diagnostic line, but it slipped through: {sample!r}"
        )


def test_hidden_substrings_do_not_swallow_legit_user_text():
    """Ensure the new substring filters don't accidentally hide normal LLM
    text. A status line like "I'll now open the URL" must NOT be hidden.
    """
    legit = [
        "[STATUS] Reading config.yaml",
        "[STATUS] Kim is thinking…",
        "[STATUS] I will now read the file",
        "[SUCCESS] Task completed",
        "[TOOL] write_file({\"path\":\"a.py\"})",
        "[ANSWER] Hello there",
    ]
    for line in legit:
        assert not _line_is_hidden(line), (
            f"Legitimate status/answer line should not be filtered: {line!r}"
        )


# ── Bug 2: external tool stdin chatter must not leak ─────────────────────


def test_hidden_substrings_cover_stdin_chatter():
    samples = [
        "[err] receiving additional input from stdin",
        "[err] reading additional input from stdin",
        "[err] reading from stdin",
        "[err] waiting for stdin",
    ]
    for sample in samples:
        assert _line_is_hidden(sample), (
            f"Frontend should hide stdin-chatter line: {sample!r}"
        )


# ── Bug 1b: take_screenshot label must render as a tool, not a thought ───


def test_capturing_screen_label_is_recognized_as_tool_verb():
    """`take_screenshot` returns the label "Capturing screen". The frontend's
    parseToolVerb regex must include "Capturing" so the activity item is
    rendered as a tool event in the Thinking panel rather than falling back
    to a raw "thought" row that looks like leaked debug output to the user.

    We grep for the literal alternation list rather than parsing the full
    function — the regex inside parseToolVerb is the canonical source of
    truth for valid tool verbs.

    Note: parseToolVerb lives in chat/parsers.ts (extracted from ChatView.tsx
    during the Phase 3 restructure). The test target was updated accordingly.
    """
    chatview_path = ROOT / "desktop" / "src" / "components" / "chat" / "parsers.ts"
    if not chatview_path.exists():
        pytest.skip(f"parsers.ts not present at {chatview_path}")
    src = chatview_path.read_text(encoding="utf-8")
    # The regex literal looks like:
    #   /^(Reading|Writing|Running|...|Capturing|...)\s*(.*)/i
    # Find any line that contains that alternation block.
    match = re.search(
        r"/\^\((Reading\|[^)]+)\)\\s\*\(\.\*\)/i",
        src,
    )
    assert match, "parseToolVerb regex (starting with /^(Reading|...) not found in ChatView.tsx"
    verbs = match.group(1).split("|")
    assert "Capturing" in verbs, (
        f"parseToolVerb regex must include 'Capturing' for take_screenshot label. "
        f"Found verbs: {verbs}"
    )


# ── Bug 1c: backend startup diagnostics must not leak into UI ─────────────


def test_hidden_substrings_cover_backend_startup_diagnostics():
    """Lines like '✓ Using Codex CLI via Ollama via local daemon ...' and
    'Routing to Codex via ...' must be caught by HIDDEN_SUBSTRINGS if they
    ever leak through as non-[STATUS] lines.
    """
    samples = [
        "✓ Using Codex CLI via Ollama via local daemon (gpt-oss:120b-cloud): /opt/homebrew/bin/codex",
        "Routing to Codex via Kim's browser provider (gemini)",
        "Routing code task to Codex via OpenAI: /opt/homebrew/bin/codex",
        "/opt/homebrew/bin/codex",
    ]
    for sample in samples:
        assert _line_is_hidden(sample), (
            f"Frontend should hide backend startup diagnostic: {sample!r}"
        )


# ── Bug 2b: tool-result noise must be hidden ──────────────────────────────


def test_hidden_substrings_cover_ui_tool_results():
    """Raw tool-result text like 'No interactive controls at this depth...'
    and window title strings should be caught by HIDDEN_SUBSTRINGS.
    """
    samples = [
        "No interactive controls at this depth. Try a different window or depth.",
        "No interactive controls found",
        "title='Terminal - desktop - desktop * npm run t'",
        'title="Terminal - desktop"',
    ]
    for sample in samples:
        assert _line_is_hidden(sample), (
            f"Frontend should hide UI tool result text: {sample!r}"
        )


def test_tool_map_includes_get_windows_and_screen_info():
    """get_windows and get_screen_info must appear in TOOL_MAP so their
    tool events render with clean labels in the Thinking panel.
    """
    src = _load_utils_source()
    for tool_name in ["get_windows", "get_screen_info"]:
        assert re.search(rf"\b{tool_name}\b\s*:", src), (
            f"TOOL_MAP must include '{tool_name}' for clean Thinking display"
        )

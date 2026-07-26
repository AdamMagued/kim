"""Auto-compaction helpers for the Codex bridge proxy (claw-style two-pass).

Extracted from ``codex_engine/engine.py`` (Q6 file-size gate — engine.py is
already over the 800-line cap and may not grow). These are pure functions (no
proxy state, no I/O beyond the one LLM call in ``_summarize_messages``);
``_CodexProxy._apply_compaction``/``_apply_compaction_chat`` in engine.py call
them and own the compaction-cache bookkeeping themselves.

When estimated token count exceeds the provider threshold, the proxy
summarizes older messages via the browser LLM (first pass), then applies
priority-based line selection to keep the summary under a character budget
(second pass — adapted from claw's summary_compression.rs).
"""

from __future__ import annotations

import contextlib
import logging
import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from orchestrator.providers.browser_provider import BrowserProvider

logger = logging.getLogger("kim.codex_bridge")

# Compaction constants (adapted from claw compact.rs)
COMPACT_KEEP_ITEMS = 20      # Keep last N items uncompressed
COMPACT_MIN_ITEMS_TO_REMOVE = 5  # Don't compact if fewer than this many items would be removed

# Per-provider context thresholds (tokens) before compaction triggers
_COMPACT_THRESHOLDS: dict[str, int] = {
    "claude": 180_000,
    "chatgpt": 100_000,
    "gemini": 800_000,
    "grok": 100_000,
    "deepseek": 60_000,
}
_DEFAULT_COMPACT_THRESHOLD = 100_000


def _estimate_tokens(items: list) -> int:
    """Rough token estimate: ~4 chars per token."""
    total = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        for field in ("content", "output", "arguments", "text"):
            val = item.get(field, "")
            if isinstance(val, str):
                total += len(val) // 4
            elif isinstance(val, list):
                for block in val:
                    if isinstance(block, dict):
                        total += len(block.get("text", "")) // 4
    return total


def _get_compact_threshold(provider_name: str) -> int:
    name = provider_name.lower()
    for key, threshold in _COMPACT_THRESHOLDS.items():
        if key in name:
            return threshold
    return _DEFAULT_COMPACT_THRESHOLD


def _is_compaction_summary(item: object) -> bool:
    """Return True if this item is a Kim compaction summary block."""
    if not isinstance(item, dict):
        return False
    content = item.get("content", "")
    if isinstance(content, str):
        return "[CONTEXT SUMMARY" in content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and "[CONTEXT SUMMARY" in block.get("text", ""):
                return True
    return False


def _fix_tool_boundary(items: list, keep_from: int) -> int:
    """Walk keep_from forward past any leading function_call_output items
    to avoid orphaning a tool result without its paired tool call."""
    idx = keep_from
    while idx < len(items):
        item = items[idx]
        if isinstance(item, dict) and item.get("type") == "function_call_output":
            idx += 1
        else:
            break
    return idx


def _compress_summary(summary_text: str, max_chars: int = 1200, max_lines: int = 24, max_line_chars: int = 160) -> str:
    """Second-pass compression (adapted from claw summary_compression.rs).

    Deduplicates, truncates long lines, and selects within a char/line budget
    using a priority ordering that keeps core fields over noise.
    """
    inner = re.sub(r"</?summary>", "", summary_text).strip()

    seen: set[str] = set()
    lines: list[str] = []
    for raw_line in inner.splitlines():
        normalized = " ".join(raw_line.split())[:max_line_chars]
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        lines.append(normalized)

    def _priority(line: str) -> int:
        lower = line.lower()
        if any(lower.startswith(k) for k in (
            "scope:", "current work:", "tools used:", "key files:",
            "pending:", "recent user requests:", "key decisions:",
        )):
            return 0
        if any(lower.startswith(k) for k in (
            "timeline:", "previously compacted:", "newly compacted:",
        )):
            return 1
        if line.startswith(("- ", "• ", "* ", "  -")):
            return 2
        return 3

    lines.sort(key=_priority)

    selected: list[str] = []
    total_chars = 0
    for line in lines:
        if len(selected) >= max_lines:
            break
        if total_chars + len(line) + 1 > max_chars:
            break
        selected.append(line)
        total_chars += len(line) + 1

    omitted = len(lines) - len(selected)
    body = "\n".join(selected)
    if omitted > 0:
        body += f"\n[{omitted} additional context lines omitted]"

    return f"<summary>\n{body}\n</summary>"


def _merge_compact_summaries(existing: str, new_summary: str) -> str:
    """Merge existing and new compaction summaries (claw merge_compact_summaries pattern)."""
    existing_inner = re.sub(r"</?summary>", "", existing).strip()
    new_inner = re.sub(r"</?summary>", "", new_summary).strip()
    return (
        f"<summary>\nPreviously compacted context:\n{existing_inner}\n\n"
        f"Newly compacted context:\n{new_inner}\n</summary>"
    )


@contextlib.asynccontextmanager
async def _preserved_browser_thread():
    """``extension_bridge.preserved_thread_state``, degrading to a no-op.

    codex_engine deliberately avoids importing orchestrator at module scope
    (see engine.py's module docstring), and the extension bridge is optional —
    a run without it must still compact.
    """
    try:
        from orchestrator.providers.browser.extension_bridge import preserved_thread_state
    except Exception:  # pragma: no cover - optional dependency path
        yield
        return
    async with preserved_thread_state():
        yield


async def _summarize_messages(items: list, provider: "BrowserProvider") -> str:
    """Call the browser LLM to produce a <summary> XML block for the given items."""
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role = item.get("role", "")
        itype = item.get("type", "")
        content = item.get("content", "")

        if itype == "function_call":
            name = item.get("name", "unknown")
            args = str(item.get("arguments", ""))[:400]
            parts.append(f"[TOOL CALL: {name}]\n{args}")
        elif itype == "function_call_output":
            output = item.get("output", "")
            if isinstance(output, list):
                output = " ".join(str(o) for o in output)
            parts.append(f"[TOOL RESULT]\n{str(output)[:400]}")
        elif role == "user":
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") in ("input_text", "text"):
                        parts.append(f"[USER]\n{block.get('text', '')[:800]}")
            elif isinstance(content, str) and not _is_compaction_summary(item):
                parts.append(f"[USER]\n{content[:800]}")
        elif role == "assistant":
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                        parts.append(f"[ASSISTANT]\n{block.get('text', '')[:800]}")
            elif isinstance(content, str):
                parts.append(f"[ASSISTANT]\n{content[:800]}")

    transcript = "\n\n".join(parts)

    prompt = (
        "Summarize the following coding agent conversation for context compaction. "
        "Output ONLY this XML block:\n\n"
        "<summary>\n"
        "Scope: [N tool calls, M user turns]\n"
        "Tools used: [comma-separated tool names]\n"
        "Recent user requests: [brief list]\n"
        "Key files touched: [files created/modified/read]\n"
        "Current work: [what was in progress at the end]\n"
        "Key decisions: [important findings or decisions]\n"
        "Timeline: [brief chronological summary]\n"
        "</summary>\n\n"
        f"TRANSCRIPT (truncated to 8000 chars):\n{transcript[:8000]}"
    )

    try:
        # clear_chat=True keeps the summary out of the user's chat, but on the
        # Chrome Extension bridge it also overwrote the live thread's
        # conversation/parent-message pointers with the summarizer's throwaway
        # thread — so every turn after a compaction silently continued in the
        # wrong chat. Snapshot and restore them around the side-call.
        async with _preserved_browser_thread():
            response = await provider.complete(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                system="You are a precise context summarizer. Output only the requested <summary>...</summary> XML block. No other text.",
                clear_chat=True,
            )
        raw = response.get("content", "") if isinstance(response, dict) else str(response)
        match = re.search(r"<summary>(.*?)</summary>", raw, re.DOTALL)
        if match:
            return f"<summary>{match.group(1)}</summary>"
        return f"<summary>{raw[:1000]}</summary>"
    except Exception as exc:
        logger.warning(f"Summarization LLM call failed: {exc}")
        return f"<summary>Context compacted. {len(items)} messages summarized.</summary>"

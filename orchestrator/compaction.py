"""Local compaction for Kim agent sessions.

Deterministic, no-LLM compaction logic for API-style providers (Ollama,
Claude, OpenAI, Codex, etc.) that are stateless — the full history is sent
each call, so we replace old messages with a compact summary + verbatim
recent tail.

Browser providers use a different (LLM-based) compact path in agent.py.
"""

from __future__ import annotations

import json
from typing import Any

# Single source of truth for token estimation (finding 4.4): compaction reuses
# context_meter's estimator so the compact threshold and the UI gauge agree.
from orchestrator.context_meter import IMAGE_TOKEN_ESTIMATE, estimate_content_tokens  # noqa: F401

COMPACT_PREAMBLE = (
    "This session is being continued from a previous conversation that ran out of "
    "context. The summary below covers the earlier portion of the conversation.\n\n"
)
COMPACT_RECENT_NOTE = "Recent messages are preserved verbatim."
COMPACT_RESUME_INSTRUCTION = (
    "Continue the conversation from where it left off without asking the user any "
    "further questions. Resume directly — do not acknowledge the summary, do not "
    "recap what was happening, and do not preface with continuation text."
)

PRESERVE_RECENT_MESSAGES = 6
MAX_ESTIMATED_TOKENS = 10_000
# Max chars of a tool-result body kept in the local summary timeline. Large
# enough to carry the useful part of a web_text/read_file/get_windows result,
# small enough that 20 timeline entries stay well under the compact threshold.
TOOL_RESULT_SNIPPET_CHARS = 500


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def should_compact(messages: list[dict], max_tokens: int = MAX_ESTIMATED_TOKENS) -> bool:
    """Return True if the message list exceeds the token threshold."""
    compactable = _compactable_messages(messages)
    total = sum(_estimate_message_tokens(m) for m in compactable)
    return total > max_tokens


def compact_messages(
    messages: list[dict],
    preserve_recent: int = PRESERVE_RECENT_MESSAGES,
    summary_text: str | None = None,
) -> list[dict]:
    """Return a compacted list: [system_summary_msg, ...recent_verbatim_msgs].

    The returned list always starts with a single system-role message
    containing the continuation summary, followed by the most recent
    ``preserve_recent`` messages verbatim.  Raises ValueError if there
    are no messages to compact.

    ``summary_text`` replaces the deterministic local summary when the caller
    already has a better one (e.g. the browser provider's in-thread compact,
    where the live thread's own model writes the handoff).
    """
    if not messages:
        raise ValueError("compact_messages: empty message list")

    # Separate any existing compact summary from the real history
    existing_summary, history = _split_existing_summary(messages)

    if len(history) <= preserve_recent:
        keep_from = 0
    else:
        keep_from = len(history) - preserve_recent

    # Walk back to avoid orphaning a tool result without its tool call
    keep_from = _fix_tool_boundary(history, keep_from)

    to_summarize = history[:keep_from]
    to_keep = history[keep_from:]

    new_summary = summary_text.strip() if summary_text and summary_text.strip() else _summarize_messages(to_summarize)
    if existing_summary:
        merged = _merge_summaries(existing_summary, new_summary)
    else:
        merged = new_summary

    summary_content = (
        COMPACT_PREAMBLE
        + "Summary:\n"
        + merged
        + "\n\n"
        + COMPACT_RECENT_NOTE
        + "\n\n"
        + COMPACT_RESUME_INSTRUCTION
    )

    summary_msg = {
        "role": "compact_summary",
        "content": summary_content,
    }
    return [summary_msg] + list(to_keep)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compactable_messages(messages: list[dict]) -> list[dict]:
    """Return only the real history (skip any leading compact_summary)."""
    _, history = _split_existing_summary(messages)
    return history


def _split_existing_summary(messages: list[dict]) -> tuple[str, list[dict]]:
    """Split off an existing compact summary at the head of the list."""
    if not messages:
        return "", []
    first = messages[0]
    if first.get("role") == "compact_summary":
        return str(first.get("content", "")), list(messages[1:])
    return "", list(messages)


def _estimate_message_tokens(msg: dict) -> int:
    # Single tokenizer: delegate to context_meter's estimator so should_compact
    # (10k threshold) and the UI gauge (200k budget) can never disagree about
    # what a message "costs" (finding 4.4).
    return max(1, estimate_content_tokens(msg.get("content", "")))


def _is_tool_result(msg: dict) -> bool:
    """True if msg is a user message that starts with '[Tool result:'.

    M4: multimodal tool results (screenshots) are LIST content of the shape
    [{"type": "text", "text": "[Tool result: …"}, {"type": "image", …}] —
    str(list) starts with "[{'type'…", so the old str() coercion never matched
    them and a compaction boundary could orphan a screenshot result from its
    tool call. Mirrors memory.py's list-aware check.
    """
    if msg.get("role") != "user":
        return False
    content = msg.get("content", "")
    if isinstance(content, str):
        return content.lstrip().startswith("[Tool result:")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                if str(item.get("text", "")).lstrip().startswith("[Tool result:"):
                    return True
    return False


def _is_tool_call(msg: dict) -> bool:
    """True if msg is an assistant message containing a JSON tool call."""
    if msg.get("role") != "assistant":
        return False
    content = msg.get("content", "")
    if isinstance(content, list):
        # Structured content: check if any item is a tool_call dict
        return any(
            isinstance(item, dict) and item.get("type") == "tool_call"
            for item in content
        )
    text = content if isinstance(content, str) else str(content)
    try:
        parsed = json.loads(text)
        return isinstance(parsed, dict) and parsed.get("type") == "tool_call"
    except (json.JSONDecodeError, ValueError):
        return False


def _fix_tool_boundary(history: list[dict], keep_from: int) -> int:
    """Walk keep_from back if the first preserved message is an orphaned tool result."""
    if keep_from <= 0 or keep_from >= len(history):
        return keep_from
    if not _is_tool_result(history[keep_from]):
        return keep_from
    # The first preserved message is a tool result — check if its paired tool
    # call is also preserved (it would be at keep_from - 1 if present).
    if keep_from > 0 and _is_tool_call(history[keep_from - 1]):
        keep_from -= 1  # include the tool call
    # If we walked back and now keep_from - 1 is still a tool result, keep going
    while keep_from > 0 and _is_tool_result(history[keep_from]):
        keep_from -= 1
    return max(0, keep_from)


def _summarize_messages(messages: list[dict]) -> str:
    """Build a structured local summary — no LLM call."""
    if not messages:
        return "(no prior context)"

    tools_used: list[str] = []
    user_requests: list[str] = []
    key_files: list[str] = []
    timeline: list[str] = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        text = content if isinstance(content, str) else _content_to_text(content)
        text = text.strip()
        if not text:
            continue

        if role == "user":
            if text.startswith("[Tool result:"):
                # Keep a content-bearing snippet of the BODY, not just the
                # "[Tool result: <tool>]" header line — the body often carries
                # the answer the rest of the run depends on (web_text output,
                # file contents, window lists). Finding 4.3.
                flattened = " ".join(text.split())
                if len(flattened) > TOOL_RESULT_SNIPPET_CHARS:
                    flattened = flattened[:TOOL_RESULT_SNIPPET_CHARS] + " …[truncated]"
                timeline.append(f"Tool result: {flattened}")
            else:
                snippet = text[:200].replace("\n", " ")
                user_requests.append(snippet)
        elif role == "assistant":
            if _is_tool_call(msg):
                try:
                    parsed = json.loads(text)
                    tool_name = parsed.get("tool", "unknown")
                    args = parsed.get("args", {})
                    if tool_name not in tools_used:
                        tools_used.append(tool_name)
                    # Extract file paths from args
                    for v in args.values():
                        if isinstance(v, str) and ("/" in v or "\\" in v or v.endswith(
                            (".py", ".ts", ".tsx", ".js", ".rs", ".toml", ".json", ".md")
                        )):
                            if v not in key_files:
                                key_files.append(v)
                    timeline.append(f"Tool call: {tool_name}({_args_summary(args)})")
                except (json.JSONDecodeError, ValueError):
                    pass
            else:
                snippet = text[:300].replace("\n", " ")
                timeline.append(f"Assistant: {snippet}")

    parts: list[str] = []

    if user_requests:
        parts.append("## User Requests\n" + "\n".join(f"- {r}" for r in user_requests[-5:]))

    if tools_used:
        parts.append("## Tools Used\n" + ", ".join(tools_used))

    if key_files:
        parts.append("## Key Files\n" + "\n".join(f"- {f}" for f in key_files[:10]))

    if timeline:
        parts.append("## Timeline\n" + "\n".join(f"- {e}" for e in timeline[-20:]))

    return "\n\n".join(parts) if parts else "(no summarizable content)"


def _merge_summaries(existing: str, new_summary: str) -> str:
    """Merge an existing compact summary with a new one."""
    return (
        "## Previously compacted context\n\n"
        + existing.strip()
        + "\n\n## Newly compacted context\n\n"
        + new_summary.strip()
    )


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") == "image":
                    parts.append("[image]")
                else:
                    parts.append(str(item))
            else:
                parts.append(str(item))
        return " ".join(parts)
    return str(content)


def _args_summary(args: dict) -> str:
    if not args:
        return ""
    items = []
    for k, v in list(args.items())[:3]:
        v_str = str(v)[:40]
        items.append(f"{k}={v_str!r}")
    return ", ".join(items)

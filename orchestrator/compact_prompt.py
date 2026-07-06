"""
Browser/LLM-based compaction prompt I/O helpers extracted from orchestrator/agent.py.

_build_compact_prompt serializes a transcript into the compact request prompt;
_parse_compact_json parses the model's JSON reply (strips ```json fences,
falls back to brace-extraction).
"""

import json
import re
from typing import Any


def _build_compact_prompt(messages: list[dict]) -> str:
    transcript = []
    for idx, msg in enumerate(messages, start=1):
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "image":
                    parts.append("[image omitted]")
                elif isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or item))
                else:
                    parts.append(str(item))
            content_text = "\n".join(parts)
        else:
            content_text = str(content)
        if len(content_text) > 3000:
            content_text = content_text[:1400] + "\n…[middle trimmed for compact prompt]…\n" + content_text[-1400:]
        transcript.append(f"[{idx}] {role}:\n{content_text}")

    return (
        "Compact this Kim Pro conversation into a durable handoff artifact. "
        "Preserve concrete decisions, user preferences, file paths, commands, "
        "provider/session details, errors, NEED_HELP outcomes, and open questions.\n\n"
        "Return ONLY valid JSON with this shape:\n"
        '{"summary":"...","decisions":["..."],"paths":["..."],'
        '"open_questions":["..."],"need_help":["..."],"next_steps":["..."]}\n\n'
        "Transcript:\n"
        + "\n\n---\n\n".join(transcript)
    )


def build_in_thread_compact_prompt() -> str:
    """Compact request for a live stateful browser thread.

    Unlike ``_build_compact_prompt`` this sends NO transcript — the thread
    already holds the full conversation, so the model summarizes what it can
    see above.  Used by the mid-run thread rollover in stateful mode.
    """
    return (
        "Compact our conversation above into a durable handoff artifact for a "
        "fresh chat that will continue this same work. Preserve the original "
        "task and its current status, concrete decisions, user preferences, "
        "file paths, commands, provider/session details, errors, and the exact "
        "next steps still pending.\n\n"
        "Return ONLY valid JSON with this shape:\n"
        '{"summary":"...","decisions":["..."],"paths":["..."],'
        '"open_questions":["..."],"need_help":["..."],"next_steps":["..."]}\n\n'
        "Do not call tools and do not wrap the JSON in markdown."
    )


def render_handoff_text(artifact: dict[str, Any]) -> str:
    """Flatten a compact artifact into the plain-text handoff block that seeds
    the first message of the replacement thread."""
    parts: list[str] = []
    summary = str(artifact.get("summary") or "").strip()
    if summary:
        parts.append(summary)
    for key, label in (
        ("decisions", "Decisions"),
        ("paths", "Key files/paths"),
        ("next_steps", "Next steps"),
        ("open_questions", "Open questions"),
        ("need_help", "Blockers"),
    ):
        items = artifact.get(key)
        if isinstance(items, list):
            lines = [str(i).strip() for i in items if str(i).strip()]
            if lines:
                parts.append(f"{label}:\n" + "\n".join(f"- {line}" for line in lines))
    return "\n\n".join(parts)


def _parse_compact_json(raw: str) -> dict[str, Any]:
    if not raw:
        return {"summary": "Conversation compacted, but the model returned an empty summary."}
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {"summary": cleaned[:8000]}

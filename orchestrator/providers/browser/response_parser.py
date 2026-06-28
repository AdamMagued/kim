"""
Response parsing for the browser provider.

Converts raw DOM-scraped text into the canonical provider response format:
  {"type": "tool_call", "tool": str, "args": dict}
  {"type": "text", "content": str}
"""

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


def strip_transport_markers(text: str, completion_hash: str) -> str:
    """Keep the newest response fragment and remove dynamic/legacy markers."""
    if not text:
        return ""

    if completion_hash and completion_hash in text:
        text = text.split(completion_hash, 1)[0]

    marker_re = r"\[END_OF_RESPONSE(?:_[A-Za-z0-9-]+)?\]"
    if re.search(marker_re, text, flags=re.IGNORECASE):
        parts = [part.strip() for part in re.split(marker_re, text, flags=re.IGNORECASE) if part.strip()]
        if parts:
            text = parts[-1]

    text = re.sub(marker_re, "", text, flags=re.IGNORECASE).strip()
    return text


def try_parse_tool_json(s: str) -> Optional[dict]:
    """Attempt to parse a JSON string as a tool call."""
    try:
        import json5
    except ImportError:
        json5 = None
    try:
        import json_repair
    except ImportError:
        json_repair = None

    data = None
    try:
        data = json.loads(s.strip())
    except json.JSONDecodeError:
        if json5:
            try:
                data = json5.loads(s.strip())
            except Exception:
                pass
        if data is None and json_repair:
            try:
                data = json_repair.loads(s.strip())
            except Exception:
                pass
        if data is None:
            return None

    if isinstance(data, dict) and "tool" in data:
        return {
            "type": "tool_call",
            "tool": data["tool"],
            "args": data.get("args", data.get("arguments", {})),
        }
    return None


def scan_for_json_match(text: str) -> Optional[tuple[dict, int, int]]:
    """Find the first balanced tool JSON object and return parsed + span."""
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    candidate = text[start: i + 1]
                    parsed = try_parse_tool_json(candidate)
                    if parsed:
                        return parsed, start, i + 1
                    start = -1
    return None


def _with_surrounding_content(parsed: dict, text: str, start: int, end: int) -> dict:
    """Attach non-JSON prose so PLAN/STEP markers are not lost."""
    before = text[:start].strip()
    after = text[end:].strip()
    content = "\n".join(part for part in (before, after) if part).strip()
    if content:
        parsed = dict(parsed)
        parsed["content"] = content
    return parsed


def parse_response(text: str, completion_hash: str) -> dict:
    """
    Parse the scraped DOM text into the canonical response format.
    Handles:
        - ``TASK_COMPLETE:`` / ``NEED_HELP:``  -> ``{"type": "text", ...}``
        - fenced ``json`` code blocks           -> ``{"type": "tool_call", ...}``
        - bare JSON ``{"tool": ...}``           -> ``{"type": "tool_call", ...}``
    """
    text = strip_transport_markers(text, completion_hash)
    text = re.sub(r'\bKIM_[a-f0-9]{8}\b', '', text).strip()
    text = re.sub(r'</?tool_call>', '', text).strip()

    for prefix in ("TASK_COMPLETE:", "NEED_HELP:"):
        # DOTALL (not MULTILINE): the answer after the marker can span multiple lines
        # (lists, code, paragraphs). MULTILINE's `(.+)$` stopped at the first line,
        # truncating multi-line browser answers to one line (e.g. a bullet list →
        # "Here are the colors:"). Capture everything after the marker to the end.
        m = re.search(r"\b" + re.escape(prefix) + r"\s*(.+)\Z", text, re.IGNORECASE | re.DOTALL)
        if m:
            return {"type": "text", "content": f"{prefix} {m.group(1).strip()}"}

    for pattern in [
        r"```(?:json)?\s*(\{.*?\})\s*```",
        r"`(\{[^`]+\})`",
    ]:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            parsed = try_parse_tool_json(m.group(1))
            if parsed:
                return _with_surrounding_content(parsed, text, *m.span(0))

    match = scan_for_json_match(text)
    if match:
        parsed, start, end = match
        return _with_surrounding_content(parsed, text, start, end)

    return {"type": "text", "content": text}

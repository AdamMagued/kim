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
    """Keep the response fragment for the CURRENT turn and remove dynamic/legacy markers.

    Anchors on the current turn's completion_hash sentinel rather than blindly
    taking the last split part, to avoid selecting a previous turn's fragment
    when a chat tab is reused (#40).
    """
    if not text:
        return ""

    # Remove everything after the current-turn completion hash.  The hash is
    # unique per-request so this anchors us to exactly this turn's response.
    # Anchor on the LAST occurrence (L6): a scraped DOM that echoes the user
    # Remove all END_OF_RESPONSE markers (with optional completion hash suffix)
    # without truncating trailing code blocks or text.
    marker_re = r"\[END_OF_RESPONSE(?:_[A-Za-z0-9-]+)?\]"
    text = re.sub(marker_re, "", text, flags=re.IGNORECASE).strip()

    # Strip ChatGPT Web internal widget/control blocks (e200-e203) & genui_run text
    text = re.sub(r'[\ue200-\ue203][^\ue200-\ue203]*[\ue200-\ue203]?', '', text)
    text = re.sub(r'genui_run result of\s*:?\s*', '', text).strip()
    return text


def try_parse_tool_json(s: str, known_tools: Optional[set] = None) -> Optional[dict]:
    """Attempt to parse a JSON string as a tool call.

    known_tools: if provided, the parsed tool name MUST be in this set to
    prevent scraped/echoed page content shaped like a tool call from triggering
    real tool dispatch (prompt-injection defence, #38).  Prefer fenced code
    blocks only; bare JSON in DOM text is only accepted when the tool name is
    in the registered set.
    """
    try:
        import json5  # type: ignore[import-not-found]
    except ImportError:
        json5 = None
    # json_repair is intentionally NOT used here: lenient parsing of arbitrary
    # DOM content increases the prompt-injection surface (#38/#45).  Clean JSON
    # only; json5 is retained only for code-fence content.

    data = None
    try:
        data = json.loads(s.strip())
    except json.JSONDecodeError:
        if json5:
            try:
                data = json5.loads(s.strip())
            except Exception:
                pass
        if data is None:
            return None

    if isinstance(data, dict) and "tool" in data:
        tool_name = data["tool"]
        # Reject tool calls that are not in the registered tool set — these are
        # almost certainly injected/echoed content, not real model output (#38).
        if known_tools is not None and tool_name not in known_tools:
            logger.debug("Ignoring JSON tool call for unknown tool %r (prompt-injection guard)", tool_name)
            return None
        # L3: {"tool": "x", "args": null} (or a string/list) must not leak a
        # non-dict into tool dispatch — downstream .get() calls would crash.
        args = data.get("args", data.get("arguments", {}))
        if not isinstance(args, dict):
            logger.debug("Coercing non-dict tool args %r to {} for tool %r", args, tool_name)
            args = {}
        return {
            "type": "tool_call",
            "tool": tool_name,
            "args": args,
        }
    return None


def scan_for_json_match(text: str, known_tools: Optional[set] = None) -> Optional[tuple[dict, int, int]]:
    """Find the first balanced tool JSON object in fenced code first, then bare text.

    When known_tools is provided, only returns a match if the tool name is in
    the registered set (prompt-injection defence, #38).
    """
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
                    parsed = try_parse_tool_json(candidate, known_tools=known_tools)
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


def strip_widget_jsons(text: str) -> str:
    """Strip raw ChatGPT web GenUI/search widget JSONs & GenUI control tokens from text."""
    text = re.sub(r'[\ue200-\ue203]', '', text)
    text = re.sub(r'\bgenui(?:_run)?\b\s*(?:result of\s*:?)?', '', text, flags=re.IGNORECASE)
    s = text.strip()
    if s.startswith('{') and ('"weather_' in s or '"genui_' in s or '"widget_' in s):
        depth = 0
        end_idx = -1
        for i, ch in enumerate(s):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end_idx = i
                    break
        if end_idx != -1:
            return s[end_idx + 1:].strip()
    return text.strip()


def parse_response(text: str, completion_hash: str, known_tools: Optional[set] = None) -> dict:
    """
    Parse the scraped DOM text into the canonical response format.
    Handles:
        - fenced ``json`` code blocks           -> ``{"type": "tool_call", ...}``
        - bare JSON ``{"tool": ...}``           -> ``{"type": "tool_call", ...}``
        - ``TASK_COMPLETE:`` / ``NEED_HELP:``  -> ``{"type": "text", ...}``

    Tool JSON is parsed FIRST so that a tool call followed by prose containing
    TASK_COMPLETE does not silently drop the tool call (#41).

    known_tools: when provided, bare-JSON tool names must be registered to
    prevent prompt-injection from scraped page content (#38).
    """
    text = strip_transport_markers(text, completion_hash)
    text = re.sub(r'\bKIM_[a-f0-9]{8}\b', '', text).strip()
    text = re.sub(r'</?tool_call>', '', text).strip()

    # ── 1. Fenced code block — highest confidence, parse first ────────────────
    for pattern in [
        r"```(?:json)?\s*(\{.*?\})\s*```",
        r"`(\{[^`]+\})`",
    ]:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            # In the browser provider the "model output" is DOM-scraped text;
            # a malicious page could inject a fenced JSON block with a fake tool
            # name. Apply the same known_tools guard as the bare-JSON path (#38).
            parsed = try_parse_tool_json(m.group(1), known_tools=known_tools)
            if parsed:
                return _with_surrounding_content(parsed, text, *m.span(0))

    # ── 2. Bare JSON tool call — require registered tool name (#38) ────────────
    match = scan_for_json_match(text, known_tools=known_tools)
    if match:
        parsed, start, end = match
        return _with_surrounding_content(parsed, text, start, end)

    # ── 3. Completion markers — checked after tool JSON (#41) ─────────────────
    for prefix in ("TASK_COMPLETE:", "NEED_HELP:"):
        m = re.search(r"\b" + re.escape(prefix) + r"\s*(.+)\Z", text, re.IGNORECASE | re.DOTALL)
        if m:
            body = m.group(1).strip()
            if prefix.upper().startswith("NEED_HELP"):
                # If NEED_HELP asks to run a shell command, convert it into a tool call
                cmd_m = re.search(r'`([^`]+)`|((?:pwd|rg|grep|find|ls|cat|git|npm|python|node|cd|make)\s+[^\n]+)', body)
                if cmd_m:
                    cmd = (cmd_m.group(1) or cmd_m.group(2)).strip()
                    logger.info("Auto-converting NEED_HELP command %r into exec_command tool call", cmd)
                    return {"type": "tool_call", "tool": "exec_command", "args": {"command": cmd}}
            return {"type": "text", "content": f"{prefix} {body}"}

    # Clean ChatGPT Web internal search/widget JSON blocks & cite tokens
    text = strip_widget_jsons(text)
    text = re.sub(r'cite\s*[A-Za-z0-9_]+', '', text)
    text = re.sub(r'turn\d+search\d+', '', text).strip()

    return {"type": "text", "content": text or "Response received."}

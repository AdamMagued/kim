"""
Tool name normalization and text-JSON tool call extraction.

Extracted from orchestrator/agent.py. Used by KimAgent to:
  - Normalize raw tool names from LLM responses (handles aliases, spacing, etc.)
  - Extract tool calls embedded as JSON in plain-text LLM responses
    (for models that cannot use native function calling)
"""

import json
import re
from typing import Any

_TOOL_NAME_ALIASES = {
    "screenshot": "take_screenshot",
    "screen_shot": "take_screenshot",
    "screen_capture": "take_screenshot",
    "take_screenshot": "take_screenshot",
    "take_screenshot_tool": "take_screenshot",
    "take_screenshots": "take_screenshot",
    "annotated_screenshot": "take_annotated_screenshot",
    "take_annotated_screenshot": "take_annotated_screenshot",
    "observe_ui": "observe_ui",
    "click_ui": "click_ui",
}


def normalize_tool_name(raw_name: Any) -> str:
    """Normalize a raw tool name string: lower-case, collapse whitespace/dashes,
    strip non-alphanumeric characters, and apply the alias map."""
    name = str(raw_name or "").strip().lower()
    if not name:
        return ""
    name = re.sub(r"[\s\-]+", "_", name)
    name = re.sub(r"[^a-z0-9_:]", "", name)
    return _TOOL_NAME_ALIASES.get(name, name)


# Keep private alias for agent.py internal usage (no rename needed externally)
_normalize_tool_name = normalize_tool_name


def extract_json_tool_call(content: str) -> dict | None:
    """Find and parse a text-JSON tool call of the form {"tool": "...", "args": {...}}.

    Some models (e.g. gpt-oss:20b) cannot use native tool_calls and instead emit
    the call as plain text JSON. This extracts the first valid call found so the
    agent can execute it rather than storing raw JSON in the chat history.
    Returns a dict with 'tool', 'args', 'start', 'end' keys, or None.
    """
    idx = content.find('"tool"')
    while idx != -1:
        start = content.rfind('{', 0, idx)
        if start == -1:
            break
        depth = 0
        for i in range(start, len(content)):
            if content[i] == '{':
                depth += 1
            elif content[i] == '}':
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(content[start:i + 1])
                        if isinstance(obj.get('tool'), str) and isinstance(obj.get('args'), dict):
                            return {'start': start, 'end': i + 1,
                                    'tool': obj['tool'], 'args': obj['args']}
                    except (json.JSONDecodeError, KeyError, ValueError):
                        pass
                    break
        idx = content.find('"tool"', idx + 1)
    return None


# Keep private alias for agent.py internal usage
_extract_json_tool_call = extract_json_tool_call

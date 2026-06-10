"""
Pure element-scoring helpers for web_observe / web_click / web_fill.

Extracted from mcp_server/tools/web.py. All functions here are stateless —
they take plain dicts/strings and return plain values. Nothing here touches
the module-level singletons (_element_data_map, _element_map, etc.).

Imported back into web.py as:
    from mcp_server.tools.web_element_scoring import (
        _GENERIC_INTENT_TOKENS, _ACTION_INTENT_TOKENS, _RESOLVE_THRESHOLDS,
        _norm, _tokens, _as_str_list, _strip_placeholder_prefix,
        _role_candidates, _infer_preferred_roles, _intent_focus,
        _important_tokens, _match_score, _best_match,
        _is_visible_element, _debug_label, _candidate_metadata,
        _scope_value, _searchable_text, _missing_strict_tokens,
    )
"""

from __future__ import annotations

import re
from typing import Any


_GENERIC_INTENT_TOKENS = {
    "a",
    "an",
    "and",
    "button",
    "field",
    "input",
    "textbox",
    "text",
    "box",
    "select",
    "dropdown",
    "control",
    "element",
    "the",
}

_ACTION_INTENT_TOKENS = {
    "click",
    "confirm",
    "continue",
    "create",
    "finish",
    "open",
    "press",
    "publish",
    "save",
    "send",
    "submit",
}

_RESOLVE_THRESHOLDS = {
    "loose": 0.20,
    "normal": 0.25,
    "strict": 0.58,
}

# Vocabulary bridges between how users describe a field and how sites label it.
# Used by _expand_with_synonyms to generate extra match needles — the original
# needle always scores first, so synonyms only widen recall, never override.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "name": ("title", "label"),
    "title": ("name", "subject"),
    "repo": ("repository",),
    "repository": ("repo",),
    "private": ("restricted",),
    "submit": ("create", "save", "send", "confirm", "done", "continue", "go"),
    "create": ("new", "add", "submit"),
    "email": ("e-mail", "mail", "address"),
    "username": ("user", "login", "handle"),
    "password": ("passphrase", "pass"),
    "search": ("find", "query", "lookup"),
    "description": ("summary", "details", "about", "bio"),
    "delete": ("remove", "trash", "discard"),
    "settings": ("preferences", "options", "configuration"),
    "visibility": ("access", "privacy"),
    "phone": ("telephone", "mobile", "tel"),
    "login": ("signin", "sign"),
    "message": ("body", "comment", "text"),
    "folder": ("directory",),
}


def _expand_with_synonyms(needles: list[str]) -> list[str]:
    """Return needles plus single-token synonym variants (originals first)."""
    expanded = list(needles)
    seen = {_norm(n) for n in needles}
    for needle in needles:
        toks = _tokens(needle)
        for i, tok in enumerate(toks):
            for syn in _SYNONYMS.get(tok, ()):
                variant = " ".join(toks[:i] + [syn] + toks[i + 1:])
                if variant and variant not in seen:
                    seen.add(variant)
                    expanded.append(variant)
    return expanded


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _tokens(value: Any) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", _norm(value)) if len(t) > 1]


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)]


def _strip_placeholder_prefix(value: Any) -> str:
    text = str(value or "").strip()
    if text.lower().startswith("placeholder:"):
        return text.split(":", 1)[1].strip()
    return text


def _role_candidates(el: dict[str, Any]) -> set[str]:
    role = _norm(el.get("role"))
    tag = _norm(el.get("tag"))
    typ = _norm(el.get("type"))
    candidates = {v for v in (role, tag, typ) if v}
    if tag == "textarea":
        candidates.add("textbox")
    if tag == "select":
        candidates.add("combobox")
        candidates.add("listbox")
    if tag == "button":
        candidates.add("button")
    if tag == "input":
        if typ in {"button", "submit", "reset"}:
            candidates.add("button")
        elif typ == "checkbox":
            candidates.add("checkbox")
        elif typ == "radio":
            candidates.add("radio")
        elif typ in {"email", "search", "tel", "text", "url", "password", "number"} or not typ:
            candidates.add("textbox")
            if typ:
                candidates.add(f"{typ}box")
    if role in {"searchbox", "textbox"}:
        candidates.add("textbox")
    if role == "link":
        candidates.add("a")
    return candidates


def _infer_preferred_roles(intent: str) -> list[str]:
    toks = set(_tokens(intent))
    roles: list[str] = []
    if toks & {"button", "submit", "create", "save", "send", "continue", "publish"}:
        roles.append("button")
    if toks & {"radio", "visibility", "public", "private"}:
        roles.append("radio")
    if toks & {"checkbox", "check"}:
        roles.append("checkbox")
    if toks & {
        "textbox",
        "input",
        "field",
        "email",
        "recipient",
        "name",
        "description",
        "search",
        "text",
    }:
        roles.append("textbox")
    return roles


def _intent_focus(intent: str) -> str:
    keep = [t for t in _tokens(intent) if t not in _GENERIC_INTENT_TOKENS]
    return " ".join(keep) or intent


def _important_tokens(intent: str) -> list[str]:
    return [
        t for t in _tokens(intent)
        if t not in _GENERIC_INTENT_TOKENS and t not in _ACTION_INTENT_TOKENS
    ]


def _match_score(needle: str, haystack: str) -> float:
    needle_norm = _norm(needle)
    hay_norm = _norm(haystack)
    if not needle_norm or not hay_norm:
        return 0.0
    if needle_norm == hay_norm:
        return 1.0
    if needle_norm in hay_norm:
        return 0.88
    hay_tokens = set(_tokens(hay_norm))
    needle_tokens = [t for t in _tokens(needle_norm) if t not in _GENERIC_INTENT_TOKENS]
    if not needle_tokens or not hay_tokens:
        return 0.0
    overlap = sum(1 for t in needle_tokens if t in hay_tokens)
    if overlap == len(needle_tokens):
        return 0.74
    if overlap:
        return 0.36 + (0.28 * overlap / max(len(needle_tokens), 1))
    compact_hay = hay_norm.replace("-", " ").replace("_", " ")
    return 0.55 if needle_norm.replace("-", " ").replace("_", " ") in compact_hay else 0.0


def _best_match(needles: list[str], haystacks: list[str]) -> tuple[float, str]:
    best = 0.0
    best_reason = ""
    for needle in needles:
        for hay in haystacks:
            score = _match_score(needle, hay)
            if score > best:
                best = score
                best_reason = f"{needle!r} matched {hay!r}"
    return best, best_reason


def _is_visible_element(el: dict[str, Any]) -> bool:
    if el.get("visible") is False:
        return False
    if el.get("hidden") is True:
        return False
    bbox = el.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        return False
    try:
        return float(bbox[2]) > 1 and float(bbox[3]) > 1
    except (TypeError, ValueError):
        return False


def _debug_label(el: dict[str, Any]) -> str:
    for key in ("label", "aria_label", "placeholder", "name", "text", "title", "value"):
        value = _strip_placeholder_prefix(el.get(key))
        if value:
            return str(value)
    return f"<{el.get('tag', '?')} role={el.get('role', '?')}>"


def _candidate_metadata(el: dict[str, Any], confidence: float, reason: str) -> dict[str, Any]:
    return {
        "element_id": el.get("id"),
        "confidence": round(confidence, 3),
        "reason": reason,
        "tag": el.get("tag", ""),
        "role": el.get("role", ""),
        "type": el.get("type", ""),
        "label": el.get("label", ""),
        "text": el.get("text", ""),
        "aria_label": el.get("aria_label", ""),
        "placeholder": el.get("placeholder", ""),
        "name": el.get("name", ""),
        "value": el.get("value", ""),
        "title": el.get("title", ""),
        "nearby_text": el.get("nearby_text", ""),
        "disabled": bool(el.get("disabled")),
        "required": bool(el.get("required")),
        "visible": _is_visible_element(el),
        "hidden": bool(el.get("hidden")),
        "in_viewport": bool(el.get("in_viewport")),
        "form_id": el.get("form_id", ""),
        "container_id": el.get("container_id", ""),
        "bbox": el.get("bbox"),
    }


def _scope_value(scope: Any, key: str) -> str:
    if not isinstance(scope, dict):
        return ""
    value = scope.get(key)
    return str(value or "").strip()


def _searchable_text(el: dict[str, Any]) -> str:
    return " ".join(
        str(el.get(k, ""))
        for k in (
            "label",
            "text",
            "aria_label",
            "placeholder",
            "name",
            "value",
            "title",
            "nearby_text",
            "container_text",
            "type",
        )
    )


def _missing_strict_tokens(el: dict[str, Any], intent: str) -> list[str]:
    hay_tokens = set(_tokens(_searchable_text(el)))
    return [token for token in _important_tokens(intent) if token not in hay_tokens]

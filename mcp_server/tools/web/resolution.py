"""Semantic intent -> element resolution.

``_resolve_element`` scores every element from the last observation against
a natural-language intent (role, label, placeholder, text and synonym
matches — the scoring primitives live in web_element_scoring). Also owns
scope constraints (same form / container / after-element) and the
element-id -> selector lookup used by the action tools.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from mcp_server.tools.web_element_scoring import (
    _RESOLVE_THRESHOLDS,
    _norm, _tokens, _as_str_list, _strip_placeholder_prefix,
    _role_candidates, _infer_preferred_roles, _intent_focus,
    _best_match, _is_visible_element, _debug_label, _candidate_metadata,
    _scope_value, _searchable_text, _missing_strict_tokens,
    _expand_with_synonyms,
)

from . import observation

logger = logging.getLogger(__name__)

def _scope_for_element(element_id: str | None) -> dict[str, Any]:
    if not element_id:
        return {}
    el = observation._element_data_map.get(str(element_id))
    if not el:
        return {}
    scope: dict[str, Any] = {"same_container_as": str(element_id)}
    if el.get("form_id"):
        scope["same_form_as"] = str(element_id)
        scope["form_id"] = el.get("form_id")
    if el.get("container_id"):
        scope["container_id"] = el.get("container_id")
    return scope


def _element_scope_matches(el: dict[str, Any], scope: Any) -> tuple[bool, str]:
    if not isinstance(scope, dict) or not scope:
        return True, ""

    same_form_as = _scope_value(scope, "same_form_as")
    if same_form_as:
        ref = observation._element_data_map.get(same_form_as)
        ref_form = str((ref or {}).get("form_id") or "")
        if not ref_form or str(el.get("form_id") or "") != ref_form:
            return False, f"outside form of {same_form_as}"

    form_id = _scope_value(scope, "form_id")
    if form_id and str(el.get("form_id") or "") != form_id:
        return False, f"outside form {form_id!r}"

    same_container_as = _scope_value(scope, "same_container_as")
    if same_container_as:
        ref = observation._element_data_map.get(same_container_as)
        ref_container = str((ref or {}).get("container_id") or "")
        if ref_container and str(el.get("container_id") or "") != ref_container:
            return False, f"outside container of {same_container_as}"

    container_id = _scope_value(scope, "container_id")
    if container_id and str(el.get("container_id") or "") != container_id:
        return False, f"outside container {container_id!r}"

    after_element = _scope_value(scope, "after_element")
    if after_element:
        ref = observation._element_data_map.get(after_element)
        ref_bbox = (ref or {}).get("bbox")
        bbox = el.get("bbox")
        try:
            if isinstance(ref_bbox, list) and isinstance(bbox, list) and float(bbox[1]) < float(ref_bbox[1]):
                return False, f"before {after_element}"
        except (TypeError, ValueError):
            pass

    return True, "scope matched"



def _is_global_nav_candidate(el: dict[str, Any]) -> bool:
    form_id = str(el.get("form_id") or "")
    container = _norm(el.get("container_id"))
    text = _norm(_searchable_text(el))
    if form_id:
        return False
    if any(part in container for part in ("header", "nav", "toolbar", "menu")):
        return True
    return bool(re.search(r"\b(create new|open menu|navigation|notifications|search or jump)\b", text))


def _resolve_element(
    intent: str,
    preferred_roles: list[str] | None = None,
    text_hints: list[str] | None = None,
    label_hints: list[str] | None = None,
    require_visible: bool = True,
    require_enabled: bool = False,
    elements: list[dict[str, Any]] | None = None,
    mode: str = "normal",
    scope: dict[str, Any] | None = None,
    require_text_evidence: bool = False,
    restrict_roles: bool = False,
) -> dict[str, Any]:
    """Resolve a semantic browser intent to the best known observed element."""
    intent = str(intent or "").strip()
    if elements is None:
        elements = list(observation._element_data_map.values())

    mode = _norm(mode) or "normal"
    if mode not in _RESOLVE_THRESHOLDS:
        mode = "normal"
    preferred_input = _as_str_list(preferred_roles) or _infer_preferred_roles(intent)
    preferred = [_norm(r) for r in preferred_input if _norm(r)]
    focus = _intent_focus(intent)
    label_needles = _expand_with_synonyms(_as_str_list(label_hints) or [focus])
    text_needles = _expand_with_synonyms(_as_str_list(text_hints) or [focus])
    intent_needles = _expand_with_synonyms([intent, focus])

    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    skipped = 0
    for el in elements:
        if require_visible and not _is_visible_element(el):
            skipped += 1
            rejected.append(_candidate_metadata(el, 0.0, "not visible"))
            continue
        if require_enabled and el.get("disabled"):
            skipped += 1
            rejected.append(_candidate_metadata(el, 0.0, "disabled"))
            continue
        scope_ok, scope_reason = _element_scope_matches(el, scope or {})
        if not scope_ok:
            skipped += 1
            rejected.append(_candidate_metadata(el, 0.0, scope_reason))
            continue

        roles = _role_candidates(el)
        score = 0.0
        reasons: list[str] = []

        if preferred:
            if roles.intersection(preferred):
                score += 0.24
                reasons.append(f"role matched {sorted(roles.intersection(preferred))[0]}")
            elif "input" in preferred and el.get("tag") == "input":
                score += 0.14
                reasons.append("tag matched input")
            else:
                if mode == "strict" or restrict_roles:
                    skipped += 1
                    rejected.append(_candidate_metadata(el, 0.0, f"role {sorted(roles)} did not match {preferred}"))
                    continue
                score -= 0.08

        if mode == "strict":
            missing = _missing_strict_tokens(el, intent)
            if missing:
                skipped += 1
                rejected.append(_candidate_metadata(el, 0.0, f"missing strict token(s): {', '.join(missing)}"))
                continue
            if _is_global_nav_candidate(el):
                skipped += 1
                rejected.append(_candidate_metadata(el, 0.0, "global navigation/menu candidate"))
                continue

        label_score, label_reason = _best_match(
            label_needles,
            [
                _strip_placeholder_prefix(el.get("label")),
                el.get("aria_label", ""),
                el.get("name", ""),
                el.get("title", ""),
            ],
        )
        if label_score:
            score += 0.42 * label_score
            reasons.append(f"label {label_reason}")

        placeholder_score, placeholder_reason = _best_match(label_needles, [el.get("placeholder", "")])
        if placeholder_score:
            score += 0.28 * placeholder_score
            reasons.append(f"placeholder {placeholder_reason}")

        text_score, text_reason = _best_match(
            text_needles,
            [el.get("text", ""), el.get("value", ""), el.get("nearby_text", "")],
        )
        if text_score:
            score += 0.30 * text_score
            reasons.append(f"text {text_reason}")

        intent_score, intent_reason = _best_match(
            intent_needles,
            [
                _strip_placeholder_prefix(el.get("label")),
                el.get("aria_label", ""),
                el.get("placeholder", ""),
                el.get("name", ""),
                el.get("text", ""),
                el.get("nearby_text", ""),
            ],
        )
        if intent_score:
            score += 0.22 * intent_score
            reasons.append(f"intent {intent_reason}")

        # Role/visibility bonuses alone can clear the loose/normal thresholds,
        # which lets a junk intent resolve to a random field. Callers that act
        # on the result (web_fill_form) demand at least one textual match.
        if require_text_evidence and not (label_score or placeholder_score or text_score or intent_score):
            skipped += 1
            rejected.append(_candidate_metadata(el, 0.0, "no text evidence for intent"))
            continue

        if _is_visible_element(el):
            score += 0.04
        else:
            score -= 0.20
            reasons.append("not visible")

        if el.get("in_viewport"):
            score += 0.02
        elif _is_visible_element(el):
            reasons.append("offscreen")

        if el.get("disabled"):
            score -= 0.18
            reasons.append("disabled")
        else:
            score += 0.03

        if el.get("required") and "required" in _tokens(intent):
            score += 0.04
            reasons.append("required")

        if scope_reason:
            score += 0.04
            reasons.append(scope_reason)

        confidence = max(0.0, min(score, 0.99))
        candidates.append(_candidate_metadata(el, confidence, "; ".join(reasons) or "weak metadata match"))

    candidates.sort(key=lambda c: c["confidence"], reverse=True)
    best = candidates[0] if candidates else None
    ok = bool(best and best["confidence"] >= _RESOLVE_THRESHOLDS[mode])
    if best:
        reason = best["reason"]
        element_id = str(best.get("element_id") or "")
        confidence = float(best["confidence"])
    else:
        reason = "No observed element matched the resolver constraints."
        element_id = ""
        confidence = 0.0

    if rejected:
        logger.info(
            "web_resolve rejected intent=%r mode=%s rejected=%s",
            intent,
            mode,
            rejected[:5],
        )
    return {
        "ok": ok,
        "element_id": element_id or None,
        "confidence": round(confidence, 3),
        "reason": reason,
        "intent": intent,
        "preferred_roles": preferred,
        "require_visible": require_visible,
        "require_enabled": require_enabled,
        "mode": mode,
        "scope": scope or {},
        "candidate_count": len(candidates),
        "skipped_count": skipped,
        "candidates": candidates[:8],
        "observation_generation": observation._observe_generation,
        "url": (observation._last_observation or {}).get("url", ""),
        "title": (observation._last_observation or {}).get("title", ""),
    }


async def handle_web_resolve(args: dict) -> str:
    intent = str(args.get("intent", "")).strip()
    if not intent:
        return "ERROR: intent is required"
    result = _resolve_element(
        intent=intent,
        preferred_roles=args.get("preferred_roles") or None,
        text_hints=args.get("text_hints") or None,
        label_hints=args.get("label_hints") or None,
        require_visible=bool(args.get("require_visible", True)),
        require_enabled=bool(args.get("require_enabled", False)),
        mode=str(args.get("mode") or "normal"),
        scope=args.get("scope") if isinstance(args.get("scope"), dict) else None,
    )
    logger.info(
        "web_resolve intent=%r element_id=%r confidence=%s reason=%r generation=%s",
        intent,
        result.get("element_id"),
        result.get("confidence"),
        result.get("reason"),
        result.get("observation_generation"),
    )
    return json.dumps(result, indent=2)


async def _resolve_selector(element_id: str) -> tuple[str | None, str]:
    selector = observation._element_map.get(element_id.strip())
    if not selector:
        return None, (f"ERROR: unknown element_id {element_id!r}. "
                      "Call web_observe first to (re)discover element IDs.")
    return selector, ""

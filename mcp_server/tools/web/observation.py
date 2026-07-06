"""Page observation and form diagnostics.

Owns the element maps that give every observed element a stable ID
(``_element_map`` id -> selector, ``_element_data_map`` id -> metadata) plus
the last-observation snapshot and its form diagnostics. Resolution and
action submodules read that state through this module — the scalar values
(``_last_observation``, ``_last_form_diagnostics``, ``_observe_generation``)
are rebound on every observation, so they must not be ``from``-imported.

SINGLE-RUN INVARIANT (3.6-web): this element state is module-global and NOT
keyed by session/run. That is safe today because the orchestrator spawns ONE
MCP server subprocess per run, so the process boundary IS the session
boundary. If the MCP server is ever shared across concurrent runs, element
ids from one run would resolve against another run's page — use
``reset_element_state()`` at run boundaries (or key these maps by a session
token) before making that change.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from mcp_server.tools.web_observe_js import _OBSERVE_JS
from mcp_server.tools.web_element_scoring import (
    _norm, _strip_placeholder_prefix, _role_candidates,
    _is_visible_element, _debug_label,
)

from . import browser

logger = logging.getLogger(__name__)

_element_map: dict[str, str] = {}
_element_data_map: dict[str, dict[str, Any]] = {}
_last_observation: dict[str, Any] | None = None
_last_form_diagnostics: dict[str, Any] = {}
_observe_generation: int = 0


def reset_element_state() -> None:
    """Clear all observation state (see SINGLE-RUN INVARIANT in module docstring).

    Call this at a run/session boundary if one MCP server process ever serves
    more than one run, so element ids can never bleed across runs.
    """
    global _last_observation, _last_form_diagnostics, _observe_generation
    _element_map.clear()
    _element_data_map.clear()
    _last_observation = None
    _last_form_diagnostics = {}
    _observe_generation = 0


def _is_field(el: dict[str, Any]) -> bool:
    roles = _role_candidates(el)
    return bool(roles & {"textbox", "searchbox", "combobox", "listbox", "checkbox", "radio"}) or el.get("tag") in {
        "input",
        "textarea",
        "select",
    }


def _is_submit_or_create_button(el: dict[str, Any]) -> bool:
    roles = _role_candidates(el)
    if "button" not in roles:
        return False
    text = " ".join(
        str(el.get(k, ""))
        for k in ("label", "text", "value", "aria_label", "name", "title", "type")
    ).lower()
    return bool(re.search(r"\b(submit|create|save|send|publish|continue|confirm|finish)\b", text)) or _norm(el.get("type")) == "submit"


def _field_display_name(el: dict[str, Any]) -> str:
    label = _debug_label(el).strip()
    roles = _role_candidates(el)
    if "radio" in roles:
        kind = "radio"
    elif "checkbox" in roles:
        kind = "checkbox"
    elif "combobox" in roles or "listbox" in roles:
        kind = "select"
    else:
        kind = "textbox"
    if kind in label.lower():
        return label
    return f"{label} {kind}".strip()


def _button_display_name(el: dict[str, Any]) -> str:
    label = _debug_label(el).strip()
    if "button" in label.lower():
        return label
    return f"{label} button".strip()


def _required_field_empty(el: dict[str, Any]) -> bool:
    roles = _role_candidates(el)
    if roles & {"checkbox", "radio"}:
        return not bool(el.get("checked"))
    return not str(el.get("value") or "").strip()


def _build_form_diagnostics(result: dict[str, Any]) -> dict[str, Any]:
    elements = list(result.get("elements") or [])
    forms_by_id: dict[str, dict[str, Any]] = {}
    fields: list[dict[str, Any]] = []
    required_fields: list[dict[str, Any]] = []
    empty_required_fields: list[dict[str, Any]] = []
    disabled_submit_buttons: list[dict[str, Any]] = []

    for el in elements:
        if not _is_visible_element(el):
            continue
        form_id = str(el.get("form_id") or "page")
        form = forms_by_id.setdefault(form_id, {"form_id": form_id, "fields": [], "buttons": []})
        summary = {
            "element_id": el.get("id"),
            "name": _debug_label(el),
            "label": _strip_placeholder_prefix(el.get("label")),
            "role": el.get("role", ""),
            "tag": el.get("tag", ""),
            "type": el.get("type", ""),
            "required": bool(el.get("required")),
            "empty": _required_field_empty(el) if _is_field(el) else False,
            "disabled": bool(el.get("disabled")),
            "visible": _is_visible_element(el),
            "in_viewport": bool(el.get("in_viewport")),
            "form_id": el.get("form_id", ""),
            "container_id": el.get("container_id", ""),
            "bbox": el.get("bbox"),
        }
        if _is_field(el):
            fields.append(summary)
            form["fields"].append(summary)
            if el.get("required"):
                required_fields.append(summary)
                if _required_field_empty(el):
                    empty_required_fields.append(summary)
        if "button" in _role_candidates(el):
            form["buttons"].append(summary)
            if el.get("disabled") and _is_submit_or_create_button(el):
                disabled_submit_buttons.append(summary)

    empty_names = [f.get("name") or f.get("label") or str(f.get("element_id")) for f in empty_required_fields]
    messages: list[str] = []
    for field in empty_required_fields:
        element = _element_data_map.get(str(field.get("element_id"))) or next(
            (el for el in elements if el.get("id") == field.get("element_id")),
            {},
        )
        messages.append(f"{_field_display_name(element)} is required and empty.")

    for button in disabled_submit_buttons:
        element = _element_data_map.get(str(button.get("element_id"))) or next(
            (el for el in elements if el.get("id") == button.get("element_id")),
            {},
        )
        button_name = _button_display_name(element)
        if empty_names:
            reason = ", ".join(empty_names[:3])
            if len(empty_names) > 3:
                reason += f", and {len(empty_names) - 3} more"
            messages.append(f"{button_name} is disabled, likely because {reason} is empty.")
        else:
            messages.append(f"{button_name} is disabled; likely required fields or validation are incomplete.")

    return {
        "page_title": result.get("title", ""),
        "url": result.get("url", ""),
        "forms": list(forms_by_id.values()),
        "visible_interactive_element_count": len([el for el in elements if _is_visible_element(el)]),
        "fields": fields,
        "required_fields": required_fields,
        "empty_required_fields": empty_required_fields,
        "disabled_submit_buttons": disabled_submit_buttons,
        "messages": messages,
    }


def _remember_observation(result: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    global _last_observation, _last_form_diagnostics, _observe_generation
    elements = list(result.get("elements") or [])
    _observe_generation += 1
    _element_map.clear()
    _element_data_map.clear()
    for el in elements:
        element_id = str(el.get("id") or "")
        selector = str(el.get("selector") or "")
        if element_id and selector:
            _element_map[element_id] = selector
            _element_data_map[element_id] = dict(el)
    _last_form_diagnostics = _build_form_diagnostics(result)
    _last_observation = {
        "url": result.get("url", ""),
        "title": result.get("title", ""),
        "generation": _observe_generation,
        "elements": elements,
        "form_diagnostics": _last_form_diagnostics,
    }
    logger.info(
        "web_observe generation=%s url=%r elements=%s required=%s empty_required=%s disabled_submit=%s",
        _observe_generation,
        result.get("url", ""),
        len(elements),
        len(_last_form_diagnostics.get("required_fields", [])),
        len(_last_form_diagnostics.get("empty_required_fields", [])),
        len(_last_form_diagnostics.get("disabled_submit_buttons", [])),
    )
    return elements, _last_form_diagnostics


def _structured_observation_payload(
    result: dict[str, Any],
    elements: list[dict[str, Any]],
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    def public_element(el: dict[str, Any]) -> dict[str, Any]:
        return {
            "element_id": el.get("id"),
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

    return {
        "observation_generation": _observe_generation,
        "page_title": result.get("title", ""),
        "url": result.get("url", ""),
        "interactive_elements": [public_element(el) for el in elements],
        "visible_interactive_elements": [public_element(el) for el in elements if _is_visible_element(el)],
        "forms": diagnostics.get("forms", []),
        "required_fields": diagnostics.get("required_fields", []),
        "empty_required_fields": diagnostics.get("empty_required_fields", []),
        "disabled_submit_buttons": diagnostics.get("disabled_submit_buttons", []),
        "diagnostic_messages": diagnostics.get("messages", []),
    }


async def handle_web_observe(args: dict) -> str:
    page = await browser._page()
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass

    try:
        result = await page.evaluate(_OBSERVE_JS)
    except Exception as e:
        return f"ERROR: observe failed: {e}"

    elements, diagnostics = _remember_observation(result)
    shown_elements = [el for el in elements if _is_visible_element(el)]

    limit = int(args.get("limit", 80))
    lines = [
        "WEB_OBSERVATION",
        f"URL: {result.get('url', '')}",
        f"Title: {result.get('title', '')}",
        f"Found {len(elements)} interactive elements"
        + (f" ({len(shown_elements)} visible/offscreen)")
        + (f" (showing first {limit})" if len(shown_elements) > limit else "")
        + ".",
        "Use web_click(element_id) and web_fill(element_id, text). "
        "Use web_press for Enter/Tab on the focused field.",
        "",
    ]

    if not shown_elements:
        lines.append("- No interactive elements found. The page may still be loading; "
                     "call web_wait_for or web_observe again.")
        lines.append("")
        lines.append("WEB_OBSERVATION_JSON:")
        lines.append(json.dumps(_structured_observation_payload(result, elements, diagnostics), indent=2))
        return "\n".join(lines)

    for el in shown_elements[:limit]:
        flags = []
        if el.get("disabled"):
            flags.append("disabled")
        if el.get("required"):
            flags.append("required")
        if el.get("checked"):
            flags.append("checked")
        if el.get("hidden"):
            flags.append("hidden")
        elif not el.get("in_viewport"):
            flags.append("offscreen")
        flag_str = f" [{','.join(flags)}]" if flags else ""

        label = el.get("label") or "(no label)"
        extras = []
        if el.get("value"):
            extras.append(f"value={el['value']!r}")
        if el.get("href"):
            extras.append(f"href={el['href']}")
        if el.get("type") and el["type"] != el["tag"]:
            extras.append(f"type={el['type']}")
        extra_str = " " + " ".join(extras) if extras else ""

        bx, by, bw, bh = el["bbox"]
        lines.append(
            f"- {el['id']}: <{el['tag']} role={el['role']}>{flag_str} "
            f"{label!r}{extra_str} bbox=({bx},{by},{bw},{bh})"
        )

    if len(shown_elements) > limit:
        lines.append(f"... {len(shown_elements) - limit} more — re-run with limit higher if needed.")
    lines.extend(_form_schema_lines(elements))
    if diagnostics.get("messages"):
        lines.append("")
        lines.append("Form diagnostics:")
        for message in diagnostics["messages"]:
            lines.append(f"- {message}")
    lines.append("")
    lines.append("WEB_OBSERVATION_JSON:")
    lines.append(json.dumps(_structured_observation_payload(result, elements, diagnostics), indent=2))
    return "\n".join(lines)


def _form_schema_lines(elements: list[dict[str, Any]]) -> list[str]:
    """Compact fill-ready schema: one line per field, radio groups collapsed.

    Gives the model everything it needs to emit a single web_fill_form call
    without resolving fields one by one.
    """
    visible_fields = [el for el in elements if _is_visible_element(el) and _is_field(el)]
    if not visible_fields:
        return []

    lines = ["", "FORM_SCHEMA (one web_fill_form call can fill all of these):"]
    radio_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    flat: list[dict[str, Any]] = []
    for el in visible_fields:
        roles = _role_candidates(el)
        if "radio" in roles and el.get("name"):
            key = (str(el.get("form_id") or ""), str(el["name"]))
            radio_groups.setdefault(key, []).append(el)
        else:
            flat.append(el)

    for el in flat:
        roles = _role_candidates(el)
        if "checkbox" in roles:
            kind = "checkbox"
        elif roles & {"combobox", "listbox"}:
            kind = "select"
        else:
            kind = "text"
        bits = [kind]
        if el.get("required"):
            bits.append("required")
        if kind == "checkbox":
            bits.append("checked" if el.get("checked") else "unchecked")
        elif str(el.get("value") or "").strip():
            bits.append(f"value={str(el['value'])[:40]!r}")
        lines.append(f"- {el.get('id')}: {_debug_label(el)!r} ({', '.join(bits)})")

    for (_form_id, name), group in radio_groups.items():
        options = ", ".join(
            _debug_label(el) + (" *" if el.get("checked") else "")
            for el in group
        )
        lines.append(f"- radio group {name!r}: options: {options} (* = selected)")

    return lines


async def _post_action_state(page) -> str:
    """Compact page-state line for mutation results.

    Read-only: evaluates the observe JS transiently WITHOUT calling
    _remember_observation, so previously issued element_ids stay valid.
    """
    try:
        url = page.url
    except Exception:
        return ""
    prev = _last_observation or {}
    parts: list[str] = []
    if url != prev.get("url", ""):
        parts.append(f"url changed -> {url}")
    try:
        result = await page.evaluate(_OBSERVE_JS)
        fresh_elements = list(result.get("elements") or [])
        new_count = len([el for el in fresh_elements if _is_visible_element(el)])
        old_count = len([el for el in (prev.get("elements") or []) if _is_visible_element(el)])
        if new_count != old_count:
            parts.append(
                f"visible elements {old_count} -> {new_count} "
                "(element_ids may be stale; web_observe before further id-based actions)"
            )
        diagnostics = _build_form_diagnostics(result)
        parts.extend((diagnostics.get("messages") or [])[:3])
    except Exception:
        pass
    if not parts:
        return ""
    return "PAGE_STATE: " + "; ".join(parts)


async def _observe_now(page) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run the observe JS and commit it to the element maps."""
    result = await page.evaluate(_OBSERVE_JS)
    return _remember_observation(result)

"""
Kim's controlled web browser via Playwright.

Spawns a persistent Chromium with its own profile in
``sessions/kim_browser/``. First run: window opens, user signs into the
sites they want. Cookies/storage persist across runs.

Why this exists: macOS Accessibility cannot reliably read web page
content in Chrome — only the chrome itself (toolbar, address bar) and
some static text are exposed. ``observe_ui`` works for native apps but
returns 3-4 elements on github.com/new. Playwright drives Chromium via
CDP, so we get the actual DOM with 100% accuracy.

Tools registered in mcp_server/server.py:

    web_open(url)                    – navigate the controlled browser
    web_observe()                    – structured list of interactive
                                       elements with stable IDs
    web_click(element_id)            – click by ID from last observe
    web_fill(element_id, text)       – fill input by ID
    web_press(key)                   – press a key (Enter, Tab, …)
    web_text()                       – plain text of the page
    web_screenshot()                 – base64 PNG fallback
    web_wait_for(text, timeout_ms)   – wait until text appears
    web_back() / web_close()
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from mcp_server.config import PROJECT_ROOT, USE_REAL_BROWSER
from mcp_server.os_utils import IS_MACOS, IS_WINDOWS, IS_LINUX

logger = logging.getLogger(__name__)

# Module-level singletons. The MCP server is a single subprocess, so these
# survive across tool calls within a session.
_lock = asyncio.Lock()
_playwright: Any = None
_browser_ctx: Any = None
_active_page: Any = None
_element_map: dict[str, str] = {}
_element_data_map: dict[str, dict[str, Any]] = {}
_last_observation: dict[str, Any] | None = None
_last_form_diagnostics: dict[str, Any] = {}
_observe_generation: int = 0
_is_real_browser: bool = False
_last_connection_error: str = ""
_DEDICATED_CDP_PORT = int(os.environ.get("KIM_DEDICATED_BROWSER_CDP_PORT", "9333"))

def _resolve_user_data_dir() -> Path:
    """Pick a writable directory for the persistent Chromium profile.

    config.yaml's ``project_root`` is sometimes a stale path baked in from a
    different machine, so we try it first but fall back to a directory next
    to this file (which always exists) and finally ``~/.kim``.
    """
    candidates = [
        PROJECT_ROOT / "sessions" / "kim_browser",
        Path(__file__).resolve().parents[2] / "sessions" / "kim_browser",
        Path.home() / ".kim" / "sessions" / "kim_browser",
    ]
    for c in candidates:
        try:
            c.mkdir(parents=True, exist_ok=True)
            return c
        except OSError:
            continue
    raise RuntimeError("Could not locate a writable directory for kim_browser profile")


USER_DATA_DIR = _resolve_user_data_dir()


async def _launch_real_browser() -> bool:
    """Attempt to launch the user's real browser with remote debugging enabled."""
    cdp_arg = "--remote-debugging-port=9222"

    try:
        if IS_MACOS:
            # On macOS, simply launching with the flag is often blocked if another
            # instance is open. We use 'open' which is cleaner.
            subprocess.Popen(["open", "-a", "Google Chrome", "--args", cdp_arg])
            return True
        elif IS_WINDOWS:
            for p in [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ]:
                if Path(p).exists():
                    subprocess.Popen([p, cdp_arg])
                    return True
        elif IS_LINUX:
            for bin_name in ["google-chrome", "chromium-browser", "chromium"]:
                if shutil.which(bin_name):
                    subprocess.Popen([bin_name, cdp_arg])
                    return True
    except Exception as e:
        logger.warning(f"web: failed to launch real browser: {e}")

    return False


def _browser_executable() -> str | None:
    env_path = os.environ.get("KIM_BROWSER_EXECUTABLE", "").strip()
    if env_path and Path(env_path).exists():
        return env_path
    if IS_MACOS:
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if Path(chrome).exists():
            return chrome
        chromium = "/Applications/Chromium.app/Contents/MacOS/Chromium"
        if Path(chromium).exists():
            return chromium
    elif IS_WINDOWS:
        for path in [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]:
            if Path(path).exists():
                return path
    else:
        for bin_name in ["google-chrome", "chromium-browser", "chromium"]:
            found = shutil.which(bin_name)
            if found:
                return found
    return None


_CDP_CONNECT_TIMEOUT_MS = int(os.environ.get("KIM_CDP_CONNECT_TIMEOUT_MS", "10000"))


async def _connect_over_cdp(port: int) -> Any | None:
    global _last_connection_error
    for host in ["127.0.0.1", "localhost"]:
        try:
            return await _playwright.chromium.connect_over_cdp(
                f"http://{host}:{port}",
                timeout=_CDP_CONNECT_TIMEOUT_MS,
            )
        except Exception as e:
            _last_connection_error = str(e)
    return None


async def _launch_dedicated_browser() -> bool:
    """Launch Kim's own detached browser process so it survives MCP exit."""
    executable = _browser_executable()
    if not executable:
        _last_connection_error = "No Chrome/Chromium executable found"
        return False

    args = [
        executable,
        f"--remote-debugging-port={_DEDICATED_CDP_PORT}",
        f"--user-data-dir={USER_DATA_DIR}",
        "--no-default-browser-check",
        "--no-first-run",
        "--disable-blink-features=AutomationControlled",
        "about:blank",
    ]
    try:
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception as e:
        logger.warning(f"web: failed to launch dedicated browser: {e}")
        _last_connection_error = str(e)
        return False


async def _active_browser_context() -> Any:
    if hasattr(_browser_ctx, "pages"):
        return _browser_ctx
    contexts = getattr(_browser_ctx, "contexts", [])
    if contexts:
        return contexts[0]
    return await _browser_ctx.new_context()


async def _ensure_browser() -> None:
    """Lazily start Playwright + Chromium, reuse across tool calls."""
    global _playwright, _browser_ctx, _active_page, _is_real_browser

    if _active_page is not None:
        try:
            await _active_page.evaluate("1")
            return
        except Exception:
            logger.info("web: active page died, re-creating")
            _active_page = None

    from playwright.async_api import async_playwright

    if _playwright is None:
        _playwright = await async_playwright().start()

    if _browser_ctx is None:
        global _last_connection_error
        _last_connection_error = ""

        if USE_REAL_BROWSER:
            # 1. Try to connect to an existing browser on 9222
            _browser_ctx = await _connect_over_cdp(9222)
            if _browser_ctx is not None:
                _is_real_browser = True
                logger.info("web: connected to existing browser via CDP on port 9222")

            if _browser_ctx is None:
                # 2. If connection fails, try to launch the real browser with CDP flag
                logger.info(f"web: no browser on 9222 ({_last_connection_error}), attempting launch...")
                if await _launch_real_browser():
                    # Retry connection a few times
                    for attempt in range(1, 6):
                        logger.info(f"web: connection attempt {attempt}/5...")
                        await asyncio.sleep(2)
                        _browser_ctx = await _connect_over_cdp(9222)
                        if _browser_ctx is not None:
                            _is_real_browser = True
                            logger.info("web: connected to real browser via CDP after launch")
                        if _browser_ctx:
                            break

        # 3. Fallback to Kim's own persistent context if CDP failed or was disabled
        if _browser_ctx is None:
            _is_real_browser = False
            _browser_ctx = await _connect_over_cdp(_DEDICATED_CDP_PORT)
            if _browser_ctx is None:
                logger.info(
                    f"web: launching detached dedicated Kim browser profile "
                    f"(Reason: {_last_connection_error})"
                )
                if await _launch_dedicated_browser():
                    for attempt in range(1, 8):
                        logger.info(f"web: dedicated browser connection attempt {attempt}/7...")
                        await asyncio.sleep(1)
                        _browser_ctx = await _connect_over_cdp(_DEDICATED_CDP_PORT)
                        if _browser_ctx is not None:
                            break

            if _browser_ctx is None:
                logger.warning(
                    "web: detached dedicated browser failed; falling back to "
                    f"Playwright-owned persistent context ({_last_connection_error})"
                )
                _browser_ctx = await _playwright.chromium.launch_persistent_context(
                    user_data_dir=str(USER_DATA_DIR),
                    headless=False,
                    viewport={"width": 1280, "height": 820},
                    args=[
                        "--no-default-browser-check",
                        "--no-first-run",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )

    context = await _active_browser_context()
    pages = context.pages
    _active_page = pages[-1] if pages else await context.new_page()


def _on_context_closed() -> None:
    global _browser_ctx, _active_page, _last_observation, _last_form_diagnostics, _observe_generation
    _browser_ctx = None
    _active_page = None
    _element_map.clear()
    _element_data_map.clear()
    _last_observation = None
    _last_form_diagnostics = {}
    _observe_generation = 0


async def _page():
    async with _lock:
        await _ensure_browser()
    return _active_page


# ── tool handlers ─────────────────────────────────────────────────────────

async def handle_web_open(args: dict) -> str:
    url = str(args.get("url", "")).strip()
    username = str(args.get("username", "")).strip()
    password = str(args.get("password", "")).strip()

    if not url:
        return "ERROR: url is required"
    if not url.startswith(("http://", "https://", "file://", "about:", "chrome://", "data:")):
        url = "https://" + url

    page = await _page()

    if username and password:
        auth = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        await page.set_extra_http_headers({"Authorization": f"Basic {auth}"})
    else:
        await page.set_extra_http_headers({})

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
    except Exception as e:
        err_text = str(e)
        if (
            "ERR_INVALID_AUTH_CREDENTIALS" in err_text
            or "ERR_HTTP_RESPONSE_CODE_FAILURE" in err_text
            or "401" in err_text
        ):
            mode_str = " (Real Browser)" if _is_real_browser else " (Dedicated Kim Browser)"
            if username or password:
                return (
                    f"AUTH_FAILED: {url}{mode_str}\n"
                    "The site rejected the supplied HTTP authentication credentials. "
                    "Do not call web_observe/web_text/web_screenshot for page content yet; "
                    "ask the user to verify the username/password or sign in manually."
                )
            return (
                f"AUTH_REQUIRED: {url}{mode_str}\n"
                "The site is open but blocked by an HTTP authentication popup. "
                "Call web_open again with username and password if you have them; "
                "otherwise ask the user to sign in manually."
            )
        return f"ERROR: navigation failed: {e}"

    if page.url.startswith("chrome-error://"):
        mode_str = " (Real Browser)" if _is_real_browser else " (Dedicated Kim Browser)"
        return (
            f"ERROR: browser error page after opening {url}{mode_str}\n"
            "The controlled browser did not reach usable page content. "
            "Do not treat this as open; ask for help or retry with valid credentials."
        )
    try:
        title = await page.title()
    except Exception:
        title = ""

    if not _is_real_browser and USE_REAL_BROWSER:
        err_hint = f" ({_last_connection_error})" if _last_connection_error else ""
        if "target closed" in err_hint.lower() or "connection closed" in err_hint.lower():
            err_hint += " — This usually means Chrome is already open with your profile. You must QUIT Chrome completely before Kim can control it."
        mode_str = f" (Fallback: Dedicated Kim Browser — Could not connect to your real browser{err_hint})"
    elif _is_real_browser:
        mode_str = " (Real Browser)"
    else:
        mode_str = " (Dedicated Kim Browser)"

    return f"Opened: {page.url}{mode_str}\nTitle: {title}"


from mcp_server.tools.web_observe_js import _OBSERVE_JS
from mcp_server.tools.web_element_scoring import (
    _GENERIC_INTENT_TOKENS, _ACTION_INTENT_TOKENS, _RESOLVE_THRESHOLDS,
    _norm, _tokens, _as_str_list, _strip_placeholder_prefix,
    _role_candidates, _infer_preferred_roles, _intent_focus,
    _important_tokens, _match_score, _best_match,
    _is_visible_element, _debug_label, _candidate_metadata,
    _scope_value, _searchable_text, _missing_strict_tokens,
    _expand_with_synonyms,
)


def _scope_for_element(element_id: str | None) -> dict[str, Any]:
    if not element_id:
        return {}
    el = _element_data_map.get(str(element_id))
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
        ref = _element_data_map.get(same_form_as)
        ref_form = str((ref or {}).get("form_id") or "")
        if not ref_form or str(el.get("form_id") or "") != ref_form:
            return False, f"outside form of {same_form_as}"

    form_id = _scope_value(scope, "form_id")
    if form_id and str(el.get("form_id") or "") != form_id:
        return False, f"outside form {form_id!r}"

    same_container_as = _scope_value(scope, "same_container_as")
    if same_container_as:
        ref = _element_data_map.get(same_container_as)
        ref_container = str((ref or {}).get("container_id") or "")
        if ref_container and str(el.get("container_id") or "") != ref_container:
            return False, f"outside container of {same_container_as}"

    container_id = _scope_value(scope, "container_id")
    if container_id and str(el.get("container_id") or "") != container_id:
        return False, f"outside container {container_id!r}"

    after_element = _scope_value(scope, "after_element")
    if after_element:
        ref = _element_data_map.get(after_element)
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
        elements = list(_element_data_map.values())

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
        "observation_generation": _observe_generation,
        "url": (_last_observation or {}).get("url", ""),
        "title": (_last_observation or {}).get("title", ""),
    }


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
    page = await _page()
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
    selector = _element_map.get(element_id.strip())
    if not selector:
        return None, (f"ERROR: unknown element_id {element_id!r}. "
                      "Call web_observe first to (re)discover element IDs.")
    return selector, ""


async def handle_web_click(args: dict) -> str:
    el_id = str(args.get("element_id", "")).strip()
    if not el_id:
        return "ERROR: element_id is required"
    selector, err = await _resolve_selector(el_id)
    if not selector:
        return err
    page = await _page()
    try:
        locator = page.locator(selector).first
        await locator.scroll_into_view_if_needed(timeout=6000)
        await locator.click(timeout=6000)
    except Exception as e:
        return f"ERROR: click failed for {el_id} ({selector}): {e}"
    state = await _post_action_state(page)
    return f"Clicked {el_id}" + (f"\n{state}" if state else "")


async def handle_web_fill(args: dict) -> str:
    el_id = str(args.get("element_id", "")).strip()
    text = str(args.get("text", ""))
    if not el_id:
        return "ERROR: element_id is required"
    selector, err = await _resolve_selector(el_id)
    if not selector:
        return err
    page = await _page()
    try:
        await page.locator(selector).first.fill(text, timeout=6000)
    except Exception as e:
        return f"ERROR: fill failed for {el_id}: {e}"
    state = await _post_action_state(page)
    return f"Filled {el_id} with {len(text)} chars" + (f"\n{state}" if state else "")


# ── Composite form filling ───────────────────────────────────────────────────

_TRUTHY_VALUES = {"true", "yes", "on", "1", "checked", "check", "enable", "enabled"}
_FALSY_VALUES = {"false", "no", "off", "0", "unchecked", "uncheck", "disable", "disabled"}


def _coerce_bool(value: Any) -> bool | None:
    """Interpret a field value as a checkbox toggle. None = not boolean-like."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = _norm(value)
    if text in _TRUTHY_VALUES:
        return True
    if text in _FALSY_VALUES:
        return False
    return None


async def _observe_now(page) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run the observe JS and commit it to the element maps."""
    result = await page.evaluate(_OBSERVE_JS)
    return _remember_observation(result)


async def _act_on_element(page, element_id: str, action: str, value: Any = None) -> str | None:
    """Perform click/fill/select on a mapped element. Returns error text or None."""
    selector = _element_map.get(element_id)
    if not selector:
        return f"no selector mapped for {element_id}"
    locator = page.locator(selector).first
    try:
        await locator.scroll_into_view_if_needed(timeout=6000)
        if action == "click":
            await locator.click(timeout=6000)
        elif action == "select":
            label = str(value)
            try:
                await locator.select_option(label=label, timeout=6000)
            except Exception:
                await locator.select_option(value=label, timeout=6000)
        else:
            await locator.fill(str(value), timeout=6000)
        return None
    except Exception as e:
        return str(e)


async def _fill_one_field(page, intent: str, raw_value: Any, mode: str) -> dict[str, Any]:
    """Resolve one field description and apply its value.

    Handles four shapes:
      - text inputs / textareas        -> fill
      - checkboxes (boolean-ish value) -> click to reach desired state
      - radio groups (option value)    -> resolve "<value> <intent>" and click
      - selects                        -> select_option by label, then value
    Falls back to Playwright's accessible-label lookup when the observation
    map can't resolve the field.
    """
    entry: dict[str, Any] = {"intent": intent, "value": raw_value, "ok": False}
    bool_value = _coerce_bool(raw_value)

    resolved = _resolve_element(intent=intent, mode=mode, require_text_evidence=True)
    el = _element_data_map.get(str(resolved.get("element_id") or "")) if resolved.get("ok") else None
    roles = _role_candidates(el) if el else set()

    # Option-valued field ("visibility" -> "private"): the specific option
    # element matches "<value> <intent>" better than the group label does.
    if bool_value is None and isinstance(raw_value, str) and raw_value.strip():
        if el is None or "radio" in roles:
            option = _resolve_element(
                intent=f"{raw_value} {intent}",
                preferred_roles=["radio", "checkbox"],
                label_hints=[str(raw_value)],
                text_hints=[str(raw_value)],
                mode=mode,
                require_text_evidence=True,
                restrict_roles=True,
            )
            if option.get("ok"):
                option_el = _element_data_map.get(str(option["element_id"]))
                option_roles = _role_candidates(option_el or {})
                if option_roles & {"radio", "checkbox"}:
                    resolved, el, roles = option, option_el, option_roles

    if el is not None:
        element_id = str(el.get("id"))
        entry["element_id"] = element_id
        entry["confidence"] = resolved.get("confidence")
        entry["matched_label"] = _debug_label(el)

        if "radio" in roles:
            entry["action"] = "click (radio)"
            error = None if el.get("checked") else await _act_on_element(page, element_id, "click")
        elif "checkbox" in roles:
            desired = bool_value if bool_value is not None else True
            entry["action"] = f"click (checkbox -> {'checked' if desired else 'unchecked'})"
            if bool(el.get("checked")) == desired:
                error = None
            else:
                error = await _act_on_element(page, element_id, "click")
        elif roles & {"combobox", "listbox"} or el.get("tag") == "select":
            entry["action"] = "select"
            error = await _act_on_element(page, element_id, "select", raw_value)
            if error:
                entry["action"] = "fill (select fallback)"
                error = await _act_on_element(page, element_id, "fill", raw_value)
        else:
            entry["action"] = "fill"
            error = await _act_on_element(page, element_id, "fill", raw_value)

        if error:
            entry["error"] = error
        else:
            entry["ok"] = True
        return entry

    # Fallback: browser-native accessible-label lookup (covers labels the
    # observation extractor missed). Text fields only — option semantics
    # need the observation metadata.
    if bool_value is None:
        try:
            pattern = re.compile(re.escape(_intent_focus(intent)), re.IGNORECASE)
            locator = page.get_by_label(pattern).first
            if await locator.count() > 0:
                await locator.fill(str(raw_value), timeout=6000)
                entry["ok"] = True
                entry["action"] = "fill (accessible-label fallback)"
                return entry
        except Exception as e:
            entry["fallback_error"] = str(e)

    entry["error"] = f"could not resolve field: {resolved.get('reason') or 'no match'}"
    entry["candidates"] = (resolved.get("candidates") or [])[:3]
    return entry


async def handle_web_fill_form(args: dict) -> str:
    """Fill a whole form in one tool call: observe, resolve every field,
    act, optionally submit, and report. Replaces 5-10 single-step calls."""
    fields = args.get("fields")
    if not isinstance(fields, dict) or not fields:
        return ("ERROR: 'fields' must be a non-empty object mapping field descriptions "
                "to values, e.g. {\"repository name\": \"my-repo\", \"private\": true}.")
    submit = str(args.get("submit") or "").strip()
    mode = str(args.get("mode") or "normal")

    page = await _page()
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    try:
        await _observe_now(page)
    except Exception as e:
        return f"ERROR: could not observe the page before filling: {e}"

    report: list[dict[str, Any]] = []
    fields_ok = True
    for intent, raw_value in fields.items():
        entry = await _fill_one_field(page, str(intent), raw_value, mode)
        report.append(entry)
        if not entry.get("ok"):
            fields_ok = False

    submit_entry: dict[str, Any] | None = None
    if submit:
        if not fields_ok:
            submit_entry = {
                "intent": submit,
                "ok": False,
                "skipped": True,
                "error": "skipped because one or more fields failed — fix them or submit manually",
            }
        else:
            # Re-observe: filling often enables a previously disabled submit.
            try:
                await _observe_now(page)
            except Exception:
                pass
            resolved = _resolve_element(
                intent=submit,
                preferred_roles=["button"],
                require_enabled=True,
                mode=mode,
                require_text_evidence=True,
                restrict_roles=True,
            )
            if resolved.get("ok"):
                element_id = str(resolved["element_id"])
                error = await _act_on_element(page, element_id, "click")
                if error is None:
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=8000)
                    except Exception:
                        pass
                submit_entry = {
                    "intent": submit,
                    "ok": error is None,
                    "element_id": element_id,
                    "confidence": resolved.get("confidence"),
                }
                if error:
                    submit_entry["error"] = error
            else:
                submit_entry = {
                    "intent": submit,
                    "ok": False,
                    "error": f"could not resolve an enabled submit target: {resolved.get('reason')}",
                    "candidates": (resolved.get("candidates") or [])[:3],
                }

    final_state: dict[str, Any] = {}
    try:
        elements, diagnostics = await _observe_now(page)
        final_state = {
            "url": (_last_observation or {}).get("url", ""),
            "title": (_last_observation or {}).get("title", ""),
            "diagnostic_messages": diagnostics.get("messages", []),
        }
    except Exception:
        try:
            final_state = {"url": page.url}
        except Exception:
            pass

    overall_ok = fields_ok and (submit_entry is None or bool(submit_entry.get("ok")))
    payload = {
        "ok": overall_ok,
        "fields": report,
        "submit": submit_entry,
        "final_state": final_state,
        "note": "element_ids from earlier observations are stale; call web_observe before further id-based actions.",
    }
    logger.info(
        "web_fill_form ok=%s fields=%d failed=%d submit=%r",
        overall_ok,
        len(report),
        sum(1 for r in report if not r.get("ok")),
        submit or None,
    )
    return "FORM_FILL_REPORT\n" + json.dumps(payload, indent=2)


async def handle_web_press(args: dict) -> str:
    key = str(args.get("key", "")).strip()
    if not key:
        return "ERROR: key is required (e.g. 'Enter', 'Tab', 'Escape')"
    page = await _page()
    try:
        await page.keyboard.press(key)
    except Exception as e:
        return f"ERROR: press failed: {e}"
    return f"Pressed {key}"


async def handle_web_text(args: dict) -> str:
    page = await _page()
    try:
        text = await page.evaluate(
            "() => (document.body && document.body.innerText) || ''"
        )
    except Exception as e:
        return f"ERROR: text extraction failed: {e}"
    max_chars = int(args.get("max_chars", 8000))
    text = text.strip()
    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n... [truncated; total {len(text)} chars]"
    return text or "(empty page)"


async def handle_web_screenshot(args: dict) -> str:
    page = await _page()
    full = bool(args.get("full_page", False))
    try:
        png = await page.screenshot(full_page=full, type="png")
    except Exception as e:
        return f"ERROR: screenshot failed: {e}"
    b64 = base64.b64encode(png).decode("ascii")
    return f"WEB_SCREENSHOT_BASE64:image/png:{b64}"


async def handle_web_wait_for(args: dict) -> str:
    target = str(args.get("text") or args.get("selector") or "").strip()
    timeout = int(args.get("timeout_ms", 10000))
    if not target:
        return "ERROR: 'text' or 'selector' is required"
    page = await _page()
    try:
        if target.startswith("/") or target.startswith(("css=", "xpath=")) or any(
            ch in target for ch in "#.[>:"
        ):
            await page.locator(target).first.wait_for(timeout=timeout, state="visible")
            return f"Selector matched: {target}"
        await page.get_by_text(target, exact=False).first.wait_for(
            timeout=timeout, state="visible"
        )
        return f"Text appeared: {target!r}"
    except Exception as e:
        return f"Timeout waiting for {target!r}: {e}"


async def handle_web_wait_for_url(args: dict) -> str:
    contains = str(args.get("url_contains") or "").strip()
    regex = str(args.get("url_regex") or "").strip()
    timeout = int(args.get("timeout_ms", 10000))
    if not contains and not regex:
        return "ERROR: 'url_contains' or 'url_regex' is required"
    page = await _page()
    try:
        if regex:
            pattern = re.compile(regex)
            await page.wait_for_url(lambda url: bool(pattern.search(str(url))), timeout=timeout)
            return f"URL matched regex: {page.url}"
        await page.wait_for_url(lambda url: contains in str(url), timeout=timeout)
        return f"URL contains {contains!r}: {page.url}"
    except Exception as e:
        current = getattr(page, "url", "")
        target = regex or contains
        return f"Timeout waiting for URL {target!r}; current URL is {current!r}: {e}"


async def handle_web_back(args: dict) -> str:
    page = await _page()
    try:
        await page.go_back(wait_until="domcontentloaded", timeout=10000)
    except Exception as e:
        return f"ERROR: back failed: {e}"
    return f"Navigated back to {page.url}"


async def handle_web_close(args: dict) -> str:
    """
    Prevent the LLM from closing the browser automatically.
    The browser should stay open so the user remains signed in.
    """
    return "The Kim browser remains open so that your session and logins are preserved for the next task."

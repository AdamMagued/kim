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

Package layout (behavior-preserving split of the former web.py monolith):

    browser.py      – Playwright/Chromium session lifecycle singletons
    navigation.py   – web_open (SSRF/scheme guards), back/close, wait tools
    observation.py  – web_observe, element maps, form diagnostics
    resolution.py   – semantic intent -> element resolution, web_resolve
    actions.py      – web_click/fill/press/text/screenshot, web_fill_form

This ``__init__`` is a facade: it re-exports the tool handlers plus the
helpers that github.py and the test-suite reach for, and forwards reads of
the rebound module-level state (``_last_observation`` & friends) to the
owning submodule so they stay live.
"""

from __future__ import annotations

from typing import Any

from . import actions, browser, navigation, observation, resolution
from .browser import USER_DATA_DIR, _ensure_browser, _page
from .navigation import (
    _is_ssrf_target,
    handle_web_back,
    handle_web_close,
    handle_web_open,
    handle_web_wait_for,
    handle_web_wait_for_url,
)
from .observation import (
    _element_data_map,
    _element_map,
    _form_schema_lines,
    _observe_now,
    _post_action_state,
    _remember_observation,
    handle_web_observe,
)
from .resolution import (
    _resolve_element,
    _resolve_selector,
    _scope_for_element,
    handle_web_resolve,
)
from .actions import (
    _coerce_bool,
    handle_web_click,
    handle_web_fill,
    handle_web_fill_form,
    handle_web_press,
    handle_web_screenshot,
    handle_web_text,
)

# Rebound module-level state: these names are reassigned (not mutated) by the
# owning submodule, so a static ``from``-import here would go stale. PEP 562
# forwarding keeps package-level reads (e.g. github.py's
# ``web._last_form_diagnostics``) live. To *write* them (tests), target the
# owning submodule directly (``web.observation._last_observation = …``).
_FORWARDED_STATE = {
    "_last_observation": observation,
    "_last_form_diagnostics": observation,
    "_observe_generation": observation,
    "_playwright": browser,
    "_browser_ctx": browser,
    "_active_page": browser,
    "_is_real_browser": browser,
    "_last_connection_error": browser,
}


def __getattr__(name: str) -> Any:
    owner = _FORWARDED_STATE.get(name)
    if owner is not None:
        return getattr(owner, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

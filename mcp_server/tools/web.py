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
import logging
from pathlib import Path
from typing import Any

from mcp_server.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

# Module-level singletons. The MCP server is a single subprocess, so these
# survive across tool calls within a session.
_lock = asyncio.Lock()
_playwright: Any = None
_browser_ctx: Any = None
_active_page: Any = None
_element_map: dict[str, str] = {}

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


async def _ensure_browser() -> None:
    """Lazily start Playwright + Chromium, reuse across tool calls."""
    global _playwright, _browser_ctx, _active_page

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
        _browser_ctx.on("close", lambda: _on_context_closed())

    pages = _browser_ctx.pages
    _active_page = pages[-1] if pages else await _browser_ctx.new_page()


def _on_context_closed() -> None:
    global _browser_ctx, _active_page, _element_map
    _browser_ctx = None
    _active_page = None
    _element_map.clear()


async def _page():
    async with _lock:
        await _ensure_browser()
    return _active_page


# ── tool handlers ─────────────────────────────────────────────────────────

async def handle_web_open(args: dict) -> str:
    url = str(args.get("url", "")).strip()
    if not url:
        return "ERROR: url is required"
    if not url.startswith(("http://", "https://", "file://", "about:", "chrome://")):
        url = "https://" + url
    page = await _page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
    except Exception as e:
        return f"ERROR: navigation failed: {e}"
    try:
        title = await page.title()
    except Exception:
        title = ""
    return f"Opened: {page.url}\nTitle: {title}"


# JS that walks the live DOM and returns interactive elements with stable
# selectors and bounding boxes. Runs entirely in the page so we see exactly
# what the user sees.
_OBSERVE_JS = r"""
() => {
  const isVisible = el => {
    if (!el || !el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect();
    if (r.width <= 1 || r.height <= 1) return false;
    const s = window.getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none') return false;
    if (parseFloat(s.opacity || '1') < 0.05) return false;
    if (r.bottom < 0 || r.top > (window.innerHeight + 200)) return false;
    return true;
  };

  const labelOf = el => {
    const al = el.getAttribute && el.getAttribute('aria-label');
    if (al) return al.trim();
    if (el.placeholder) return ('placeholder: ' + el.placeholder).trim();
    if (el.title) return el.title.trim();
    if (el.alt) return el.alt.trim();
    if (el.value && el.tagName === 'INPUT' && (el.type === 'submit' || el.type === 'button')) return el.value.trim();
    const lid = el.getAttribute && el.getAttribute('aria-labelledby');
    if (lid) {
      const r = document.getElementById(lid);
      if (r) return (r.textContent || '').trim();
    }
    if (el.id) {
      const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lab) return (lab.textContent || '').trim();
    }
    const inner = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    return inner.slice(0, 120);
  };

  const cssPath = el => {
    if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) {
      return '#' + CSS.escape(el.id);
    }
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur !== document.body && parts.length < 6) {
      let sel = cur.tagName.toLowerCase();
      if (cur.id) {
        sel += '#' + CSS.escape(cur.id);
        parts.unshift(sel);
        break;
      }
      const parent = cur.parentNode;
      if (parent) {
        const sibs = Array.from(parent.children).filter(n => n.tagName === cur.tagName);
        if (sibs.length > 1) sel += `:nth-of-type(${sibs.indexOf(cur) + 1})`;
      }
      parts.unshift(sel);
      cur = cur.parentNode;
    }
    return parts.join(' > ');
  };

  const SEL = [
    'a[href]', 'button', 'input:not([type="hidden"])', 'textarea', 'select',
    '[role="button"]', '[role="link"]', '[role="textbox"]', '[role="searchbox"]',
    '[role="checkbox"]', '[role="radio"]', '[role="combobox"]', '[role="menuitem"]',
    '[role="tab"]', '[contenteditable="true"]', '[contenteditable=""]',
    'summary', '[onclick]', '[tabindex]:not([tabindex="-1"])'
  ].join(',');

  const seen = new Set();
  const out = [];
  let i = 0;
  for (const el of document.querySelectorAll(SEL)) {
    if (seen.has(el) || !isVisible(el)) continue;
    seen.add(el);
    const r = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role') || tag;
    out.push({
      id: 'w' + (++i),
      tag,
      role,
      label: labelOf(el).slice(0, 140),
      value: (el.value || '').slice(0, 120),
      href: (tag === 'a' && el.href) ? el.href.slice(0, 120) : '',
      type: el.type || '',
      checked: el.checked === true,
      disabled: el.disabled === true,
      bbox: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      selector: cssPath(el),
    });
    if (out.length >= 300) break;
  }
  return { url: location.href, title: document.title, elements: out };
}
"""


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

    elements = result.get("elements", [])
    _element_map.clear()
    for el in elements:
        _element_map[el["id"]] = el["selector"]

    limit = int(args.get("limit", 80))
    lines = [
        "WEB_OBSERVATION",
        f"URL: {result.get('url', '')}",
        f"Title: {result.get('title', '')}",
        f"Found {len(elements)} interactive elements"
        + (f" (showing first {limit})" if len(elements) > limit else "")
        + ".",
        "Use web_click(element_id) and web_fill(element_id, text). "
        "Use web_press for Enter/Tab on the focused field.",
        "",
    ]

    if not elements:
        lines.append("- No interactive elements found. The page may still be loading; "
                     "call web_wait_for or web_observe again.")
        return "\n".join(lines)

    for el in elements[:limit]:
        flags = []
        if el.get("disabled"):
            flags.append("disabled")
        if el.get("checked"):
            flags.append("checked")
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

    if len(elements) > limit:
        lines.append(f"... {len(elements) - limit} more — re-run with limit higher if needed.")
    return "\n".join(lines)


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
        await page.locator(selector).first.click(timeout=6000)
    except Exception as e:
        return f"ERROR: click failed for {el_id} ({selector}): {e}"
    return f"Clicked {el_id}"


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
    return f"Filled {el_id} with {len(text)} chars"


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


async def handle_web_back(args: dict) -> str:
    page = await _page()
    try:
        await page.go_back(wait_until="domcontentloaded", timeout=10000)
    except Exception as e:
        return f"ERROR: back failed: {e}"
    return f"Navigated back to {page.url}"


async def handle_web_close(args: dict) -> str:
    """Close the controlled browser. Profile data is preserved on disk."""
    global _browser_ctx, _active_page
    if _browser_ctx is not None:
        try:
            await _browser_ctx.close()
        except Exception as e:
            logger.warning(f"web: error during close: {e}")
    _browser_ctx = None
    _active_page = None
    _element_map.clear()
    return "Closed Kim browser. Profile saved at " + str(USER_DATA_DIR)

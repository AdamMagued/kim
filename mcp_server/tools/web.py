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
import os
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


async def _connect_over_cdp(port: int) -> Any | None:
    global _last_connection_error
    for host in ["127.0.0.1", "localhost"]:
        try:
            return await _playwright.chromium.connect_over_cdp(f"http://{host}:{port}")
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
    """
    Prevent the LLM from closing the browser automatically.
    The browser should stay open so the user remains signed in.
    """
    return "The Kim browser remains open so that your session and logins are preserved for the next task."

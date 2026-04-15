"""
Browser provider — 100% API-key-free LLM access via Playwright CDP.

Connects to an existing Chrome session (remote debugging port 9222), finds the
active AI chat tab (Claude, ChatGPT, or Gemini), injects the full context as a
single text message, waits for generation to finish, scrapes the response, and
parses it into the canonical {"type": "tool_call"|"text", ...} format.

MODES:
    1. Visible (browser_headless: false)  —  Default / first-time setup.
       User manually launches Chrome with remote debugging and logs in.
       Session cookies are saved to sessions/chrome_data/ for reuse.

    2. Headless (browser_headless: true)  —  Background mode after first login.
       Kim auto-launches Chromium invisibly via Playwright, reusing the
       saved session directory.  No browser window appears on screen.

       IMPORTANT: You must have logged in ONCE in visible mode first so
       the session cookies exist in sessions/chrome_data/.

SETUP (visible mode):
    Launch Chrome with remote debugging and a persistent profile:

    Windows:
        chrome.exe --remote-debugging-port=9222 --user-data-dir="<project>/sessions/chrome_data"

    macOS:
        /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \\
            --remote-debugging-port=9222 --user-data-dir="<project>/sessions/chrome_data"

    Linux:
        google-chrome --remote-debugging-port=9222 --user-data-dir="<project>/sessions/chrome_data"

    Then navigate to one of:
        https://claude.ai/new
        https://chatgpt.com
        https://gemini.google.com
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    Browser,
    Page,
    Playwright,
    async_playwright,
)

from orchestrator.providers.base import BaseProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-site configuration
# ---------------------------------------------------------------------------

SITE_CONFIGS: dict[str, dict] = {
    "claude": {
        "url_pattern": "claude.ai",
        # ProseMirror editor used by Claude.ai
        "input_selectors": [
            'div[contenteditable="true"].ProseMirror',
            'div[contenteditable="true"]',
        ],
        "send_selectors": [
            'button[aria-label*="Send"]',
            'button[aria-label*="send"]',
        ],
        "stop_selectors": [
            'button[aria-label*="Stop"]',
            'button[aria-label*="stop"]',
        ],
        "response_selectors": [
            '[data-testid^="conversation-turn"]',
            '.font-claude-message',
        ],
    },
    "chatgpt": {
        "url_pattern": "chatgpt.com",
        "input_selectors": [
            "div#prompt-textarea",
            'div[contenteditable="true"]',
        ],
        "send_selectors": [
            'button[data-testid="send-button"]',
            'button[aria-label*="Send"]',
        ],
        "stop_selectors": [
            'button[data-testid="stop-button"]',
            'button[aria-label*="Stop"]',
        ],
        "response_selectors": [
            "div.markdown",
            "article div.prose",
        ],
    },
    "gemini": {
        "url_pattern": "gemini.google.com",
        "input_selectors": [
            "rich-textarea div[contenteditable]",
            'div[contenteditable="true"]',
        ],
        "send_selectors": [
            'button[aria-label*="Send message"]',
            'button[aria-label*="Send"]',
        ],
        "stop_selectors": [
            'button[aria-label*="Stop"]',
            'button[aria-label*="stop"]',
        ],
        "response_selectors": [
            "model-response",
            ".response-content",
        ],
    },
}

def _to_list(value) -> list[str]:
    """Normalise a selector value from config: string → [string], list → list."""
    if not value:
        return []
    if isinstance(value, list):
        return [str(s) for s in value if s]
    return [str(value)]


CDP_URL = "http://localhost:9222"
# Seconds to wait for a new response to appear after sending
RESPONSE_WAIT_S = 90
# Seconds to wait for generation to complete after response appears
GENERATION_WAIT_S = 180


class BrowserProvider(BaseProvider):
    """
    Provider that drives a locally-open browser chat session.
    No API key required — the logged-in browser session handles auth.

    Session persistence:
        Chrome's user data directory defaults to <PROJECT_ROOT>/sessions/chrome_data.
        This preserves cookies, localStorage, and login sessions across restarts.
        Override via config.yaml → browser_provider.user_data_dir.

    Headless mode (browser_headless: true):
        Kim auto-launches Chromium via Playwright using the persistent session
        directory.  No visible browser window.  Requires a prior visible login
        so that session cookies exist in the data directory.
    """

    def __init__(self, config: dict):
        bp_cfg = config.get("browser_provider", {})
        cdp_url = bp_cfg.get("cdp_url", CDP_URL)
        self._cdp_url = cdp_url
        self._max_history_messages = int(bp_cfg.get("max_history_messages", 6))
        self._max_inject_chars = int(bp_cfg.get("max_inject_chars", 12000))
        self._headless = bool(bp_cfg.get("browser_headless", False))

        # Track Playwright-managed browser for auto-launch mode
        self._managed_pw = None      # Playwright context manager
        self._managed_browser = None  # Browser instance we launched ourselves

        # ── Persistent session directory ────────────────────────────────
        project_root = Path(
            os.environ.get("PROJECT_ROOT")
            or config.get("project_root", str(Path.cwd()))
        ).resolve()
        default_data_dir = str(project_root / "sessions" / "chrome_data")
        self._user_data_dir = str(
            Path(bp_cfg.get("user_data_dir", default_data_dir)).resolve()
        )
        # Ensure the directory exists
        Path(self._user_data_dir).mkdir(parents=True, exist_ok=True)
        logger.info(
            f"BrowserProvider: session dir = {self._user_data_dir}  "
            f"headless = {self._headless}"
        )

        # Merge user-defined custom sites from config.yaml into the lookup table.
        # Each entry must have url_pattern + at least input/send/response selectors.
        # Selectors can be a string or a list; strings are wrapped in a list.
        self._site_configs = dict(SITE_CONFIGS)  # copy so class-level dict is untouched
        for site_key, site_def in (config.get("custom_sites") or {}).items():
            if not site_def.get("url_pattern"):
                logger.warning(f"custom_sites.{site_key}: missing url_pattern, skipping")
                continue
            self._site_configs[site_key] = {
                "url_pattern": site_def["url_pattern"],
                "input_selectors":    _to_list(site_def.get("input_selectors") or site_def.get("input_selector", "")),
                "send_selectors":     _to_list(site_def.get("send_selectors")   or site_def.get("send_button", "")),
                "stop_selectors":     _to_list(site_def.get("stop_selectors")   or site_def.get("stop_button", "")),
                "response_selectors": _to_list(site_def.get("response_selectors") or site_def.get("response_selector", "")),
            }
            logger.info(f"Registered custom site: {site_key!r} → {site_def['url_pattern']!r}")

        logger.info(f"BrowserProvider: cdp_url={cdp_url}  sites={list(self._site_configs)}")

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
    ) -> dict:
        """
        Formats the full context as a single text block, injects it into the
        active AI chat tab, waits for the response, and parses it.
        """
        prompt = self._format_prompt(messages, tools, system)
        logger.debug(f"Injecting {len(prompt)} chars into browser")

        try:
            async with async_playwright() as pw:
                browser = await self._connect(pw)
                page, site = await self._find_chat_page(browser)
                if page is None or site is None:
                    return {
                        "type": "text",
                        "content": (
                            "NEED_HELP: No AI chat tab found. "
                            "Please open Claude, ChatGPT, or Gemini in Chrome "
                            f"(launched with --remote-debugging-port=9222). "
                            f"Detected pages: {await self._list_pages(browser)}"
                        ),
                    }
                cfg = SITE_CONFIGS[site]
                raw_response = await self._send_and_wait(page, cfg, prompt)
                return self._parse_response(raw_response)
        except Exception as e:
            logger.error(f"BrowserProvider.complete failed: {e}", exc_info=True)
            return {"type": "text", "content": f"BROWSER_ERROR: {e}"}

    # ------------------------------------------------------------------
    # CDP connection / headless auto-launch
    # ------------------------------------------------------------------

    async def _connect(self, pw: Playwright) -> Browser:
        """
        Two-tier connection strategy:
          1. Try connecting to an externally-launched Chrome via CDP.
          2. If that fails AND headless mode is enabled, auto-launch
             Chromium via Playwright with the persistent session dir.
        """
        # ── Tier 1: Try CDP connection to user-launched Chrome ────────────
        try:
            browser = await pw.chromium.connect_over_cdp(self._cdp_url)
            logger.info(f"Connected to Chrome at {self._cdp_url} (external)")
            return browser
        except Exception as cdp_error:
            logger.debug(f"CDP connection failed: {cdp_error}")

        # ── Tier 2: Auto-launch headless if enabled ──────────────────────
        if self._headless:
            logger.info("CDP unavailable — auto-launching headless Chromium")
            return await self._auto_launch(pw)

        # ── Neither worked: give the user actionable instructions ────────
        import platform
        sys_name = platform.system()
        if sys_name == "Darwin":
            launch_cmd = (
                '/Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome '
                f'--remote-debugging-port=9222 --user-data-dir="{self._user_data_dir}"'
            )
        elif sys_name == "Linux":
            launch_cmd = (
                f'google-chrome --remote-debugging-port=9222 '
                f'--user-data-dir="{self._user_data_dir}"'
            )
        else:
            launch_cmd = (
                f'chrome.exe --remote-debugging-port=9222 '
                f'--user-data-dir="{self._user_data_dir}"'
            )
        raise ConnectionError(
            f"Cannot connect to Chrome at {self._cdp_url}.\n"
            f"\n"
            f"Option A — Launch Chrome manually (for initial login):\n"
            f"  {launch_cmd}\n"
            f"\n"
            f"Option B — Enable headless mode (after first login):\n"
            f"  Set browser_headless: true in config.yaml\n"
            f"\n"
            f"Session data: {self._user_data_dir}"
        )

    async def _auto_launch(self, pw: Playwright) -> Browser:
        """
        Launch a Playwright-managed Chromium instance in headless mode,
        reusing the persistent session directory for saved cookies.

        This provides a completely invisible browser that preserves login
        state from a previous visible session.
        """
        session_path = Path(self._user_data_dir)
        if not any(session_path.iterdir()):
            raise RuntimeError(
                f"Headless mode requires a prior login session, but the session "
                f"directory is empty: {self._user_data_dir}\n"
                f"Run once with browser_headless: false, log into your AI chat, "
                f"then switch to headless mode."
            )

        logger.info(
            f"Launching headless Chromium with session dir: {self._user_data_dir}"
        )

        # launch_persistent_context gives us a BrowserContext directly;
        # we use its .browser reference for consistency with CDP mode.
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=self._user_data_dir,
            headless=True,
            # Chrome args that improve headless stability
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-popup-blocking",
            ],
            viewport={"width": 1280, "height": 900},
            # Persist cookies/localStorage by using the user_data_dir
            ignore_default_args=["--enable-automation"],
        )

        # Navigate to a known chat URL if no pages exist yet
        if not context.pages:
            page = await context.new_page()
            await page.goto("https://claude.ai/new", wait_until="domcontentloaded")
            await asyncio.sleep(2)  # Let the page settle
            logger.info("Opened default chat page: claude.ai/new")

        # The caller expects a Browser object for _find_chat_page.
        # launch_persistent_context returns a BrowserContext, so we
        # store it and adapt our page-finding to work with it.
        self._managed_context = context
        logger.info(
            f"Headless Chromium ready — {len(context.pages)} page(s) loaded"
        )

        # Return the browser that owns this context (may be None for
        # persistent contexts — we handle this in _find_chat_page)
        return context.browser or context  # type: ignore[return-value]

    async def _list_pages(self, browser: Browser) -> list[str]:
        pages = []
        for ctx in browser.contexts:
            for page in ctx.pages:
                pages.append(page.url)
        return pages

    async def _find_chat_page(self, browser) -> tuple[Optional[Page], Optional[str]]:
        """
        Scan all open pages and return (page, site_key) for the first matching
        chat tab.  Custom sites (from config.yaml) are checked before the
        hardcoded built-in ones so they can override defaults.

        Works with both CDP-connected Browser objects and Playwright-managed
        BrowserContext objects (headless mode).
        """
        # Check custom sites first, then built-ins
        ordered = [
            (k, v) for k, v in self._site_configs.items() if k not in SITE_CONFIGS
        ] + [
            (k, v) for k, v in self._site_configs.items() if k in SITE_CONFIGS
        ]

        # Collect all pages from either Browser or BrowserContext
        all_pages = []
        if hasattr(browser, 'contexts'):
            # Standard Browser object (CDP mode)
            for ctx in browser.contexts:
                all_pages.extend(ctx.pages)
        elif hasattr(browser, 'pages'):
            # BrowserContext object (headless persistent mode)
            all_pages.extend(browser.pages)

        for page in all_pages:
            url = page.url
            for site_key, cfg in ordered:
                if cfg["url_pattern"] in url:
                    logger.info(f"Found {site_key} tab: {url}")
                    return page, site_key
        return None, None

    # ------------------------------------------------------------------
    # Injection + wait + scrape
    # ------------------------------------------------------------------

    async def _send_and_wait(self, page: Page, cfg: dict, message: str) -> str:
        # Count current responses before sending
        response_sel = cfg["response_selectors"][0]
        initial_count = await page.locator(response_sel).count()
        logger.debug(f"Response count before send: {initial_count}")

        # Focus and clear the input box
        input_sel = await self._find_selector(page, cfg["input_selectors"])
        if not input_sel:
            raise RuntimeError("Could not locate chat input box")

        await self._inject_text(page, input_sel, message)
        await asyncio.sleep(0.3)  # Brief settle before clicking send

        # Click the send button
        send_sel = await self._find_selector(page, cfg["send_selectors"])
        if not send_sel:
            # Try pressing Enter as fallback
            logger.warning("Send button not found; pressing Enter")
            await page.keyboard.press("Enter")
        else:
            await page.click(send_sel)

        logger.info("Message sent, waiting for response...")

        # Wait for response count to increase (generation started)
        started = await self._wait_for_new_response(page, response_sel, initial_count)
        if not started:
            raise TimeoutError(f"No new response appeared after {RESPONSE_WAIT_S}s")

        # Wait for generation to finish (stop button disappears)
        await self._wait_for_generation_complete(page, cfg["stop_selectors"])

        # Extra settle time
        await asyncio.sleep(1.5)

        # Scrape the last response
        return await self._scrape_last_response(page, cfg["response_selectors"])

    async def _find_selector(self, page: Page, selectors: list[str]) -> Optional[str]:
        """Return the first selector from the list that matches at least one element."""
        for sel in selectors:
            try:
                count = await page.locator(sel).count()
                if count > 0:
                    return sel
            except Exception:
                continue
        return None

    async def _inject_text(self, page: Page, selector: str, text: str) -> None:
        """
        Clear the contenteditable/textarea element and insert the given text via
        execCommand (works on ProseMirror, draft-js, and plain contenteditable).
        Falls back to Playwright type() for plain <textarea> elements.
        """
        await page.evaluate(
            """([selector, text]) => {
                const el = document.querySelector(selector);
                if (!el) return false;
                el.focus();
                // Select all existing content
                const range = document.createRange();
                range.selectNodeContents(el);
                const sel = window.getSelection();
                sel.removeAllRanges();
                sel.addRange(range);
                // Replace with new text using execCommand (triggers framework events)
                document.execCommand('delete', false, null);
                document.execCommand('insertText', false, text);
                return true;
            }""",
            [selector, text],
        )

    async def _wait_for_new_response(
        self, page: Page, response_sel: str, initial_count: int
    ) -> bool:
        """Poll until the response element count increases."""
        deadline = asyncio.get_event_loop().time() + RESPONSE_WAIT_S
        while asyncio.get_event_loop().time() < deadline:
            count = await page.locator(response_sel).count()
            if count > initial_count:
                logger.debug(f"Response element count: {initial_count} → {count}")
                return True
            await asyncio.sleep(0.5)
        return False

    async def _wait_for_generation_complete(
        self, page: Page, stop_selectors: list[str]
    ) -> None:
        """
        Wait until all known stop-button selectors are invisible (generation
        finished) or until GENERATION_WAIT_S seconds elapse.
        """
        deadline = asyncio.get_event_loop().time() + GENERATION_WAIT_S
        while asyncio.get_event_loop().time() < deadline:
            any_stop_visible = False
            for sel in stop_selectors:
                try:
                    if await page.locator(sel).is_visible():
                        any_stop_visible = True
                        break
                except Exception:
                    pass
            if not any_stop_visible:
                logger.debug("Generation complete (stop button gone)")
                return
            await asyncio.sleep(0.75)
        logger.warning(f"Generation did not complete after {GENERATION_WAIT_S}s — scraping anyway")

    async def _scrape_last_response(self, page: Page, response_selectors: list[str]) -> str:
        """Return inner text of the last response element."""
        for sel in response_selectors:
            try:
                elements = await page.locator(sel).all()
                if elements:
                    text = await elements[-1].inner_text()
                    logger.debug(f"Scraped {len(text)} chars from {sel}")
                    return text.strip()
            except Exception:
                continue
        raise RuntimeError("Could not scrape response from any known selector")

    # ------------------------------------------------------------------
    # Prompt formatting
    # ------------------------------------------------------------------

    def _format_prompt(
        self, messages: list[dict], tools: list[dict], system: str
    ) -> str:
        """
        Compose the full context as a single text block.  Images are omitted
        (text-only injection).  History is trimmed to the most recent
        `max_history_messages` turns.
        """
        # Compact tool list
        compact_tools = [
            {"name": t["name"], "description": t.get("description", ""),
             "args": list(t.get("parameters", {}).get("properties", {}).keys())}
            for t in tools
        ]
        tools_json = json.dumps(compact_tools, indent=2)

        # History — text only, last N messages
        tail = messages[-self._max_history_messages:]
        history_lines = []
        for msg in tail:
            role_label = "User" if msg["role"] == "user" else "Assistant"
            content = msg["content"]
            if isinstance(content, list):
                text_parts = [
                    item["text"] for item in content
                    if item.get("type") == "text" and item.get("text")
                ]
                text = "\n".join(text_parts)
            else:
                text = str(content)
            if text.strip():
                history_lines.append(f"{role_label}: {text.strip()}")

        history_block = "\n\n".join(history_lines)

        prompt = (
            f"[SYSTEM]\n{system}\n\n"
            f"[AVAILABLE TOOLS]\n{tools_json}\n\n"
            f"[CONVERSATION HISTORY]\n{history_block}\n\n"
            "[INSTRUCTIONS]\n"
            "Respond with EXACTLY ONE of:\n"
            '1. A JSON tool call on a single line: {"tool": "<name>", "args": {<args>}}\n'
            "2. TASK_COMPLETE: <one-line summary>\n"
            "3. NEED_HELP: <reason you cannot proceed>\n"
            "Do NOT include markdown formatting around the JSON."
        )

        # Trim if too long
        if len(prompt) > self._max_inject_chars:
            trim_at = self._max_inject_chars - 200
            prompt = prompt[:trim_at] + "\n...[context trimmed]\n" + prompt[-200:]

        return prompt

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(self, text: str) -> dict:
        """
        Parse the scraped DOM text into the canonical response format.
        Handles:
            - TASK_COMPLETE: / NEED_HELP:  → {"type": "text", ...}
            - ```json {...} ```             → {"type": "tool_call", ...}
            - bare JSON {"tool": ...}       → {"type": "tool_call", ...}
        """
        text = text.strip()

        # Explicit completion/help signals
        for prefix in ("TASK_COMPLETE:", "NEED_HELP:"):
            if text.startswith(prefix) or f"\n{prefix}" in text:
                # Extract the relevant line
                for line in text.splitlines():
                    line = line.strip()
                    if line.startswith(prefix):
                        return {"type": "text", "content": line}

        # JSON in fenced code block
        for pattern in [
            r"```(?:json)?\s*(\{.*?\})\s*```",
            r"`(\{[^`]+\})`",
        ]:
            m = re.search(pattern, text, re.DOTALL)
            if m:
                parsed = self._try_parse_tool_json(m.group(1))
                if parsed:
                    return parsed

        # Bare JSON object scan (outermost braces only)
        parsed = self._scan_for_json(text)
        if parsed:
            return parsed

        return {"type": "text", "content": text}

    def _try_parse_tool_json(self, s: str) -> Optional[dict]:
        try:
            data = json.loads(s.strip())
            if isinstance(data, dict) and "tool" in data:
                return {
                    "type": "tool_call",
                    "tool": data["tool"],
                    "args": data.get("args", data.get("arguments", {})),
                }
        except (json.JSONDecodeError, KeyError):
            pass
        return None

    def _scan_for_json(self, text: str) -> Optional[dict]:
        """Find the first balanced JSON object in text that has a 'tool' key."""
        depth = 0
        start = -1
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start >= 0:
                    candidate = text[start: i + 1]
                    parsed = self._try_parse_tool_json(candidate)
                    if parsed:
                        return parsed
                    start = -1
        return None

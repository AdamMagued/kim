"""
Browser provider — 100% API-key-free LLM access via Playwright CDP.

Connects to an existing Chrome session (remote debugging port 9222), finds the
active AI chat tab (Claude, ChatGPT, or Gemini), injects the full context as a
single text message, waits for generation to finish, scrapes the response, and
parses it into the canonical {"type": "tool_call"|"text", ...} format.

Multimodal support:
    Screenshots from the conversation history are decoded from base64, written
    to a temporary ``temp_screenshot.png`` file, and uploaded via the site's
    upload button + Playwright file chooser (or a hidden ``<input type="file">``)
    BEFORE the text prompt is pasted.  The temp file is cleaned up in a
    ``finally`` block after the message has been sent.

Text injection:
    Uses clipboard-paste (``navigator.clipboard.writeText`` + Cmd/Ctrl+V)
    instead of the deprecated ``document.execCommand('insertText')``, which
    truncates at newlines in contenteditable editors (ProseMirror, Gemini's
    rich-textarea, etc.).

Popup handling:
    After uploading an image, a ``_dismiss_popups`` sweep clicks through any
    one-time consent dialogs (Gemini "I agree" / "Got it" / "Continue") so
    they don't block the Send button.

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
        /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\
            --remote-debugging-port=9222 --user-data-dir="<project>/sessions/chrome_data"

    Linux:
        google-chrome --remote-debugging-port=9222 --user-data-dir="<project>/sessions/chrome_data"

    Then navigate to one of:
        https://claude.ai/new
        https://chatgpt.com
        https://gemini.google.com
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    Browser,
    Page,
    Playwright,
    async_playwright,
)

from orchestrator.context_meter import IMAGE_TOKEN_ESTIMATE, estimate_text_tokens
from orchestrator.providers.base import BaseProvider
from orchestrator.providers.browser.bridge_client import complete_via_webview_bridge
from orchestrator.providers.browser.prompt_builder import format_prompt
from orchestrator.providers.browser.response_parser import parse_response, strip_transport_markers
from orchestrator.providers.browser.site_configs import (
    CDP_URL,
    GENERATION_WAIT_S,
    MOD_KEY,
    RESPONSE_WAIT_S,
    SITE_CONFIGS,
    _INJECT_MAX_RETRIES,
    _POPUP_DISMISS_LABELS,
    _VERIFY_MIN_CHARS,
    to_list,
)

logger = logging.getLogger(__name__)

# Re-export for backward compatibility (tests access these via the provider instance)
_MOD_KEY = MOD_KEY
_to_list = to_list


class BrowserProvider(BaseProvider):
    """
    Provider that drives a locally-open browser chat session.
    No API key required — the logged-in browser session handles auth.

    Session persistence:
        Chrome's user data directory defaults to <PROJECT_ROOT>/sessions/chrome_data.
        This preserves cookies, localStorage, and login sessions across restarts.
        Override via config.yaml -> browser_provider.user_data_dir.

    Headless mode (browser_headless: true):
        Kim auto-launches Chromium via Playwright using the persistent session
        directory.  No visible browser window.  Requires a prior visible login
        so that session cookies exist in the data directory.
    """

    def __init__(self, config: dict):
        self._config = config
        bp_cfg = config.get("browser_provider", {})
        cdp_url = bp_cfg.get("cdp_url", CDP_URL)
        self._cdp_url = cdp_url
        self._max_history_messages = int(bp_cfg.get("max_history_messages", 6))
        self._max_inject_chars = int(bp_cfg.get("max_inject_chars", 120000))
        self._headless = bool(bp_cfg.get("browser_headless", False))
        self._force_headless = bool(bp_cfg.get("browser_force_headless", False))
        if self._force_headless:
            self._headless = True
        self._preferred_site = (bp_cfg.get("preferred_site") or "").strip().lower() or None

        env_site = os.environ.get("KIM_PREFERRED_SITE", "").strip().lower()
        if env_site:
            self._preferred_site = env_site

        self._model_tier = None
        if self._preferred_site and ":" in self._preferred_site:
            parts = self._preferred_site.split(":", 1)
            self._preferred_site = parts[0]
            self._model_tier = parts[1]

        self._bridge_url = os.environ.get("KIM_WEBVIEW_BRIDGE_URL", "").strip().rstrip("/")
        self._bridge_token = os.environ.get("KIM_WEBVIEW_BRIDGE_TOKEN", "").strip()
        self._use_webview_bridge = bool(self._bridge_url and self._bridge_token)
        self._gemini_authuser = self._parse_authuser_env(os.environ.get("KIM_GEMINI_AUTHUSER", ""))
        if self._gemini_authuser is None:
            self._gemini_authuser = self._load_active_gemini_authuser_from_account()

        self._managed_pw = None
        self._managed_browser = None

        # ── Persistent session directory ────────────────────────────────
        project_root = Path(
            os.environ.get("PROJECT_ROOT")
            or config.get("project_root", str(Path.cwd()))
        ).resolve()
        default_data_dir = str(project_root / "sessions" / "chrome_data")
        self._user_data_dir = str(
            Path(bp_cfg.get("user_data_dir", default_data_dir)).resolve()
        )
        self._project_root = project_root
        Path(self._user_data_dir).mkdir(parents=True, exist_ok=True)
        logger.info(
            f"BrowserProvider: session dir = {self._user_data_dir}  "
            f"headless = {self._headless}  preferred_site = {self._preferred_site!r} "
            f"in_app_bridge = {self._use_webview_bridge}"
        )

        self._sent_system_prompt = False
        self._last_chat_page_url: Optional[str] = None
        self._last_chat_site: Optional[str] = None
        self._load_site_configs()

    def _load_site_configs(self) -> None:
        self._site_configs = dict(SITE_CONFIGS)
        custom_sites_cfg = self._config.get("custom_sites")
        if not isinstance(custom_sites_cfg, dict):
            custom_sites_cfg = {}
        for site_key, site_def in custom_sites_cfg.items():
            if not isinstance(site_def, dict) or not site_def.get("url_pattern"):
                logger.warning(f"custom_sites.{site_key}: missing url_pattern or not a dict, skipping")
                continue
            self._site_configs[site_key] = {
                "url_pattern": site_def["url_pattern"],
                "input_selectors": to_list(
                    site_def.get("input_selectors") or site_def.get("input_selector", "")
                ),
                "send_selectors": to_list(
                    site_def.get("send_selectors") or site_def.get("send_button", "")
                ),
                "stop_selectors": to_list(
                    site_def.get("stop_selectors") or site_def.get("stop_button", "")
                ),
                "response_selectors": to_list(
                    site_def.get("response_selectors") or site_def.get("response_selector", "")
                ),
                "upload_button_selectors": to_list(
                    site_def.get("upload_button_selectors")
                    or site_def.get("upload_button", "")
                ),
            }
            logger.info(f"Registered custom site: {site_key!r} -> {site_def['url_pattern']!r}")

        logger.info(
            f"BrowserProvider: cdp_url={self._cdp_url}  sites={list(self._site_configs)}"
        )

    def reset_session(self) -> None:
        pass

    def _estimate_prompt_usage(self, prompt: str, attachments: list[dict]) -> dict:
        image_count = sum(
            1 for a in attachments
            if str(a.get("mime_type", "")).startswith("image/") and a.get("data_base64")
        )
        return {
            "input": estimate_text_tokens(prompt) + image_count * IMAGE_TOKEN_ESTIMATE,
            "output": 0,
            "estimated": True,
            "source": "browser_prompt",
        }

    @staticmethod
    def _attach_usage(result: dict, usage: dict) -> dict:
        if not isinstance(result, dict):
            return result
        if "usage" not in result:
            result = dict(result)
            result["usage"] = usage
        return result

    # ==================================================================
    # Main entry point
    # ==================================================================

    @staticmethod
    def _parse_authuser_env(raw: str) -> Optional[int]:
        raw = str(raw or "").strip()
        if not raw:
            return None
        try:
            value = int(raw, 10)
        except ValueError:
            logger.warning("Ignoring invalid KIM_GEMINI_AUTHUSER=%r", raw)
            return None
        if value < 0:
            logger.warning("Ignoring negative KIM_GEMINI_AUTHUSER=%r", raw)
            return None
        return value

    @staticmethod
    def _kim_account_path() -> Path:
        override = os.environ.get("KIM_ACCOUNT_PATH", "").strip()
        if override:
            return Path(override).expanduser()

        home = Path.home()
        system = platform.system()
        if system == "Darwin":
            return home / "Library" / "Application Support" / "kim" / "account.json"
        if system == "Windows":
            appdata = os.environ.get("APPDATA", "").strip()
            if appdata:
                return Path(appdata) / "kim" / "account.json"
        config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
        base = Path(config_home).expanduser() if config_home else home / ".config"
        return base / "kim" / "account.json"

    def _load_active_gemini_authuser_from_account(self) -> Optional[int]:
        path = self._kim_account_path()
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Could not read Kim account file for Gemini authuser: %s", e)
            return None
        accounts = raw.get("google_accounts")
        if not isinstance(accounts, list) or not accounts:
            return None
        active_email = str(raw.get("google_active_account") or "").strip().lower()
        selected = None
        for entry in accounts:
            if not isinstance(entry, dict):
                continue
            email = str(entry.get("email") or "").strip().lower()
            if active_email and email == active_email:
                selected = entry
                break
            if selected is None:
                selected = entry
        if not isinstance(selected, dict):
            return None
        return self._parse_authuser_env(str(selected.get("authuser_index", "")))

    async def complete(  # type: ignore[override]
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str = "",
        clear_chat: bool = False,
        **kwargs,
    ) -> dict:
        # Optimize prompt payload: if resuming an existing thread (url restored),
        # the system prompt was already sent on the first turn of this session.
        restore_status = os.environ.get("KIM_BROWSER_RESTORE_STATUS", "").strip().lower()
        sent_sys = self._sent_system_prompt
        if restore_status == "stored_thread" and len(messages) > 1:
            sent_sys = True

        prompt, attachments, completion_hash, new_sent = format_prompt(
            messages,
            tools or [],
            system,
            sent_system_prompt=sent_sys,
            max_inject_chars=self._max_inject_chars,
            use_webview_bridge=self._use_webview_bridge,
        )
        self._sent_system_prompt = new_sent

        estimated_usage = self._estimate_prompt_usage(prompt, attachments)
        logger.debug(
            f"Prompt ready: {len(prompt)} chars, "
            f"{len(attachments)} attachment(s) extracted, hash={completion_hash}"
        )

        if self._use_webview_bridge:
            result = await complete_via_webview_bridge(
                bridge_url=self._bridge_url,
                bridge_token=self._bridge_token,
                preferred_site=self._preferred_site,
                model_tier=self._model_tier,
                gemini_authuser=self._gemini_authuser,
                prompt=prompt,
                attachments=attachments,
                completion_hash=completion_hash,
                clear_chat=clear_chat,
                site_configs=getattr(self, "_site_configs", None),
            )
            return self._attach_usage(result, estimated_usage)

        try:
            async with async_playwright() as pw:
                browser = await self._connect(pw)
                page, site = await self._find_chat_page(browser)
                if page is None or site is None:
                    if self._last_chat_site:
                        logger.warning(
                            f"Lost reference to {self._last_chat_site} tab "
                            f"(was at {self._last_chat_page_url!r}). Retrying page scan once…"
                        )
                        await asyncio.sleep(1)
                        page, site = await self._find_chat_page(browser)

                if page is None or site is None:
                    return {
                        "type": "text",
                        "content": (
                            "NEED_HELP: Kim lost the active browser chat during this task. "
                            "I will not open a new provider tab because that would lose the LLM context. "
                            "Please reopen the existing provider chat window and resend."
                        ),
                    }

                if clear_chat:
                    logger.info(f"Clearing chat context by reloading {page.url}...")
                    await page.goto(page.url, wait_until="domcontentloaded")
                    await asyncio.sleep(2.0)
                    self._sent_system_prompt = False

                cfg = self._site_configs[site]

                image_attachments = [
                    a for a in attachments
                    if str(a.get("mime_type", "")).startswith("image/") and a.get("data_base64")
                ]

                if image_attachments:
                    logger.info(f"[STATUS] Uploading screenshot to {site}…")
                    await self._inject_image_clipboard(
                        page, cfg, str(image_attachments[-1]["data_base64"])
                    )
                    await page.wait_for_timeout(1200)

                logger.info(f"[STATUS] Preparing {site}…")
                await self._dismiss_popups(page)

                logger.info(f"[STATUS] Sending message to {site}…")
                raw_response = await self._send_and_wait(page, cfg, prompt, site, completion_hash)
                return self._attach_usage(
                    parse_response(raw_response, completion_hash),
                    estimated_usage,
                )
        except Exception as e:
            logger.error(f"BrowserProvider.complete failed: {e}", exc_info=True)
            return self._attach_usage(
                {"type": "text", "content": f"NEED_HELP: Browser connection failed — {e}"},
                estimated_usage,
            )

    # ==================================================================
    # Backward-compatible method wrappers (used by tests)
    # ==================================================================

    def _format_prompt(self, messages, tools, system):
        prompt, attachments, completion_hash, new_sent = format_prompt(
            messages,
            tools,
            system,
            sent_system_prompt=self._sent_system_prompt,
            max_inject_chars=self._max_inject_chars,
            use_webview_bridge=self._use_webview_bridge,
        )
        self._sent_system_prompt = new_sent
        return prompt, attachments, completion_hash

    def _parse_response(self, text, completion_hash):
        return parse_response(text, completion_hash)

    def _strip_transport_markers(self, text, completion_hash):
        return strip_transport_markers(text, completion_hash)

    # ==================================================================
    # CDP connection / headless auto-launch
    # ==================================================================

    async def _connect(self, pw: Playwright) -> Browser:
        if self._force_headless:
            logger.info("browser_force_headless=true — bypassing CDP, launching headless directly")
            return await self._auto_launch(pw)

        try:
            browser = await pw.chromium.connect_over_cdp(self._cdp_url)
            logger.info(f"Connected to Chrome at {self._cdp_url} (external)")
            return browser
        except Exception as cdp_error:
            logger.debug(f"CDP connection failed: {cdp_error}")

        if self._headless:
            logger.info("CDP unavailable — auto-launching headless Chromium")
            return await self._auto_launch(pw)

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
        session_path = Path(self._user_data_dir)
        if not any(session_path.iterdir()):
            if not self._force_headless:
                raise RuntimeError(
                    f"Headless mode requires a prior login session, but the session "
                    f"directory is empty: {self._user_data_dir}\n"
                    f"Run once with browser_headless: false, log into your AI chat, "
                    f"then switch to headless mode."
                )
            logger.info(
                "force_headless: launching with empty user_data_dir — "
                "sign-in must happen via the Tauri webview first."
            )

        logger.info(
            f"Launching headless Chromium with session dir: {self._user_data_dir}"
        )

        context = await pw.chromium.launch_persistent_context(
            user_data_dir=self._user_data_dir,
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-popup-blocking",
            ],
            viewport={"width": 1280, "height": 900},
            ignore_default_args=["--enable-automation"],
        )

        if not context.pages:
            if not self._force_headless:
                raise RuntimeError(
                    "NEED_HELP: No existing provider chat page was found in the saved "
                    "browser session. Kim will not open a new provider tab because "
                    "that would lose the LLM context. Reopen the existing provider "
                    "chat window and resend."
                )
            await context.new_page()

        self._managed_context = context
        logger.info(
            f"Headless Chromium ready — {len(context.pages)} page(s) loaded"
        )
        return context  # type: ignore[return-value]

    async def _list_pages(self, browser) -> list[str]:
        pages: list[str] = []
        if hasattr(browser, 'contexts'):
            for ctx in browser.contexts:
                for page in ctx.pages:
                    pages.append(page.url)
        elif hasattr(browser, 'pages'):
            for page in browser.pages:
                pages.append(page.url)
        return pages

    async def _find_chat_page(self, browser) -> tuple[Optional[Page], Optional[str]]:
        ordered = [
            (k, v) for k, v in self._site_configs.items() if k not in SITE_CONFIGS
        ] + [
            (k, v) for k, v in self._site_configs.items() if k in SITE_CONFIGS
        ]

        all_pages = []
        if hasattr(browser, 'contexts'):
            for ctx in browser.contexts:
                all_pages.extend(ctx.pages)
        elif hasattr(browser, 'pages'):
            all_pages.extend(browser.pages)

        if self._preferred_site:
            ordered = [(k, v) for k, v in ordered if k == self._preferred_site] + [
                (k, v) for k, v in ordered if k != self._preferred_site
            ]

        matches: list[tuple[Page, str]] = []
        for page in all_pages:
            url = page.url
            for site_key, cfg in ordered:
                if cfg["url_pattern"] in url:
                    matches.append((page, site_key))
                    break

        if not matches:
            return None, None

        if self._last_chat_site:
            same_site_matches = [
                (p, sk) for p, sk in matches if sk == self._last_chat_site
            ]
            if same_site_matches:
                if self._last_chat_page_url:
                    for page, site_key in same_site_matches:
                        if page.url == self._last_chat_page_url:
                            logger.info(f"Reusing exact tab for {site_key}: {page.url}")
                            return page, site_key
                page, site_key = same_site_matches[0]
                if page.url != self._last_chat_page_url:
                    logger.info(
                        f"Tab URL changed for {site_key}: "
                        f"{self._last_chat_page_url!r} -> {page.url!r} (same site)"
                    )
                    self._maybe_reset_system_prompt(page.url)
                self._last_chat_page_url = page.url
                return page, site_key

        focused_matches: list[tuple[Page, str]] = []
        for page, site_key in matches:
            try:
                has_focus = await page.evaluate("() => document.hasFocus()")
                if has_focus:
                    focused_matches.append((page, site_key))
            except Exception:
                continue

        if focused_matches:
            page, site_key = focused_matches[0]
            self._maybe_reset_system_prompt(page.url)
            self._last_chat_page_url = page.url
            self._last_chat_site = site_key
            logger.info(f"Using focused {site_key} tab: {page.url}")
            return page, site_key

        if self._preferred_site:
            for page, site_key in matches:
                if site_key == self._preferred_site:
                    self._maybe_reset_system_prompt(page.url)
                    self._last_chat_page_url = page.url
                    self._last_chat_site = site_key
                    logger.info(f"Found preferred {site_key} tab: {page.url}")
                    return page, site_key

        page, site_key = matches[0]
        self._maybe_reset_system_prompt(page.url)
        self._last_chat_page_url = page.url
        self._last_chat_site = site_key
        logger.info(f"Found {site_key} tab: {page.url}")
        return page, site_key

    @staticmethod
    def _extract_conversation_id(url: str) -> str:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.path or "/"

    def _maybe_reset_system_prompt(self, new_url: str) -> None:
        if not self._last_chat_page_url:
            return
        if self._last_chat_page_url == new_url:
            return

        old_site = self._last_chat_site
        new_site = None
        site_configs = getattr(self, "_site_configs", SITE_CONFIGS)
        for site_key, cfg in site_configs.items():
            if cfg["url_pattern"] in new_url:
                new_site = site_key
                break

        if old_site and new_site and old_site == new_site:
            old_conv = self._extract_conversation_id(self._last_chat_page_url)
            new_conv = self._extract_conversation_id(new_url)
            if old_conv != new_conv:
                logger.info(
                    f"Conversation changed within {old_site}: "
                    f"{old_conv!r} -> {new_conv!r}, will re-inject system prompt."
                )
                self._sent_system_prompt = False
            else:
                logger.debug(
                    f"URL changed within same site {old_site}: "
                    f"{self._last_chat_page_url!r} -> {new_url!r} (keeping system prompt)"
                )
            return

        logger.info(
            f"Chat site changed ({old_site!r} -> {new_site!r}), "
            "will re-inject system prompt."
        )
        self._sent_system_prompt = False

    # ==================================================================
    # Popup dismissal
    # ==================================================================

    async def _dismiss_popups(self, page: Page) -> None:
        for i in range(3):
            try:
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.3)
            except Exception:
                break
        logger.debug("Escape-key sweep complete (3 presses)")
        for label in _POPUP_DISMISS_LABELS:
            try:
                btn = page.locator(
                    f"xpath=//button[contains(translate(., "
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                    f"'abcdefghijklmnopqrstuvwxyz'), "
                    f"'{label.lower()}')]"
                )
                if await btn.count() > 0:
                    first = btn.first
                    try:
                        await first.wait_for(state="visible", timeout=1500)
                        await first.click()
                        logger.info(f"Dismissed popup button: {label!r}")
                        await asyncio.sleep(0.5)
                    except Exception:
                        pass
            except Exception as e:
                logger.debug(f"Popup check for {label!r} failed: {e}")
                continue

        try:
            dialog_btns = page.locator(
                '[role="dialog"] button, '
                '[role="alertdialog"] button, '
                '.modal button, '
                '.dialog button'
            )
            count = await dialog_btns.count()
            for i in range(count):
                btn = dialog_btns.nth(i)
                try:
                    text = (await btn.inner_text()).strip().lower()
                    if text in {"i agree", "got it", "continue", "accept", "ok", "dismiss"}:
                        await btn.click()
                        logger.info(f"Dismissed dialog button: {text!r}")
                        await asyncio.sleep(0.5)
                        break
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"Generic dialog dismiss sweep failed: {e}")

    # ==================================================================
    # Screenshot upload
    # ==================================================================

    async def _inject_image_clipboard(
        self, page: Page, cfg: dict, image_b64: str
    ) -> None:
        if not image_b64.startswith("data:"):
            data_uri = f"data:image/png;base64,{image_b64}"
        else:
            data_uri = image_b64

        input_sel = await self._find_selector(page, cfg["input_selectors"])
        if not input_sel:
            logger.warning("Cannot paste image — editor not found")
            return

        await page.click(input_sel)
        await asyncio.sleep(0.2)

        try:
            await page.evaluate(
                """async (dataUri) => {
                    const res  = await fetch(dataUri);
                    const blob = await res.blob();
                    const item = new ClipboardItem({ [blob.type]: blob });
                    await navigator.clipboard.write([item]);
                }""",
                data_uri,
            )
            await asyncio.sleep(0.2)
            await page.keyboard.press(f"{MOD_KEY}+v")
            logger.info("Screenshot pasted into editor via clipboard")
        except Exception as e:
            logger.warning(f"Clipboard image injection failed: {e}")

    # ==================================================================
    # Text injection via clipboard paste
    # ==================================================================

    async def _inject_text(self, page: Page, selector: str, text: str) -> None:
        for attempt in range(1, _INJECT_MAX_RETRIES + 1):
            await page.click(selector)
            await asyncio.sleep(0.2)

            await page.keyboard.press(f"{MOD_KEY}+a")
            await asyncio.sleep(0.1)
            await page.keyboard.press("Backspace")
            await asyncio.sleep(0.1)

            try:
                await page.evaluate(
                    """async (text) => {
                        await navigator.clipboard.writeText(text);
                    }""",
                    text,
                )
                await asyncio.sleep(0.1)
                await page.keyboard.press(f"{MOD_KEY}+v")
                await asyncio.sleep(0.5)

                if await self._verify_injection(page, selector, text):
                    logger.info(
                        f"Text injected via clipboard paste (attempt {attempt})"
                    )
                    return
            except Exception as e:
                logger.debug(
                    f"navigator.clipboard.writeText failed ({e}), "
                    "trying ClipboardEvent fallback"
                )

            await page.click(selector)
            await page.keyboard.press(f"{MOD_KEY}+a")
            await page.keyboard.press("Backspace")
            await asyncio.sleep(0.1)

            await page.evaluate(
                """([selector, text]) => {
                    const el = document.querySelector(selector);
                    if (!el) return;
                    el.focus();
                    const dt = new DataTransfer();
                    dt.setData('text/plain', text);
                    const event = new ClipboardEvent('paste', {
                        clipboardData: dt,
                        bubbles: true,
                        cancelable: true,
                    });
                    el.dispatchEvent(event);
                }""",
                [selector, text],
            )
            await asyncio.sleep(0.5)

            if await self._verify_injection(page, selector, text):
                logger.info(
                    f"Text injected via ClipboardEvent (attempt {attempt})"
                )
                return

            logger.debug("ClipboardEvent fallback failed, using DOM setter")
            await page.click(selector)
            await page.keyboard.press(f"{MOD_KEY}+a")
            await page.keyboard.press("Backspace")
            await asyncio.sleep(0.1)

            await page.evaluate(
                """([selector, text]) => {
                    const el = document.querySelector(selector);
                    if (!el) return false;
                    el.focus();
                    if ('value' in el) {
                        const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLTextAreaElement.prototype, "value")?.set;
                        if (nativeInputValueSetter) {
                            nativeInputValueSetter.call(el, text);
                        } else {
                            el.value = text;
                        }
                    } else {
                        el.textContent = text;
                    }
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }""",
                [selector, text],
            )
            await asyncio.sleep(0.5)

            if await self._verify_injection(page, selector, text):
                logger.info(
                    f"Text injected via DOM setter (attempt {attempt})"
                )
                return

            logger.warning(
                f"Injection verification failed "
                f"(attempt {attempt}/{_INJECT_MAX_RETRIES}), retrying…"
            )
            await asyncio.sleep(0.3)

        msg = (
            f"Text injection verification failed after {_INJECT_MAX_RETRIES} "
            "attempts. Refusing to send an incomplete prompt."
        )
        logger.error(msg)
        raise RuntimeError(msg)

    async def _read_editor_text(self, page: Page, selector: str) -> str:
        try:
            value = await page.evaluate(
                """(selector) => {
                    const el = document.querySelector(selector);
                    if (!el) return '';
                    return String(el.value ?? el.innerText ?? el.textContent ?? '');
                }""",
                selector,
            )
            return value if isinstance(value, str) else ""
        except Exception as e:
            logger.debug(f"Editor readback error: {e}")
            return ""

    async def _verify_injection(self, page: Page, selector: str, expected: str) -> bool:
        import re as _re
        actual = await self._read_editor_text(page, selector)

        def normalize_typo(s: str) -> str:
            return (
                " ".join(s.split())
                .replace("“", '"').replace("”", '"')
                .replace("‘", "'").replace("’", "'")
                .replace("—", "--").replace("–", "--")
                .replace("…", "...")
            )

        expected_norm = normalize_typo(expected)
        actual_norm = normalize_typo(actual)

        logger.debug(
            f"Injection verify: editor has {len(actual)} chars; "
            f"expected {len(expected)} chars"
        )

        if len(expected_norm) < _VERIFY_MIN_CHARS:
            return actual_norm == expected_norm

        if len(actual_norm) < max(_VERIFY_MIN_CHARS, int(len(expected_norm) * 0.98)):
            logger.warning(
                f"Injection failed: actual length {len(actual_norm)} is < 98% of expected {len(expected_norm)}"
            )
            return False

        def fuzzy_match(a: str, b: str) -> bool:
            return _re.sub(r'\W+', '', a) == _re.sub(r'\W+', '', b)

        if not fuzzy_match(actual_norm[:200], expected_norm[:200]):
            logger.warning(
                f"Injection failed: Prefix mismatch. Actual: {actual_norm[:50]}... Expected: {expected_norm[:50]}..."
            )
            return False

        if not fuzzy_match(actual_norm[-200:], expected_norm[-200:]):
            logger.warning(
                f"Injection failed: Suffix mismatch. Actual: ...{actual_norm[-50:]} Expected: ...{expected_norm[-50:]}"
            )
            return False

        return True

    # ==================================================================
    # Send + wait + scrape
    # ==================================================================

    async def _send_and_wait(
        self, page: Page, cfg: dict, message: str, site: str = "AI", completion_hash: str = ""
    ) -> str:
        response_sel = await self._find_selector(page, cfg["response_selectors"])
        if not response_sel:
            response_sel = cfg["response_selectors"][0]
        initial_count = await page.locator(response_sel).count()
        logger.debug(f"Response count before send: {initial_count}")

        await page.keyboard.press("Escape")
        await asyncio.sleep(0.2)

        input_sel = await self._find_selector(page, cfg["input_selectors"])
        if not input_sel:
            raise RuntimeError("Could not locate chat input box")

        await self._inject_text(page, input_sel, message)
        await asyncio.sleep(0.3)
        if not await self._verify_injection(page, input_sel, message):
            raise RuntimeError("Prompt changed after injection; refusing to send a partial prompt")

        send_sel = await self._find_selector(page, cfg.get("send_selectors", []))
        if send_sel:
            await page.locator(send_sel).first.click()
            logger.debug("Submitted via send button click")
        else:
            for key in ["Enter", "Meta+Enter", "Control+Enter"]:
                await page.keyboard.press(key)
                await asyncio.sleep(0.3)
                try:
                    remaining = await self._read_editor_text(page, input_sel)
                    if len(remaining.strip()) < len(message) * 0.1:
                        break
                except Exception:
                    break
            logger.debug("Submitted via keyboard Enter fallback")

        logger.info(f"[STATUS] Waiting for {site} to respond…")
        logger.info("Message sent, waiting for response…")

        started = await self._wait_for_new_response(
            page, response_sel, initial_count
        )
        if not started:
            raise TimeoutError(
                f"No new response appeared after {RESPONSE_WAIT_S}s"
            )

        new_count = await page.locator(response_sel).count()
        new_element_index = max(new_count - 1, 0)

        logger.info(f"[STATUS] {site} is responding…")

        await self._wait_for_generation_complete(
            page,
            cfg["stop_selectors"],
            cfg["response_selectors"],
            completion_hash,
            site,
            min_index=new_element_index
        )

        await asyncio.sleep(1.5)

        logger.info(f"[STATUS] Reading {site}'s response…")
        return await self._scrape_last_response(page, cfg["response_selectors"], min_index=new_element_index)

    async def _find_selector(
        self, page: Page, selectors: list[str]
    ) -> Optional[str]:
        for sel in selectors:
            try:
                loc = page.locator(sel)
                count = await loc.count()
                for i in range(count):
                    try:
                        if await loc.nth(i).is_visible():
                            return sel
                    except Exception:
                        pass
            except Exception:
                continue
        return None

    async def _wait_for_new_response(
        self, page: Page, response_sel: str, initial_count: int
    ) -> bool:
        deadline = asyncio.get_running_loop().time() + RESPONSE_WAIT_S
        while asyncio.get_running_loop().time() < deadline:
            count = await page.locator(response_sel).count()
            if count > initial_count:
                logger.debug(
                    f"Response element count: {initial_count} -> {count}"
                )
                return True
            await asyncio.sleep(0.5)
        return False

    async def _wait_for_generation_complete(
        self, page: Page, stop_selectors: list[str], response_selectors: list[str],
        completion_hash: Optional[str], site: str = "AI", min_index: int = 0
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + GENERATION_WAIT_S
        last_status = loop.time()
        min_generation_time = loop.time() + 5.0
        elapsed = 0

        last_text_len = 0
        idle_count = 0

        while loop.time() < deadline:
            current_text = ""
            try:
                current_text = await self._scrape_last_response(page, response_selectors, min_index=min_index)
                logger.debug(
                    f"[DEBUG] _wait_for_generation_complete text (len={len(current_text)}): {current_text[-100:]!r}"
                )
                if completion_hash and completion_hash in current_text:
                    logger.debug("Generation complete (completion hash found)")
                    return
            except Exception as e:
                logger.debug(f"[DEBUG] _scrape_last_response failed: {e}")

            if len(current_text) > last_text_len:
                idle_count = 0
                last_text_len = len(current_text)
            elif len(current_text) > 0 and len(current_text) == last_text_len:
                idle_count += 1

            any_stop_visible = False
            for sel in stop_selectors:
                try:
                    loc = page.locator(sel)
                    count = await loc.count()
                    for i in range(count):
                        try:
                            if await loc.nth(i).is_visible():
                                any_stop_visible = True
                                break
                        except Exception:
                            pass
                    if any_stop_visible:
                        break
                except Exception:
                    pass

            if any_stop_visible:
                idle_count = 0
            elif idle_count > 8 and loop.time() > min_generation_time:
                if completion_hash and completion_hash not in current_text:
                    logger.warning("Generation complete (stop button hidden) but completion hash missing!")
                else:
                    logger.debug("Generation complete (stop button hidden & text settled)")
                return
            now = loop.time()
            if now - last_status >= 3:
                elapsed = int(now - (deadline - GENERATION_WAIT_S))
                if any_stop_visible:
                    logger.info(f"[STATUS] {site} is generating… ({elapsed}s, {len(current_text)} chars)")
                else:
                    logger.info(f"[STATUS] Waiting for {site} to finish… ({elapsed}s)")
                last_status = now
            await asyncio.sleep(0.75)
        logger.warning(
            f"Generation did not complete after {GENERATION_WAIT_S}s "
            "— scraping anyway"
        )

    async def _scrape_last_response(
        self, page: Page, response_selectors: list[str], min_index: int = 0
    ) -> str:
        for sel in response_selectors:
            try:
                elements = await page.locator(sel).all()
                candidates = elements[min_index:] if min_index < len(elements) else []
                for el in reversed(candidates):
                    text = await el.inner_text()
                    if not text or not text.strip():
                        text = await el.text_content()
                    if text and text.strip():
                        logger.debug(f"Scraped {len(text)} chars from {sel}")
                        return text.strip()
            except Exception:
                continue
        raise RuntimeError(
            "Could not scrape response from any known selector"
        )

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

import asyncio
import json
import logging
import os
import platform
import re
import uuid
from pathlib import Path
from typing import Optional

import httpx
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
        "upload_button_selectors": [
            'button[aria-label*="Attach"]',
            'button[aria-label*="attach"]',
            'button[aria-label*="Upload"]',
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
        "upload_button_selectors": [
            'button[aria-label*="Attach"]',
            'button[aria-label*="attach"]',
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
        "upload_button_selectors": [
            'button[aria-label*="Upload"]',
            'button[aria-label*="upload"]',
            'button[aria-label*="Add image"]',
            'button[aria-label*="add image"]',
        ],
    },
    "deepseek": {
        "url_pattern": "chat.deepseek.com",
        "input_selectors": [
            "textarea#chat-input",
            "textarea",
        ],
        # DeepSeek send controls vary; Enter fallback in _send_and_wait handles misses.
        "send_selectors": [
            'button[aria-label*="Send"]',
            'button[type="submit"]',
        ],
        "stop_selectors": [
            'button[aria-label*="Stop"]',
            'div[role="button"][class*="stop"]',
        ],
        "response_selectors": [
            "div.ds-markdown",
        ],
        "upload_button_selectors": [
            'button[aria-label*="Upload"]',
            'button[aria-label*="Attach"]',
        ],
    },
    "grok": {
        "url_pattern": "grok.com",
        "input_selectors": [
            "textarea",
            'div[contenteditable="true"]',
        ],
        "send_selectors": [
            'button[aria-label*="Send"]',
            'button[type="submit"]',
        ],
        "stop_selectors": [
            'button[aria-label*="Stop"]',
        ],
        "response_selectors": [
            "article",
            "div.markdown",
            '[data-testid*="message"]',
        ],
        "upload_button_selectors": [
            'button[aria-label*="Upload"]',
            'button[aria-label*="Attach"]',
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


# Modifier key: Cmd on Mac, Ctrl everywhere else
_MOD_KEY = "Meta" if platform.system() == "Darwin" else "Control"

CDP_URL = "http://localhost:9222"
# Seconds to wait for a new response to appear after sending
RESPONSE_WAIT_S = 90
# Seconds to wait for generation to complete after response appears
GENERATION_WAIT_S = 180
# Minimum chars expected in editor after paste, for verification
_VERIFY_MIN_CHARS = 20
# Maximum retries for clipboard paste injection
_INJECT_MAX_RETRIES = 3
# Timeout for in-app webview bridge completion calls
_BRIDGE_TIMEOUT_S = 720

# Button labels that indicate a dismissible popup / consent dialog
_POPUP_DISMISS_LABELS = [
    "I agree",
    "Got it",
    "Continue",
    "Accept",
    "OK",
    "Dismiss",
    "Close",
    "No thanks",
]


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
        self._config = config
        bp_cfg = config.get("browser_provider", {})
        cdp_url = bp_cfg.get("cdp_url", CDP_URL)
        self._cdp_url = cdp_url
        self._max_history_messages = int(bp_cfg.get("max_history_messages", 6))
        self._max_inject_chars = int(bp_cfg.get("max_inject_chars", 60000))
        self._headless = bool(bp_cfg.get("browser_headless", False))
        self._preferred_site = (bp_cfg.get("preferred_site") or "").strip().lower() or None
        
        env_site = os.environ.get("KIM_PREFERRED_SITE", "").strip().lower()
        if env_site:
            self._preferred_site = env_site
            
        self._bridge_url = os.environ.get("KIM_WEBVIEW_BRIDGE_URL", "").strip().rstrip("/")
        self._bridge_token = os.environ.get("KIM_WEBVIEW_BRIDGE_TOKEN", "").strip()
        self._use_webview_bridge = bool(self._bridge_url and self._bridge_token)

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
        self._project_root = project_root
        # Ensure the directory exists
        Path(self._user_data_dir).mkdir(parents=True, exist_ok=True)
        logger.info(
            f"BrowserProvider: session dir = {self._user_data_dir}  "
            f"headless = {self._headless}  preferred_site = {self._preferred_site!r} "
            f"in_app_bridge = {self._use_webview_bridge}"
        )

        # Stateful flag — tracks whether we've already sent the system
        # prompt + tool list to the chat UI in this session.  Subsequent
        # calls only send the latest delta message.
        self._sent_system_prompt = False
        # Sticky tab preference to keep the loop anchored to one chat tab.
        self._last_chat_page_url: Optional[str] = None
        self._last_chat_site: Optional[str] = None

    def reset_session(self) -> None:
        """Call before each new task to force system prompt re-injection.
        Without this, reusing the same BrowserProvider instance for multiple
        tasks skips the system prompt on the second task and beyond.
        """
        self._sent_system_prompt = False
        self._last_chat_page_url = None
        self._last_chat_site = None

        # Merge user-defined custom sites from config.yaml into the lookup table.
        # Each entry must have url_pattern + at least input/send/response selectors.
        # Selectors can be a string or a list; strings are wrapped in a list.
        self._site_configs = dict(SITE_CONFIGS)  # copy so class-level dict is untouched
        for site_key, site_def in (self._config.get("custom_sites") or {}).items():
            if not site_def.get("url_pattern"):
                logger.warning(f"custom_sites.{site_key}: missing url_pattern, skipping")
                continue
            self._site_configs[site_key] = {
                "url_pattern": site_def["url_pattern"],
                "input_selectors": _to_list(
                    site_def.get("input_selectors") or site_def.get("input_selector", "")
                ),
                "send_selectors": _to_list(
                    site_def.get("send_selectors") or site_def.get("send_button", "")
                ),
                "stop_selectors": _to_list(
                    site_def.get("stop_selectors") or site_def.get("stop_button", "")
                ),
                "response_selectors": _to_list(
                    site_def.get("response_selectors") or site_def.get("response_selector", "")
                ),
                "upload_button_selectors": _to_list(
                    site_def.get("upload_button_selectors")
                    or site_def.get("upload_button", "")
                ),
            }
            logger.info(f"Registered custom site: {site_key!r} → {site_def['url_pattern']!r}")

        logger.info(
            f"BrowserProvider: cdp_url={self._cdp_url}  sites={list(self._site_configs)}"
        )

    # ==================================================================
    # Main entry point
    # ==================================================================

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
    ) -> dict:
        """
        Pure-clipboard approach — no temp files.

        1. Build a text-only prompt (base64 blobs extracted, not embedded).
        2. If a screenshot was extracted, inject it into the browser
           clipboard as an image blob and paste it into the editor.
        3. Dismiss any privacy/consent popups.
        4. Paste the cleaned text prompt via clipboard.
        5. Click Send and wait for the AI response.
        """
        prompt, attachments, completion_hash = self._format_prompt(messages, tools, system)
        logger.debug(
            f"Prompt ready: {len(prompt)} chars, "
            f"{len(attachments)} attachment(s) extracted, hash={completion_hash}"
        )

        if self._use_webview_bridge:
            return await self._complete_via_webview_bridge(prompt, attachments, completion_hash)

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
                cfg = self._site_configs[site]

                image_attachments = [
                    a for a in attachments
                    if str(a.get("mime_type", "")).startswith("image/") and a.get("data_base64")
                ]

                # ── Step 1: Paste screenshot via clipboard (if any) ──────
                if image_attachments:
                    print(f"[STATUS] Uploading screenshot to {site}…", flush=True)
                    await self._inject_image_clipboard(
                        page, cfg, str(image_attachments[-1]["data_base64"])
                    )

                    # ── Step 2: Let the UI render the image ──────────────
                    await page.wait_for_timeout(5000)

                # ── Step 3: Always dismiss popups before every send ───────
                # Gemini (and other sites) show consent dialogs / marketing
                # overlays that block the input. We press Escape + click
                # known dismiss buttons regardless of whether an image was sent.
                print(f"[STATUS] Preparing {site}…", flush=True)
                await self._dismiss_popups(page)

                # ── Step 4: Paste cleaned text prompt ────────────────────
                # ── Step 5: Click Send + wait for response ───────────────
                print(f"[STATUS] Sending message to {site}…", flush=True)
                raw_response = await self._send_and_wait(page, cfg, prompt, site)
                return self._parse_response(raw_response, completion_hash)
        except Exception as e:
            logger.error(f"BrowserProvider.complete failed: {e}", exc_info=True)
            return {"type": "text", "content": f"NEED_HELP: Browser connection failed — {e}"}

    async def _complete_via_webview_bridge(
        self,
        prompt: str,
        attachments: list[dict],
        completion_hash: str,
    ) -> dict:
        """Run completion through Kim desktop's in-app webview bridge.

        Uses the split send/result API for instant send confirmation:
          1. POST /v1/send → get req_id (~150ms)
          2. GET /v1/result/{req_id} → long-poll for response

        Falls back to monolithic POST /v1/complete if /v1/send returns 404
        (backward compat with older Rust binaries).
        """
        if not self._bridge_url or not self._bridge_token:
            return {
                "type": "text",
                "content": "NEED_HELP: In-app browser bridge is not configured.",
            }

        site = self._preferred_site or "claude"
        known_sites = getattr(self, "_site_configs", SITE_CONFIGS)
        if site not in known_sites:
            site = "claude"

        bridge_attachments: list[dict] = []
        max_attachments = 8
        max_attachment_bytes = 10 * 1024 * 1024
        for i, attachment in enumerate(attachments[:max_attachments], start=1):
            data_b64 = str(attachment.get("data_base64", "")).strip()
            mime_type = str(attachment.get("mime_type", "application/octet-stream")).strip()
            if not data_b64:
                continue
            approx_size = (len(data_b64) * 3) // 4
            if approx_size > max_attachment_bytes:
                logger.warning(
                    f"Skipping oversized bridge attachment #{i} ({approx_size} bytes, {mime_type})"
                )
                continue
            bridge_attachments.append(
                {
                    "name": str(attachment.get("name", f"attachment_{i}")),
                    "mime_type": mime_type,
                    "data_base64": data_b64,
                }
            )

        if len(attachments) > max_attachments:
            prompt = (
                f"{prompt}\n\n"
                f"[Kim note: {len(attachments) - max_attachments} additional attachment(s) "
                "were omitted due to attachment limit.]"
            )

        headers = {"X-Kim-Token": self._bridge_token}
        payload = {
            "site": site,
            "prompt": prompt,
            "attachments": bridge_attachments,
            "completion_hash": completion_hash,
        }

        # ── Try split send/result API first ──────────────────────────────
        try:
            async with httpx.AsyncClient(timeout=30) as send_client:
                send_resp = await send_client.post(
                    f"{self._bridge_url}/v1/send",
                    headers=headers,
                    json=payload,
                )

            if send_resp.status_code == 404:
                # Old Rust binary — fall back to monolithic /v1/complete
                logger.info("Bridge /v1/send returned 404, falling back to /v1/complete")
                return await self._complete_via_webview_bridge_legacy(
                    prompt, headers, payload, completion_hash
                )

            try:
                send_data = send_resp.json()
            except ValueError:
                body_preview = send_resp.text[:300]
                return {
                    "type": "text",
                    "content": (
                        "NEED_HELP: Bridge /v1/send returned invalid JSON "
                        f"(status {send_resp.status_code}): {body_preview}"
                    ),
                }

            if send_resp.status_code == 409:
                msg = send_data.get("error") or (
                    "Kim opened the in-app browser window. Sign in and resend your task."
                )
                return {"type": "text", "content": f"NEED_HELP: {msg}"}

            if send_resp.status_code >= 400:
                msg = send_data.get("error") or f"HTTP {send_resp.status_code}"
                return {
                    "type": "text",
                    "content": f"NEED_HELP: In-app browser bridge send error — {msg}",
                }

            req_id = send_data.get("req_id")
            if not req_id:
                return {
                    "type": "text",
                    "content": "NEED_HELP: Bridge /v1/send did not return a req_id.",
                }

            sent_confirmed = send_data.get("sent_confirmed", False)
            logger.info(
                f"Bridge send OK: req_id={req_id}, site={send_data.get('site')}, "
                f"confirmed={sent_confirmed}"
            )

        except httpx.ReadTimeout:
            logger.warning("Bridge /v1/send timed out, falling back to /v1/complete")
            return await self._complete_via_webview_bridge_legacy(
                prompt, headers, payload, completion_hash
            )
        except Exception as e:
            logger.warning(f"Bridge /v1/send failed ({e}), falling back to /v1/complete")
            return await self._complete_via_webview_bridge_legacy(
                prompt, headers, payload, completion_hash
            )

        # ── Long-poll for result ─────────────────────────────────────────
        try:
            async with httpx.AsyncClient(timeout=_BRIDGE_TIMEOUT_S) as result_client:
                result_resp = await result_client.get(
                    f"{self._bridge_url}/v1/result/{req_id}",
                    headers=headers,
                )
        except httpx.ReadTimeout as e:
            logger.error("Bridge /v1/result timed out", exc_info=True)
            detail = str(e).strip() or "Timed out waiting for provider response"
            return {
                "type": "text",
                "content": f"NEED_HELP: In-app browser bridge timeout — {detail}",
            }
        except Exception as e:
            logger.error(f"Bridge /v1/result failed: {e}", exc_info=True)
            detail = str(e).strip() or e.__class__.__name__
            return {
                "type": "text",
                "content": f"NEED_HELP: In-app browser bridge result poll failed — {detail}",
            }

        try:
            data = result_resp.json()
        except ValueError:
            body_preview = result_resp.text[:300]
            return {
                "type": "text",
                "content": (
                    "NEED_HELP: In-app browser bridge returned invalid JSON "
                    f"(status {result_resp.status_code}): {body_preview}"
                ),
            }

        if result_resp.status_code >= 400:
            msg = data.get("error") or f"HTTP {result_resp.status_code}"
            return {
                "type": "text",
                "content": f"NEED_HELP: In-app browser bridge error — {msg}",
            }

        if not data.get("ok", False):
            msg = data.get("error") or "Unknown in-app bridge failure"
            return {
                "type": "text",
                "content": f"NEED_HELP: In-app browser execution failed — {msg}",
            }

        raw_response = data.get("response")
        if not raw_response or not isinstance(raw_response, str) or not raw_response.strip():
            return {
                "type": "text",
                "content": "NEED_HELP: In-app browser bridge returned an empty response.",
            }

        return self._parse_response(raw_response.strip(), completion_hash)

    async def _complete_via_webview_bridge_legacy(
        self,
        prompt: str,
        headers: dict,
        payload: dict,
        completion_hash: str,
    ) -> dict:
        """Monolithic /v1/complete fallback for older Rust binaries."""
        try:
            async with httpx.AsyncClient(timeout=_BRIDGE_TIMEOUT_S) as client:
                resp = await client.post(
                    f"{self._bridge_url}/v1/complete",
                    headers=headers,
                    json=payload,
                )
        except httpx.ReadTimeout as e:
            logger.error("In-app bridge request timed out", exc_info=True)
            detail = str(e).strip() or "Bridge request timed out while waiting for provider response"
            return {
                "type": "text",
                "content": f"NEED_HELP: In-app browser bridge timeout — {detail}",
            }
        except Exception as e:
            logger.error(f"In-app bridge request failed: {e}", exc_info=True)
            detail = str(e).strip() or e.__class__.__name__
            return {
                "type": "text",
                "content": f"NEED_HELP: In-app browser bridge request failed — {detail}",
            }

        try:
            data = resp.json()
        except ValueError:
            body_preview = resp.text[:300]
            return {
                "type": "text",
                "content": (
                    "NEED_HELP: In-app browser bridge returned invalid JSON "
                    f"(status {resp.status_code}): {body_preview}"
                ),
            }

        if resp.status_code == 409:
            msg = data.get("error") or (
                "Kim opened the in-app browser window. Sign in and resend your task."
            )
            return {"type": "text", "content": f"NEED_HELP: {msg}"}

        if resp.status_code >= 400:
            msg = data.get("error") or f"HTTP {resp.status_code}"
            return {
                "type": "text",
                "content": f"NEED_HELP: In-app browser bridge error — {msg}",
            }

        if not data.get("ok", False):
            msg = data.get("error") or "Unknown in-app bridge failure"
            return {
                "type": "text",
                "content": f"NEED_HELP: In-app browser execution failed — {msg}",
            }

        raw_response = data.get("response")
        if not raw_response or not isinstance(raw_response, str) or not raw_response.strip():
            return {
                "type": "text",
                "content": "NEED_HELP: In-app browser bridge returned an empty response.",
            }

        return self._parse_response(raw_response.strip(), completion_hash)

    # ==================================================================
    # CDP connection / headless auto-launch
    # ==================================================================

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

        # launch_persistent_context gives us a BrowserContext with .browser == None.
        # Return the context directly; _list_pages and _find_chat_page handle both types.
        return context  # type: ignore[return-value]

    async def _list_pages(self, browser) -> list[str]:
        """Return URLs of all open pages. Handles both Browser and BrowserContext."""
        pages: list[str] = []
        if hasattr(browser, 'contexts'):
            # Standard Browser (CDP mode)
            for ctx in browser.contexts:
                for page in ctx.pages:
                    pages.append(page.url)
        elif hasattr(browser, 'pages'):
            # BrowserContext (persistent/headless launch)
            for page in browser.pages:
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

        # Prefer desktop / CLI sub-provider (browser:chatgpt → chatgpt tab first).
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

        # Prefer the previously used tab if still available. This prevents
        # hopping between multiple same-site tabs across iterations.
        if self._last_chat_page_url and self._last_chat_site:
            for page, site_key in matches:
                if site_key == self._last_chat_site and page.url == self._last_chat_page_url:
                    logger.info(f"Reusing previous {site_key} tab: {page.url}")
                    return page, site_key

        # Next prefer the currently focused tab among matches.
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
            self._last_chat_page_url = page.url
            self._last_chat_site = site_key
            logger.info(f"Using focused {site_key} tab: {page.url}")
            return page, site_key

        if self._preferred_site:
            for page, site_key in matches:
                if site_key == self._preferred_site:
                    self._last_chat_page_url = page.url
                    self._last_chat_site = site_key
                    logger.info(f"Found preferred {site_key} tab: {page.url}")
                    return page, site_key

        page, site_key = matches[0]
        self._last_chat_page_url = page.url
        self._last_chat_site = site_key
        logger.info(f"Found {site_key} tab: {page.url}")
        return page, site_key

    # ==================================================================
    # Popup dismissal
    # ==================================================================

    async def _dismiss_popups(self, page: Page) -> None:
        """
        Dismiss any one-time consent / privacy popups (e.g. Gemini's
        "I agree" dialog, or promotional overlays like NotebookLM).

        Strategy:
          1. Press Escape a few times to clear any marketing modals or
             overlays that don't have a predictable dismiss button.
          2. Click through known consent buttons ("I agree", "Got it", …).
          3. Sweep generic [role="dialog"] containers as a catch-all.

        Uses a short timeout (1.5 s) per button so this never blocks
        execution when no popup is present.
        """
        # ── Phase 0: Escape-key sweep for marketing overlays ─────────────
        for i in range(3):
            try:
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.3)
            except Exception:
                break
        logger.debug("Escape-key sweep complete (3 presses)")
        for label in _POPUP_DISMISS_LABELS:
            try:
                # Case-insensitive text match via XPath
                btn = page.locator(
                    f"xpath=//button[contains(translate(., "
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                    f"'abcdefghijklmnopqrstuvwxyz'), "
                    f"'{label.lower()}')]"
                )
                # Wait at most 1.5 s for the button to become visible
                if await btn.count() > 0:
                    first = btn.first
                    try:
                        await first.wait_for(state="visible", timeout=1500)
                        await first.click()
                        logger.info(f"Dismissed popup button: {label!r}")
                        await asyncio.sleep(0.5)  # Let the dialog animate away
                    except Exception:
                        # Button exists but not visible/clickable — skip
                        pass
            except Exception as e:
                logger.debug(f"Popup check for {label!r} failed: {e}")
                continue

        # Also try generic dialog dismiss patterns (role="dialog" + confirm)
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
        """
        Inject a base64 PNG screenshot into the editor via the clipboard.

        Uses the Clipboard API to write an image blob, then pastes it
        into the focused editor with Cmd/Ctrl+V.  No temp files needed.
        """
        # Build the full data URI for fetch()
        if not image_b64.startswith("data:"):
            data_uri = f"data:image/png;base64,{image_b64}"
        else:
            data_uri = image_b64

        # Focus the editor first
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
            await page.keyboard.press(f"{_MOD_KEY}+v")
            logger.info("Screenshot pasted into editor via clipboard")
        except Exception as e:
            logger.warning(f"Clipboard image injection failed: {e}")

    # ==================================================================
    # Text injection via clipboard paste
    # ==================================================================

    async def _inject_text(self, page: Page, selector: str, text: str) -> None:
        """
        Inject text into a contenteditable or textarea element using the
        system clipboard.  This avoids the deprecated
        ``execCommand('insertText')`` which truncates at the first newline
        in rich-text editors.

        Steps:
          1. Focus and select-all in the editor to clear existing content.
          2. Write the prompt to the clipboard via
             ``navigator.clipboard.writeText()``.
          3. Press Cmd+V (Mac) or Ctrl+V (other) to paste.
          4. Verify the injection landed (retry up to ``_INJECT_MAX_RETRIES``
             times).

        Falls back to a synthetic ``ClipboardEvent('paste')`` dispatch, and
        finally to ``page.keyboard.type()`` in chunks as a last resort.
        """
        for attempt in range(1, _INJECT_MAX_RETRIES + 1):
            # Focus the editor element
            await page.click(selector)
            await asyncio.sleep(0.2)

            # Select all existing content and delete it
            await page.keyboard.press(f"{_MOD_KEY}+a")
            await asyncio.sleep(0.1)
            await page.keyboard.press("Backspace")
            await asyncio.sleep(0.1)

            # ── Primary: navigator.clipboard.writeText + Cmd/Ctrl+V ──────
            try:
                await page.evaluate(
                    """async (text) => {
                        await navigator.clipboard.writeText(text);
                    }""",
                    text,
                )
                await asyncio.sleep(0.1)
                await page.keyboard.press(f"{_MOD_KEY}+v")
                await asyncio.sleep(0.5)

                if await self._verify_injection(page, selector):
                    logger.info(
                        f"Text injected via clipboard paste (attempt {attempt})"
                    )
                    return
            except Exception as e:
                logger.debug(
                    f"navigator.clipboard.writeText failed ({e}), "
                    "trying ClipboardEvent fallback"
                )

            # ── Fallback A: synthetic ClipboardEvent dispatch ────────────
            await page.click(selector)
            await page.keyboard.press(f"{_MOD_KEY}+a")
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

            if await self._verify_injection(page, selector):
                logger.info(
                    f"Text injected via ClipboardEvent (attempt {attempt})"
                )
                return

            # ── Fallback B: page.keyboard.type() in chunks ───────────────
            logger.debug("ClipboardEvent fallback failed, using keyboard.type()")
            await page.click(selector)
            await page.keyboard.press(f"{_MOD_KEY}+a")
            await page.keyboard.press("Backspace")
            await asyncio.sleep(0.1)

            chunk_size = 500
            for i in range(0, len(text), chunk_size):
                await page.keyboard.type(text[i:i + chunk_size], delay=0)
                await asyncio.sleep(0.05)

            if await self._verify_injection(page, selector):
                logger.info(
                    f"Text injected via keyboard.type() (attempt {attempt})"
                )
                return

            logger.warning(
                f"Injection verification failed "
                f"(attempt {attempt}/{_INJECT_MAX_RETRIES}), retrying…"
            )
            await asyncio.sleep(0.3)

        # All retries exhausted
        logger.error(
            f"Text injection verification failed after {_INJECT_MAX_RETRIES} "
            "attempts. Proceeding — prompt may be incomplete."
        )

    async def _verify_injection(self, page: Page, selector: str) -> bool:
        """
        Check that the editor element contains a meaningful amount of text
        after injection.  Returns True if the editor has at least
        ``_VERIFY_MIN_CHARS`` characters.
        """
        try:
            text_len = await page.evaluate(
                """(selector) => {
                    const el = document.querySelector(selector);
                    if (!el) return 0;
                    return (el.innerText || el.textContent || el.value || '').length;
                }""",
                selector,
            )
            logger.debug(f"Injection verify: editor has {text_len} chars")
            return text_len >= _VERIFY_MIN_CHARS
        except Exception as e:
            logger.debug(f"Injection verify error: {e}")
            return False

    # ==================================================================
    # Send + wait + scrape
    # ==================================================================

    async def _send_and_wait(self, page: Page, cfg: dict, message: str, site: str = "AI") -> str:
        """Inject the prompt, click Send, and wait for the full response."""
        # Count current responses before sending
        response_sel = cfg["response_selectors"][0]
        initial_count = await page.locator(response_sel).count()
        logger.debug(f"Response count before send: {initial_count}")

        # Locate the input box — press Escape first to dismiss any focused popups
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.2)

        input_sel = await self._find_selector(page, cfg["input_selectors"])
        if not input_sel:
            raise RuntimeError("Could not locate chat input box")

        # Inject prompt text via clipboard paste (with verification)
        await self._inject_text(page, input_sel, message)
        await asyncio.sleep(0.3)

        # Click the send button (prefer aria-label="Send message")
        send_sel = await self._find_selector(page, cfg["send_selectors"])
        if send_sel:
            await page.click(send_sel)
        else:
            logger.warning("Send button not found; pressing Enter")
            await page.keyboard.press("Enter")

        print(f"[STATUS] Waiting for {site} to respond…", flush=True)
        logger.info("Message sent, waiting for response…")

        # Wait for response count to increase (generation started)
        started = await self._wait_for_new_response(
            page, response_sel, initial_count
        )
        if not started:
            raise TimeoutError(
                f"No new response appeared after {RESPONSE_WAIT_S}s"
            )

        print(f"[STATUS] {site} is responding…", flush=True)

        # Wait for generation to finish (stop button disappears)
        await self._wait_for_generation_complete(page, cfg["stop_selectors"], site)

        # Extra settle time
        await asyncio.sleep(1.5)

        print(f"[STATUS] Reading {site}'s response…", flush=True)
        # Scrape the last response
        return await self._scrape_last_response(page, cfg["response_selectors"])

    async def _find_selector(
        self, page: Page, selectors: list[str]
    ) -> Optional[str]:
        """Return the first selector from the list that matches ≥1 element."""
        for sel in selectors:
            try:
                if await page.locator(sel).count() > 0:
                    return sel
            except Exception:
                continue
        return None

    async def _wait_for_new_response(
        self, page: Page, response_sel: str, initial_count: int
    ) -> bool:
        """Poll until the response element count increases."""
        deadline = asyncio.get_running_loop().time() + RESPONSE_WAIT_S
        while asyncio.get_running_loop().time() < deadline:
            count = await page.locator(response_sel).count()
            if count > initial_count:
                logger.debug(
                    f"Response element count: {initial_count} → {count}"
                )
                return True
            await asyncio.sleep(0.5)
        return False

    async def _wait_for_generation_complete(
        self, page: Page, stop_selectors: list[str], site: str = "AI"
    ) -> None:
        """
        Wait until all known stop-button selectors are invisible (generation
        finished) or until ``GENERATION_WAIT_S`` seconds elapse.
        Emits a status line every 10 s so the UI shows live progress.
        """
        deadline = asyncio.get_running_loop().time() + GENERATION_WAIT_S
        last_status = asyncio.get_running_loop().time()
        elapsed = 0
        while asyncio.get_running_loop().time() < deadline:
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
            now = asyncio.get_running_loop().time()
            if now - last_status >= 10:
                elapsed = int(now - (deadline - GENERATION_WAIT_S))
                print(f"[STATUS] {site} still thinking… ({elapsed}s)", flush=True)
                last_status = now
            await asyncio.sleep(0.75)
        logger.warning(
            f"Generation did not complete after {GENERATION_WAIT_S}s "
            "— scraping anyway"
        )

    async def _scrape_last_response(
        self, page: Page, response_selectors: list[str]
    ) -> str:
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
        raise RuntimeError(
            "Could not scrape response from any known selector"
        )

    # ==================================================================
    # Prompt formatting
    # ==================================================================

    _DATA_URI_PREFIX = "data:"
    _DATA_URI_BASE64_MARKER = ";base64,"

    @staticmethod
    def _ext_for_mime(mime_type: str) -> str:
        mime_type = (mime_type or "").lower().strip()
        ext_map = {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/jpg": "jpg",
            "image/webp": "webp",
            "image/gif": "gif",
            "image/svg+xml": "svg",
            "application/pdf": "pdf",
            "text/plain": "txt",
            "text/markdown": "md",
            "application/json": "json",
            "application/zip": "zip",
            "application/octet-stream": "bin",
        }
        if mime_type in ext_map:
            return ext_map[mime_type]
        if "/" in mime_type:
            tail = mime_type.split("/")[-1].split("+")[0].strip()
            if tail:
                return tail
        return "bin"

    def _append_attachment(
        self,
        attachments_out: list[dict],
        mime_type: str,
        data_b64: str,
        name: Optional[str] = None,
    ) -> None:
        if not data_b64:
            return
        clean_mime = mime_type.strip().lower() if mime_type else "application/octet-stream"
        if "/" not in clean_mime:
            clean_mime = "application/octet-stream"
        idx = len(attachments_out) + 1
        ext = self._ext_for_mime(clean_mime)
        default_name = (
            f"screenshot_{idx}.{ext}"
            if clean_mime.startswith("image/")
            else f"attachment_{idx}.{ext}"
        )
        attachments_out.append(
            {
                "name": (name or default_name).strip() or default_name,
                "mime_type": clean_mime,
                "data_base64": data_b64,
            }
        )

    def _strip_data_uris(self, text: str, attachments_out: list[dict]) -> str:
        """Extract inline ``data:<mime>;base64,...`` URIs into attachments.

        Uses string scanning (not regex) to avoid catastrophic backtracking on
        very large base64 payloads.
        """
        out_parts: list[str] = []
        i = 0
        prefix = self._DATA_URI_PREFIX
        marker = self._DATA_URI_BASE64_MARKER

        while True:
            start = text.find(prefix, i)
            if start == -1:
                out_parts.append(text[i:])
                break

            marker_pos = text.find(marker, start)
            if marker_pos == -1:
                out_parts.append(text[i:])
                break

            mime_type = text[start + len(prefix):marker_pos].strip().lower()
            payload_start = marker_pos + len(marker)
            end = payload_start
            while end < len(text) and text[end] not in " \t\n\r\"'<>)],;":
                end += 1

            payload = text[payload_start:end]
            if payload and "/" in mime_type:
                out_parts.append(text[i:start])
                self._append_attachment(attachments_out, mime_type, payload)
                if mime_type.startswith("image/"):
                    out_parts.append("[Screenshot attached]")
                else:
                    out_parts.append(f"[Attachment: {mime_type}]")
                i = end
            else:
                # Not a valid data URI payload; advance safely.
                out_parts.append(text[i:start + len(prefix)])
                i = start + len(prefix)

        return "".join(out_parts)

    def _format_prompt(
        self, messages: list[dict], tools: list[dict], system: str
    ) -> tuple[str, list[dict], str]:
        """
        Stateful prompt formatter for browser-based chat UIs.

        Because the web UI (Gemini, Claude, ChatGPT) retains its own
        conversation history, we do **not** replay past messages.  Instead:

        - **First call**: Send ``[SYSTEM]`` + ``[AVAILABLE TOOLS]`` +
          ``[INSTRUCTIONS]`` + the latest user message.
        - **Subsequent calls**: Send **only** the latest message (the
          delta — typically a tool result or a follow-up instruction).

        In both cases, any embedded ``data:<mime>;base64,…`` blobs are
        extracted into attachment records and replaced with placeholders.

        Returns:
            ``(prompt_text, attachments)``
        """
        attachments: list[dict] = []

        # ── Extract the LAST message only (the delta) ────────────────────
        last_text = ""
        if messages:
            last_msg = messages[-1]
            content = last_msg["content"]

            if isinstance(content, list):
                text_parts: list[str] = []
                for item in content:
                    item_type = item.get("type", "")
                    if item_type == "text" and item.get("text"):
                        cleaned = self._strip_data_uris(
                            item["text"], attachments
                        )
                        text_parts.append(cleaned)
                    elif item_type == "image" and item.get("data"):
                        self._append_attachment(
                            attachments,
                            str(item.get("media_type") or "image/png"),
                            str(item["data"]),
                            str(item.get("name") or "").strip() or None,
                        )
                        text_parts.append("[Screenshot attached]")
                    elif item_type in {"file", "document", "attachment"} and item.get("data"):
                        file_name = str(item.get("name") or item.get("filename") or "").strip() or None
                        mime_type = str(
                            item.get("media_type")
                            or item.get("mime_type")
                            or "application/octet-stream"
                        )
                        self._append_attachment(
                            attachments,
                            mime_type,
                            str(item.get("data") or ""),
                            file_name,
                        )
                        if file_name:
                            text_parts.append(f"[Attachment: {file_name}]")
                        else:
                            text_parts.append("[Attachment attached]")
                last_text = "\n".join(text_parts)
            else:
                last_text = self._strip_data_uris(str(content), attachments)

        last_text = last_text.strip()

        completion_hash = f"KIM_{uuid.uuid4().hex[:8]}"

        # ── First message: include system prompt + tools ─────────────────
        if not self._sent_system_prompt:
            compact_tools = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "args": list(
                        t.get("parameters", {}).get("properties", {}).keys()
                    ),
                }
                for t in tools
            ]
            tools_json = json.dumps(compact_tools, indent=2)

            import platform as _platform
            import os as _os
            _sys = _platform.system()
            _home = _os.path.expanduser("~")
            if _sys == "Darwin":
                _os_hint = f"You are running on macOS. Home is {_home}. Use 'open' to launch apps and POSIX paths."
            elif _sys == "Linux":
                _os_hint = f"You are running on Linux. Home is {_home}. Use 'xdg-open' to launch apps and POSIX paths."
            else:
                _os_hint = f"You are running on Windows. Home is {_home}. Use 'start' to launch apps and Windows paths."

            prompt = (
                f"[SYSTEM]\n{system}\n"
                f"{_os_hint}\n\n"
                f"[AVAILABLE TOOLS]\n{tools_json}\n\n"
                "[INSTRUCTIONS]\n"
                "Respond with EXACTLY ONE of:\n"
                '1. A JSON tool call on a single line: '
                '{"tool": "<name>", "args": {<args>}}\n'
                "2. TASK_COMPLETE: <one-line summary>\n"
                "3. NEED_HELP: <reason you cannot proceed>\n"
                "Do NOT include markdown formatting around the JSON.\n"
                "CRITICAL: If your JSON arguments contain double quotes (e.g., HTML attributes or code), you MUST escape them (\\\") so the JSON is valid.\n"
                f"IMPORTANT: Always append the exact string {completion_hash} at the very end of your entire response.\n\n"
                f"{last_text}"
            )
            self._sent_system_prompt = True
        else:
            # ── Subsequent calls: delta only ─────────────────────────────
            prompt = last_text + f"\n\nRemember: append {completion_hash} at the very end of your response."

        # Trim if too long
        if len(prompt) > self._max_inject_chars:
            trim_at = self._max_inject_chars - 200
            prompt = (
                prompt[:trim_at]
                + "\n…[context trimmed]\n"
                + prompt[-200:]
            )

        return prompt, attachments, completion_hash

    # ==================================================================
    # Response parsing
    # ==================================================================

    def _parse_response(self, text: str, completion_hash: str) -> dict:
        """
        Parse the scraped DOM text into the canonical response format.
        Handles:
            - ``TASK_COMPLETE:`` / ``NEED_HELP:``  → ``{"type": "text", …}``
            - fenced ``json`` code blocks           → ``{"type": "tool_call", …}``
            - bare JSON ``{"tool": …}``             → ``{"type": "tool_call", …}``
        """
        # Strip the bridge sentinel before any parsing
        text = text.replace(completion_hash, "").strip()

        # Explicit completion/help signals — use word-boundary search so Gemini's
        # DOM label ("Gemini TASK_COMPLETE: ...") doesn't block detection after
        # normalizeText collapses newlines into spaces.
        for prefix in ("TASK_COMPLETE:", "NEED_HELP:"):
            m = re.search(r"\b" + re.escape(prefix) + r"\s*(.+)$", text, re.IGNORECASE | re.MULTILINE)
            if m:
                return {"type": "text", "content": f"{prefix} {m.group(1).strip()}"}

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
            # Fallback for models that produce unescaped double quotes inside the JSON string
            m_tool = re.search(r'"tool"\s*:\s*"([^"]+)"', s)
            if m_tool:
                tool_name = m_tool.group(1)
                if tool_name == "write_file":
                    m_path = re.search(r'"path"\s*:\s*"([^"]+)"', s)
                    # Extract everything after "content": " up to the closing brace
                    m_content = re.search(r'"content"\s*:\s*"(.*)"\s*\}\s*\}?\s*$', s, re.DOTALL)
                    if m_path and m_content:
                        # Fix up trailing quotes if the regex captured the closing quote of content
                        content = m_content.group(1)
                        if content.endswith('"'):
                            content = content[:-1]
                        return {
                            "type": "tool_call",
                            "tool": tool_name,
                            "args": {
                                "path": m_path.group(1),
                                "content": content
                            }
                        }
                elif tool_name == "run_command":
                    m_cmd = re.search(r'"cmd"\s*:\s*"(.*)"\s*\}\s*\}?\s*$', s, re.DOTALL)
                    if m_cmd:
                        cmd = m_cmd.group(1)
                        if cmd.endswith('"'):
                            cmd = cmd[:-1]
                        return {
                            "type": "tool_call",
                            "tool": "run_command",
                            "args": {"cmd": cmd}
                        }
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

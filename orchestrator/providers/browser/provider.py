"""
Browser provider — 100% API-key-free LLM access via Playwright CDP.

Connects to an existing Chrome session (remote debugging port 9222), finds the
active AI chat tab (Claude, ChatGPT, or Gemini), injects the full context as a
single text message, waits for generation to finish, scrapes the response, and
parses it into the canonical {"type": "tool_call"|"text", ...} format.

Multimodal support:
    Screenshots are decoded from base64 and pasted via the clipboard (or a
    hidden ``<input type="file">``) BEFORE the text prompt, per-image.

Text injection:
    Clipboard-paste (``navigator.clipboard.writeText`` + Cmd/Ctrl+V), not
    the deprecated ``document.execCommand('insertText')``, which truncates
    at newlines in contenteditable editors (ProseMirror, Gemini's textarea).

Popup handling:
    ``_dismiss_popups`` clicks through one-time consent dialogs (Gemini
    "I agree" / "Got it" / "Continue") so they don't block Send.

MODES:
    1. Visible (browser_headless: false)  —  Default / first-time setup.
       User manually launches Chrome with remote debugging and logs in.
       Session cookies are saved to sessions/chrome_data/ for reuse.

    2. Headless (browser_headless: true)  —  Background mode after first login.
       Kim auto-launches Chromium invisibly, reusing the saved session dir.
       Requires a prior visible-mode login so those cookies already exist.

SETUP: with browser_auto_launch (default true), Kim opens Chrome with remote
debugging itself on first use — sign in in that window once. Manual launch
is only needed with browser_auto_launch: false; see _launch_headed_chrome's
ConnectionError message for the exact command for your OS.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import uuid
from pathlib import Path
from typing import Optional, TYPE_CHECKING, cast

import httpx

# Playwright is only needed for the CDP fallback path (the in-app webview
# bridge path needs no playwright at all); imported lazily below so selecting
# a browser provider doesn't hard-crash when playwright isn't installed.
if TYPE_CHECKING:
    from playwright.async_api import Browser, Page, Playwright

from orchestrator.context_meter import IMAGE_TOKEN_ESTIMATE, estimate_text_tokens
from orchestrator.providers.base import BaseProvider
from orchestrator.providers.browser.bridge_client import complete_via_webview_bridge
from orchestrator.providers.browser.gemini_url import apply_gemini_authuser, response_candidates
from orchestrator.providers.browser.gemini_url import collect_selector_baselines
from orchestrator.providers.browser.markdown_scraper import MARKDOWN_SERIALIZER_JS
from orchestrator.providers.browser.page_driver import PageDriver
from orchestrator.providers.browser.popup_matching import EXTRA_POPUP_DISMISS_LABELS, dismiss_popup_label
from orchestrator.providers.browser.prompt_builder import format_prompt
from orchestrator.providers.browser.reasoning_effort import apply_effort, resolve_effort
from orchestrator.providers.browser.response_parser import parse_response, strip_transport_markers
from orchestrator.providers.browser.session_paths import resolve_session_root
from orchestrator.providers.browser.thinking_probe import build_thinking_probe
from orchestrator.providers.browser.turn_timeouts import (
    PhaseTimeout,
    phase_timeout_response,
    phase_timeout_s,
    run_phase,
)
from orchestrator.providers.browser.site_configs import (
    CDP_URL,
    GENERATION_WAIT_S,
    MOD_KEY,
    RESPONSE_WAIT_S,
    SITE_CONFIGS,
    _INJECT_MAX_RETRIES,
    _POPUP_DISMISS_LABELS,
    _VERIFY_MIN_CHARS,
    auth_wall_response,
    detect_auth_wall,
    lost_chat_response,
    to_list,
)

logger = logging.getLogger(__name__)

# Re-export for backward compatibility (tests access these via the provider instance)
_MOD_KEY = MOD_KEY
_to_list = to_list


# F-J-3: where the auto-launched detached CDP Chrome's PID is recorded so the
# desktop shell can reap the orphan on app quit.
_CDP_CHROME_REGISTRY_NAME = ".kim_cdp_chrome.json"


def reap_launched_cdp_chrome(project_root) -> bool:
    """Reap the Kim-launched CDP Chrome recorded under ``project_root`` (F-J-3).

    The browser provider deliberately outlives its own (short-lived) process so
    the signed-in Chrome is reused across turns, so it cannot reap that Chrome
    itself. This reads the PID registry and SIGTERMs the recorded process if it
    is still alive, then clears the registry. Intended to be called by the
    desktop shell on app quit (HANDOFF -> D').

    Returns True when a live process was signalled.
    """
    from pathlib import Path as _Path
    path = _Path(project_root) / "sessions" / _CDP_CHROME_REGISTRY_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    pid = data.get("pid")
    killed = False
    if isinstance(pid, int):
        try:
            os.kill(pid, signal.SIGTERM)
            killed = True
        except (OSError, ProcessLookupError):
            killed = False
    try:
        path.unlink()
    except OSError:
        pass
    return killed


class _DeliveredNoResponse(Exception):
    """Raised when the prompt was already SUBMITTED into the site thread but no
    response appeared in time (F-B-8).

    This must NOT be retried: the message is already in the conversation, so a
    retry (H6 re-raised the old TimeoutError as retryable) re-injects the whole
    prompt with a NEW completion hash into the SAME chat — duplicate prompts,
    interleaved answers, and a new-hash wait that latches onto the first send's
    late reply. complete() converts it into an in-band NEED_HELP instead.
    """

    def __init__(self, site: str, waited_s: int):
        self.site = site
        self.waited_s = waited_s
        super().__init__(f"No response from {site} after {waited_s}s (prompt already delivered)")


def _normalize_for_marker(text: str) -> str:
    """Collapse whitespace and markdown styling characters for marker matching.

    Chat UIs sometimes style the completion sentinel (wrap it in backticks,
    italicize the underscores, split it across a line break), which makes a raw
    substring check miss it and costs a long idle wait. Comparing normalized
    text against the normalized marker survives that styling.
    """
    return re.sub(r"[\s`*_]+", "", text or "")


# F-B-7: the injected prompt's own "always append the exact string …"
# instruction contains the literal hash, so an echoed USER bubble (matched by
# Claude/Grok response selectors too) also contains the sentinel. This
# verbatim signature marks such a scrape as the prompt echo, not the answer.
_MARKER_INSTRUCTION_SIGNATURE = "always append the exact string"

# How many normalized trailing chars beyond the sentinel still count as "at the
# tail". The prompt-echo instruction that follows the hash ("at the very end of
# your entire response…") is far longer than this, so it never sneaks in.
_HASH_TAIL_TOLERANCE = 12


def _hash_at_tail(normalized_text: str, normalized_hash: str) -> bool:
    """True only when the completion sentinel sits at (or within a few chars of)
    the END of the scraped text (F-B-7).

    A sentinel found mid-text is the echoed prompt instruction (or an assistant
    that named the marker before answering), NOT a finished response — accepting
    it there scrapes the user's own prompt or truncates an in-flight answer.
    """
    if not normalized_hash:
        return False
    tail = normalized_text[-(len(normalized_hash) + _HASH_TAIL_TOLERANCE):]
    return normalized_hash in tail


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

    def __init__(self, config: dict, page_driver: Optional[PageDriver] = None):
        self._config = config
        # Injected browser-I/O seam (K3): complete() drives this instead of playwright.
        self._page_driver = page_driver
        bp_cfg = config.get("browser_provider", {})
        cdp_url = bp_cfg.get("cdp_url", CDP_URL)
        self._cdp_url = cdp_url
        # F-B-13: browser_provider.max_history_messages was read here but used
        # nowhere — the recap in prompt_builder.build_history_recap is bounded by
        # CHARACTERS (max_recap), not a message count, so the knob changed
        # nothing. Removed rather than wired, to avoid silently altering recap
        # length for anyone who had set it. (config.yaml key removal + the dead
        # relay: section are config territory — see the HANDOFF in the commit.)
        self._max_inject_chars = int(bp_cfg.get("max_inject_chars", 120000))
        self._headless = bool(bp_cfg.get("browser_headless", False))
        self._force_headless = bool(bp_cfg.get("browser_force_headless", False))
        if self._force_headless:
            self._headless = True
        # When Chrome isn't reachable over CDP in visible mode, launch it for the
        # user (opt-out via browser_auto_launch: false) instead of erroring with
        # manual instructions.  Tracks the spawned process so we don't re-launch.
        self._auto_launch_chrome = bool(bp_cfg.get("browser_auto_launch", True))
        self._chrome_proc: Optional[subprocess.Popen] = None
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
        self._use_webview_bridge = True
        self._gemini_authuser = self._parse_authuser_env(os.environ.get("KIM_GEMINI_AUTHUSER", ""))
        if self._gemini_authuser is None:
            self._gemini_authuser = self._load_active_gemini_authuser_from_account()
        self._effort = resolve_effort(config)

        # ── Persistent session directory ────────────────────────────────
        # F-B-14: anchored to the stable Kim install, not cwd/PROJECT_ROOT (session_paths.py).
        project_root = resolve_session_root(config)
        default_data_dir = str(project_root / "sessions" / "chrome_data")
        self._user_data_dir = str(
            Path(bp_cfg.get("user_data_dir", default_data_dir)).resolve()
        )
        self._project_root = project_root
        _sess_dir = Path(self._user_data_dir)
        _sess_dir.mkdir(parents=True, exist_ok=True)
        # SECURITY (F-I-4): this profile holds live auth cookies for the user's
        # AI logins. Lock it to owner-only (0o700) so other local users can't
        # read the cookie jar. Best-effort — chmod is a no-op/behaves
        # differently on Windows, and a perms hiccup must not abort startup.
        try:
            os.chmod(_sess_dir, 0o700)
        except OSError:
            pass
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
        """Clear per-session state so a fresh task gets a clean system prompt and tab."""
        self._sent_system_prompt = False
        self._last_chat_page_url = None
        self._last_chat_site = None

    def mark_thread_continuation(self) -> None:
        """Agent-driven (stateful threads): the session's live web-chat thread
        already received the system prompt on an earlier task, so this process
        should send only delta messages. Complements the KIM_BROWSER_RESTORE_STATUS
        heuristic, which only covers restored-thread spawns."""
        self._sent_system_prompt = True

    async def start_fresh_chat(self) -> bool:
        """Open a blank provider conversation without sending a message.

        Compaction needs a real rollover at compact time, not a promise to
        clear the old chat on some later user turn.  The compact handoff stays
        in the sidecar and is injected with the next actual task.
        """
        self.reset_session()
        if self._use_webview_bridge:
            if not self._bridge_url or not self._bridge_token:
                return False
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.post(
                        f"{self._bridge_url}/v1/browser/new-chat",
                        headers={"X-Kim-Token": self._bridge_token},
                    )
                return response.status_code < 400 and bool(
                    response.json().get("ok", False)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not open fresh provider chat via bridge: %s", exc)
                return False

        try:
            if self._page_driver is not None:
                page, site = await self._page_driver.acquire()
                if page is None or site is None:
                    return False
                await page.goto(
                    self._fresh_chat_url(site) or page.url,
                    wait_until="domcontentloaded",
                )
                return True

            from playwright.async_api import async_playwright

            async with async_playwright() as pw:
                browser = await self._connect(pw)
                page, site = await self._find_chat_page(browser)
                if page is None or site is None:
                    return False
                await page.goto(
                    self._fresh_chat_url(site) or page.url,
                    wait_until="domcontentloaded",
                )
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not open fresh provider chat after compaction: %s", exc)
            return False

    def _estimate_prompt_usage(self, prompt: str, attachments: list[dict]) -> dict:
        image_count = sum(
            1 for a in attachments
            if str(a.get("mime_type", "")).startswith("image/") and a.get("data_base64")
        )
        return {
            "input": estimate_text_tokens(prompt) + image_count * IMAGE_TOKEN_ESTIMATE,
            "estimated": True,
            "source": "browser_prompt",
        }

    @staticmethod
    def _attach_usage(result: dict, usage: dict) -> dict:
        if not isinstance(result, dict):
            return result
        if "usage" not in result:
            result = dict(result)
            merged_usage = dict(usage)
            merged_usage["output"] = BrowserProvider._estimate_output_usage(result)
            result["usage"] = merged_usage
        return result

    @staticmethod
    def _estimate_output_usage(result: dict) -> int:
        if not isinstance(result, dict):
            return 0
        text_parts: list[str] = []
        content = result.get("content")
        if isinstance(content, str) and content.strip():
            text_parts.append(content)
        extra = result.get("tool") if result.get("type") == "tool_call" else None
        if isinstance(extra, str) and extra.strip():
            text_parts.append(extra)
        if isinstance(result.get("args"), dict) and result["args"]:
            text_parts.append(json.dumps(result["args"], ensure_ascii=False, sort_keys=True))
        joined = "\n".join(part for part in text_parts if part).strip()
        return estimate_text_tokens(joined) if joined else 0

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

    def _commit_sent_system_prompt(self, result: object, new_sent: bool) -> None:
        """H5: record that the system prompt reached the site only AFTER a
        delivered send. A NEED_HELP text result (bridge failure, lost tab,
        auth wall) means nothing was answered — committing early would make
        the retry rebuild the prompt without the system prompt and the
        completion-hash protocol it establishes."""
        if isinstance(result, dict):
            content = str(result.get("content", ""))
            if result.get("type") == "text" and content.startswith("NEED_HELP"):
                return
        self._sent_system_prompt = new_sent

    @staticmethod
    def _needs_chatgpt_contract_repair(
        result: object,
        *,
        preferred_site: Optional[str],
        has_agent_tools: bool,
    ) -> bool:
        """True when ChatGPT chat-mode returned unparseable plain prose.

        Codex bridge calls carry no BrowserProvider tools and use their own
        JSON contract, so this repair is intentionally limited to Kim's normal
        browser-agent route.
        """
        if (preferred_site or "").lower() != "chatgpt" or not has_agent_tools:
            return False
        if not isinstance(result, dict) or result.get("type") != "text":
            return False
        content = str(result.get("content") or "").lstrip().upper()
        return not (
            content.startswith("TASK_COMPLETE:")
            or content.startswith("NEED_HELP:")
        )

    @staticmethod
    def _needs_chatgpt_capability_repair(
        result: object,
        *,
        preferred_site: Optional[str],
        has_agent_tools: bool,
    ) -> bool:
        """Detect ChatGPT falsely denying Kim's supplied local tools."""
        if (preferred_site or "").lower() != "chatgpt" or not has_agent_tools:
            return False
        if not isinstance(result, dict) or result.get("type") != "text":
            return False
        content = str(result.get("content") or "").lower()
        if not content.lstrip().startswith("need_help:"):
            return False
        denial_phrases = (
            "don't have access",
            "do not have access",
            "no access to",
            "cannot access the local",
            "can't access the local",
            "no tools available",
            "upload the repository",
            "upload the files",
        )
        return any(phrase in content for phrase in denial_phrases)

    async def complete(  # type: ignore[override]
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str = "",
        clear_chat: bool = False,
        handoff: Optional[str] = None,
        page_driver: Optional[PageDriver] = None,
        **kwargs,
    ) -> dict:
        # Optimize prompt payload: if resuming an existing thread (url restored),
        # the system prompt was already sent on the first turn of this session.
        restore_status = os.environ.get("KIM_BROWSER_RESTORE_STATUS", "").strip().lower()
        sent_sys = self._sent_system_prompt
        if restore_status == "stored_thread" and len(messages) > 1:
            sent_sys = True
        # A cleared/fresh chat has no prior context — the system prompt (and the
        # completion-hash protocol it establishes) MUST be re-sent in full.
        if clear_chat:
            sent_sys = False

        prompt, attachments, completion_hash, new_sent = format_prompt(
            messages,
            tools or [],
            system,
            sent_system_prompt=sent_sys,
            max_inject_chars=self._max_inject_chars,
            use_webview_bridge=self._use_webview_bridge,
            handoff_summary=handoff,
            preferred_site=self._preferred_site,
        )
        # H5: do NOT commit _sent_system_prompt yet — if the send below fails
        # (bridge error, injection-verify failure, timeout), the retry must
        # rebuild the prompt WITH the system prompt + completion-hash protocol.
        # _commit_sent_system_prompt() records it only after a delivered send.

        estimated_usage = self._estimate_prompt_usage(prompt, attachments)
        logger.debug(
            f"Prompt ready: {len(prompt)} chars, "
            f"{len(attachments)} attachment(s) extracted, hash={completion_hash}"
        )

        if self._use_webview_bridge:
            known_tools = {t["name"] for t in (tools or []) if "name" in t}
            capability_repair_attempted = False
            effort_for_this_send = self._effort if not sent_sys else None  # FIX 1: first send only (mirrors CDP gate).
            result = await complete_via_webview_bridge(
                bridge_url=self._bridge_url,
                bridge_token=self._bridge_token,
                preferred_site=self._preferred_site,
                model_tier=self._model_tier,
                gemini_authuser=self._gemini_authuser,
                prompt=prompt,
                attachments=attachments,
                completion_hash=completion_hash,
                known_tools=known_tools,
                clear_chat=clear_chat,
                site_configs=getattr(self, "_site_configs", None),
                effort=effort_for_this_send,
            )
            if self._needs_chatgpt_capability_repair(
                result,
                preferred_site=self._preferred_site,
                has_agent_tools=bool(tools),
            ):
                capability_repair_attempted = True
                repair_hash = f"[END_OF_RESPONSE_{uuid.uuid4().hex[:8]}]"
                first_tool = next(iter(known_tools), "list_dir")
                capability_prompt = (
                    "That NEED_HELP response is incorrect: this chat is connected to Kim's "
                    "local runtime, and every tool listed in [AVAILABLE TOOLS] is real and "
                    "will be executed for you. Do not ask for uploads or tell the user to run "
                    "commands. Continue the original task now by returning one raw JSON tool "
                    "call using an available read-only tool. For this repository inspection, "
                    "start with list_dir if available. Required shape: "
                    '{"tool":"list_dir","args":{"path":"."}}. '
                    f"Append exactly {repair_hash} at the very end."
                )
                logger.info(
                    "ChatGPT falsely denied Kim tool access — retrying once (first=%s)",
                    first_tool,
                )
                result = await complete_via_webview_bridge(
                    bridge_url=self._bridge_url,
                    bridge_token=self._bridge_token,
                    preferred_site=self._preferred_site,
                    model_tier=self._model_tier,
                    gemini_authuser=self._gemini_authuser,
                    prompt=capability_prompt,
                    attachments=[],
                    completion_hash=repair_hash,
                    known_tools=known_tools,
                    clear_chat=False,
                    site_configs=getattr(self, "_site_configs", None),
                )
            if capability_repair_attempted and self._needs_chatgpt_contract_repair(
                result,
                preferred_site=self._preferred_site,
                has_agent_tools=True,
            ):
                # Do not turn "I'm locating the files" into TASK_COMPLETE.
                # After an explicit capability correction, only a real parsed
                # tool call or a correctly formatted terminal response is safe.
                result = {
                    "type": "text",
                    "content": (
                        "NEED_HELP: ChatGPT acknowledged Kim's local tools but did not "
                        "emit a runnable tool call after one capability repair attempt."
                    ),
                }
            if self._needs_chatgpt_contract_repair(
                result,
                preferred_site=self._preferred_site,
                has_agent_tools=bool(tools),
            ):
                repair_hash = f"[END_OF_RESPONSE_{uuid.uuid4().hex[:8]}]"
                repair_prompt = (
                    "Your previous reply was not readable by the caller because it omitted "
                    "the required response prefix. Resend the SAME answer now in exactly one "
                    "of these forms:\n"
                    "TASK_COMPLETE: <your answer>\n"
                    "NEED_HELP: <reason>\n"
                    "Do not discuss this correction and do not add text before the prefix. "
                    f"Append exactly {repair_hash} at the very end."
                )
                logger.info("ChatGPT omitted Kim's response contract — retrying once")
                result = await complete_via_webview_bridge(
                    bridge_url=self._bridge_url,
                    bridge_token=self._bridge_token,
                    preferred_site=self._preferred_site,
                    model_tier=self._model_tier,
                    gemini_authuser=self._gemini_authuser,
                    prompt=repair_prompt,
                    attachments=[],
                    completion_hash=repair_hash,
                    known_tools=known_tools,
                    clear_chat=False,
                    site_configs=getattr(self, "_site_configs", None),
                )
                if self._needs_chatgpt_contract_repair(
                    result,
                    preferred_site=self._preferred_site,
                    has_agent_tools=True,
                ):
                    result = {
                        "type": "text",
                        "content": (
                            "NEED_HELP: ChatGPT ignored Kim's required response format "
                            "after one repair attempt."
                        ),
                    }
            self._commit_sent_system_prompt(result, new_sent)
            return self._attach_usage(result, estimated_usage)

        # Injected browser-I/O seam (K3): call-site driver wins over the
        # constructor's; when present, playwright is skipped entirely.
        driver = page_driver if page_driver is not None else self._page_driver

        connect_timeout = phase_timeout_s(self._config, "connect")
        try:
            if driver is not None:
                page, site = await run_phase(
                    driver.acquire(), phase="connect", timeout_s=connect_timeout, log=logger,
                )
                if page is None or site is None:
                    return lost_chat_response()
                # cast: PageLike stands in for the concrete Page (string form: Page is TYPE_CHECKING-only).
                result = await self._run_chat_flow(
                    cast("Page", page), site, prompt, attachments, tools, completion_hash, clear_chat, estimated_usage
                )
                self._commit_sent_system_prompt(result, new_sent)
                return result

            # Playwright is imported lazily (module top only imports it under
            # TYPE_CHECKING so selecting a browser provider doesn't hard-crash when
            # playwright isn't installed). Import the runtime symbol here, where the
            # CDP path actually needs it; a missing install degrades to the NEED_HELP
            # below instead of a NameError.
            from playwright.async_api import async_playwright

            async def _acquire_cdp_page(pw) -> tuple:
                browser = await self._connect(pw)
                page, site = await self._find_chat_page(browser)
                if (page is None or site is None) and self._last_chat_site:
                    logger.warning(
                        f"Lost reference to {self._last_chat_site} tab "
                        f"(was at {self._last_chat_page_url!r}). Retrying page scan once…"
                    )
                    await asyncio.sleep(1)
                    page, site = await self._find_chat_page(browser)
                return page, site

            async with async_playwright() as pw:
                page, site = await run_phase(
                    _acquire_cdp_page(pw), phase="connect", timeout_s=connect_timeout, log=logger,
                )
                if page is None or site is None:
                    return lost_chat_response()

                result = await self._run_chat_flow(
                    page, site, prompt, attachments, tools, completion_hash, clear_chat, estimated_usage
                )
                self._commit_sent_system_prompt(result, new_sent)
                return result
        except PhaseTimeout as e:
            return self._attach_usage(phase_timeout_response(e), estimated_usage)
        except _DeliveredNoResponse as e:
            # F-B-8: prompt already delivered — return a NEED_HELP that names the
            # thread state (non-retryable, in-band) instead of re-raising a
            # retryable TimeoutError that would re-inject the whole prompt.
            logger.warning(
                "No response from %s after %ss; prompt already delivered — not retrying.",
                e.site, e.waited_s,
            )
            # The send reached the thread, so the system prompt is committed.
            self._sent_system_prompt = new_sent
            return self._attach_usage(
                {
                    "type": "text",
                    "content": (
                        f"NEED_HELP: The message was delivered to the {e.site} chat but no "
                        f"response appeared within {e.waited_s}s. The prompt is already in that "
                        "thread — do not resend it. Check the browser window; if it is still "
                        'generating, wait and send "continue" to read the reply.'
                    ),
                },
                estimated_usage,
            )
        except TimeoutError:
            # H6: TimeoutError is an OSError subclass, but a slow generation /
            # response wait is transient — re-raise so the agent's retry path
            # (classify_provider_error → "timeout", retryable) handles it
            # instead of ending the run with a terminal NEED_HELP.
            raise
        except (ConnectionError, OSError, RuntimeError) as e:
            # Only genuine browser-connection / IO failures are mapped to NEED_HELP;
            # all other exceptions (KeyError, TypeError, programming bugs) propagate
            # to the caller's retry / classify_provider_error path (#37).
            logger.error(f"BrowserProvider.complete browser connection failed: {e}", exc_info=True)
            return self._attach_usage(
                {"type": "text", "content": f"NEED_HELP: Browser connection failed — {e}"},
                estimated_usage,
            )

    async def _run_chat_flow(
        self, page: Page, site: str, prompt: str, attachments: list[dict],
        tools: list[dict] | None, completion_hash: str, clear_chat: bool,
        estimated_usage: dict,
    ) -> dict:
        """Downstream chat flow — identical for the CDP path and an injected
        PageDriver: clear/upload/popups, inject + send + two-phase wait,
        markdown scrape, then parse into the canonical response format."""
        # Rb3: a "chat tab" that is actually a sign-in / Cloudflare wall would
        # swallow the send and hang for the full generation wait — fail fast.
        # F-B-9: pass the page title too so the title-based interstitial markers
        # (Cloudflare "Just a moment", "Sign in to …") are actually reachable —
        # the sole prior call passed the URL alone, making them dead code.
        wall_title = await self._safe_page_title(page)
        wall_reason = detect_auth_wall(getattr(page, "url", "") or "", wall_title)
        if wall_reason:
            logger.warning(f"Auth wall detected on {site}: {wall_reason} ({page.url})")
            return self._attach_usage(auth_wall_response(site, wall_reason), estimated_usage)
        if clear_chat:
            # Navigate to the site root, which starts a NEW conversation.
            # Reloading page.url does NOT clear context: once a chat has
            # begun, the tab URL is a conversation permalink (e.g.
            # chatgpt.com/c/<id>), so a reload lands back in the SAME
            # chat — and a "fresh" prompt (handoff + system prompt) gets
            # sent into the old conversation.
            fresh_url = self._fresh_chat_url(site) or page.url
            logger.info(
                f"Starting a fresh {site} chat at {fresh_url} (was {page.url})..."
            )
            await page.goto(fresh_url, wait_until="domcontentloaded")
            await asyncio.sleep(2.0)
            # F-B-9: a signed-out site redirects the fresh-chat navigation to its
            # login page. Re-detect the wall AFTER the goto so we fail fast with
            # an actionable AUTH_REQUIRED instead of proceeding to _find_selector
            # and dying with the generic "could not locate chat input box".
            post_nav_reason = detect_auth_wall(
                getattr(page, "url", "") or "", await self._safe_page_title(page)
            )
            if post_nav_reason:
                logger.warning(
                    f"Auth wall after fresh-chat nav on {site}: {post_nav_reason} ({page.url})"
                )
                return self._attach_usage(auth_wall_response(site, post_nav_reason), estimated_usage)
            self._sent_system_prompt = False
            self._last_chat_page_url = page.url

        cfg = self._site_configs[site]
        image_attachments = [
            a for a in attachments
            if str(a.get("mime_type", "")).startswith("image/") and a.get("data_base64")
        ]

        if image_attachments:
            # F-B-10: upload EVERY image, not just image_attachments[-1]. The
            # prompt says "[Screenshot attached]" for each one, so pasting only
            # the last silently dropped the earlier screenshots (the bridge path
            # already supports 8). Clipboard side-effect note: on a CDP-attached
            # real Chrome each paste writes the user's system clipboard via
            # navigator.clipboard; the prior clipboard is not restored (an image
            # write cannot be reliably undone). This is a documented trade-off
            # of CDP mode — see F-I-4 / SECURITY.
            total_images = len(image_attachments)
            logger.info(f"[STATUS] Uploading {total_images} screenshot(s) to {site}…")
            delivered_count = 0
            for img in image_attachments:
                if await self._inject_image_clipboard(page, cfg, str(img["data_base64"])):
                    delivered_count += 1
                await page.wait_for_timeout(1200)
            if delivered_count < total_images:
                # Screenshot-honesty (7.2/7.3): the prompt says "[Screenshot
                # attached]" but one or more pastes failed — make the missing
                # image(s) structurally known to the site model instead of
                # letting it confabulate a screen description from no pixels.
                missing = total_images - delivered_count
                logger.warning(
                    f"{missing}/{total_images} screenshot paste(s) failed on {site} — "
                    "appending not-attached note to the prompt."
                )
                if delivered_count == 0:
                    prompt = (
                        f"{prompt}\n\n"
                        "[System note: The screenshot(s) mentioned above could NOT "
                        "be attached. You have NOT seen the image. Do not claim "
                        "to see the screen; answer from the text context only, "
                        "or say the screenshot was unavailable.]"
                    )
                else:
                    prompt = (
                        f"{prompt}\n\n"
                        f"[System note: {missing} of {total_images} screenshot(s) could NOT "
                        "be attached. Do not describe any image that was not provided; "
                        "answer those from the text context only.]"
                    )

        logger.info(f"[STATUS] Preparing {site}…")
        await self._dismiss_popups(page)
        if not self._sent_system_prompt:
            await apply_effort(page, site, self._effort, log=logger)
        logger.info(f"[STATUS] Sending message to {site}…")
        raw_response = await self._send_and_wait(page, cfg, prompt, site, completion_hash)
        # Pass known_tools so the parser can reject prompt-injected fake tool
        # calls whose names aren't in the schema the agent actually has (#38).
        known = {t["name"] for t in (tools or [])} if tools else None
        return self._attach_usage(
            parse_response(raw_response, completion_hash, known_tools=known),
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
            preferred_site=self._preferred_site,
        )
        self._sent_system_prompt = new_sent
        return prompt, attachments, completion_hash

    def _parse_response(self, text, completion_hash, known_tools=None):
        return parse_response(text, completion_hash, known_tools=known_tools)

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

        import urllib.parse as _urlparse
        _cdp_port = _urlparse.urlparse(self._cdp_url).port or 9222

        # Visible mode, no Chrome on the debugging port: open one for the user so
        # they can sign in, then attach to it — instead of erroring out with
        # manual instructions.  Opt out with browser_auto_launch: false.
        if self._auto_launch_chrome:
            browser = await self._launch_headed_chrome(pw, _cdp_port)
            if browser is not None:
                return browser

        # Fallback: auto-launch disabled or Chrome not found. Keep it short.
        chrome_hint = self._chrome_executable() or "google-chrome"
        raise ConnectionError(
            f"Cannot connect to Chrome at {self._cdp_url}, and Kim could not "
            f"launch it automatically. Start Chrome yourself with:\n"
            f'  "{chrome_hint}" --remote-debugging-port={_cdp_port} '
            f'--user-data-dir="{self._user_data_dir}"\n'
            f"then sign in to your AI chat and resend."
        )

    def _chrome_executable(self) -> Optional[str]:
        """Locate a Chrome/Chromium binary for auto-launch, or None."""
        sys_name = platform.system()
        if sys_name == "Darwin":
            candidates = [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
            ]
        elif sys_name == "Windows":
            # Per-user installs (the common case) land in %LOCALAPPDATA%, not
            # Program Files — check that first so auto-launch works without an
            # admin/system-wide Chrome. Fall back to Program Files, then Edge
            # (Chromium-based, also CDP-capable) as a last resort.
            local_appdata = os.environ.get("LOCALAPPDATA", "")
            candidates = []
            if local_appdata:
                candidates.append(
                    os.path.join(
                        local_appdata,
                        r"Google\Chrome\Application\chrome.exe",
                    )
                )
            candidates += [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            ]
        else:  # Linux / other POSIX
            candidates = [
                "google-chrome", "google-chrome-stable",
                "chromium", "chromium-browser",
            ]
        for cand in candidates:
            if os.path.isabs(cand):
                if os.path.exists(cand):
                    return cand
            else:
                found = shutil.which(cand)
                if found:
                    return found
        return None

    def _apply_gemini_authuser(self, site: str, url: str) -> str:  # FIX 6: CDP-path gemini authuser.
        return apply_gemini_authuser(site, url, self._gemini_authuser)

    def _fresh_chat_url(self, site: str) -> Optional[str]:
        """URL that opens a NEW conversation on ``site`` (the site root).

        Used by clear_chat: every supported site starts a fresh chat at its
        root (chatgpt.com/, gemini.google.com/ → /app, deepseek …), whereas
        an in-progress conversation lives at a permalink that survives reloads.
        """
        pattern = str(self._site_configs.get(site, {}).get("url_pattern") or "").strip()
        return None if not pattern else self._apply_gemini_authuser(site, f"https://{pattern}/")

    def _site_launch_url(self) -> Optional[str]:
        """Full URL to open on auto-launch so the user lands on the AI chat
        (its sign-in page when signed out). Uses the preferred site's pattern."""
        key = self._preferred_site
        if not key or key not in self._site_configs:
            # First configured site as a reasonable default.
            key = next(iter(self._site_configs), None)
        if not key:
            return None
        pattern = str(self._site_configs[key].get("url_pattern") or "").strip()
        return None if not pattern else self._apply_gemini_authuser(key, f"https://{pattern}/")

    async def _launch_headed_chrome(
        self, pw: Playwright, port: int
    ) -> Optional[Browser]:
        """Launch a visible Chrome with remote debugging so the user can sign in,
        then attach over CDP. Returns the connected browser, or None if Chrome
        could not be found/started (caller then shows manual instructions)."""
        chrome = self._chrome_executable()
        if not chrome:
            logger.warning("Auto-launch: no Chrome/Chromium binary found")
            return None

        # F-J-3: poll the previously-launched handle before spawning again — a
        # second detached Chrome on the same profile/port just stacks orphans.
        if self._chrome_proc is not None and self._chrome_proc.poll() is None:
            logger.info("Auto-launch: a previously-launched Chrome is still alive; reusing it")
        else:
            args = [
                chrome,
                f"--remote-debugging-port={port}",
                # F-I-4: bind the debug port to loopback explicitly so it is
                # never exposed on 0.0.0.0. The CDP endpoint is UNAUTHENTICATED —
                # any local process running as the user can attach, read cookies,
                # and drive the logged-in browser — so keep it strictly local.
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={self._user_data_dir}",
                "--no-first-run",
                "--no-default-browser-check",
            ]
            launch_url = self._site_launch_url()
            if launch_url:
                args.append(launch_url)

            logger.warning(
                "Auto-launching Chrome with an UNAUTHENTICATED CDP port %d (bound to "
                "127.0.0.1). Any local process running as you can drive this logged-in "
                "browser — see SECURITY (CDP mode).",
                port,
            )
            logger.info(
                "[STATUS] Chrome isn't running — opening it so you can sign in…"
            )
            try:
                # start_new_session detaches Chrome from this (short-lived) process
                # so it keeps running after the task/bridge exits and is reused next
                # turn.
                popen_kwargs: dict = {
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL,
                }
                if platform.system() != "Windows":
                    popen_kwargs["start_new_session"] = True
                self._chrome_proc = subprocess.Popen(args, **popen_kwargs)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Auto-launch: failed to start Chrome: {e}")
                return None

            # F-J-3: record the PID so the desktop shell can reap this detached
            # orphan on app quit — the provider process is short-lived and cannot
            # reap the Chrome it deliberately outlives.
            self._record_launched_chrome(self._chrome_proc.pid, port)

        # Poll the debugging port until Chrome is ready (~15s).
        last_err: Optional[Exception] = None
        for _ in range(30):
            await asyncio.sleep(0.5)
            try:
                browser = await pw.chromium.connect_over_cdp(self._cdp_url)
                logger.info(
                    "[STATUS] Chrome is open. If you're not signed in to your AI "
                    "chat, sign in in the new window, then resend your message."
                )
                return browser
            except Exception as e:  # noqa: BLE001
                last_err = e
        logger.warning(
            f"Auto-launch: Chrome started but CDP port {port} did not come up: {last_err}"
        )
        return None

    def _cdp_chrome_registry_path(self) -> Path:
        return self._project_root / "sessions" / _CDP_CHROME_REGISTRY_NAME

    def _record_launched_chrome(self, pid: int, port: int) -> None:
        """Persist the launched Chrome's PID so the desktop shell can reap it on
        app quit (F-J-3). Best-effort — a write failure is non-fatal."""
        try:
            path = self._cdp_chrome_registry_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"pid": int(pid), "port": int(port)}), encoding="utf-8")
        except OSError as e:  # noqa: BLE001
            logger.debug("Could not record CDP Chrome pid: %s", e)

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

        logger.info(
            f"Headless Chromium ready — {len(context.pages)} page(s) loaded"
        )
        return context  # type: ignore[return-value]

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

        if self._preferred_site:
            # The user explicitly selected this site (e.g. browser:chatgpt) —
            # it is authoritative, not a tie-breaker. Never fall back to a
            # different site's tab (a focused Gemini tab must not hijack a
            # chatgpt run). If no tab of the preferred site is open, open one —
            # unless we are mid-task on that same site (lost tab), where the
            # NEED_HELP path below must win so the LLM context isn't dropped.
            preferred_matches = [
                (p, sk) for p, sk in matches if sk == self._preferred_site
            ]
            if not preferred_matches:
                if self._last_chat_site == self._preferred_site:
                    return None, None
                page = await self._open_preferred_site_tab(browser)
                if page is None:
                    return None, None
                self._last_chat_page_url = page.url
                self._last_chat_site = self._preferred_site
                logger.info(
                    f"Opened new {self._preferred_site} tab: {page.url}"
                )
                return page, self._preferred_site
            matches = preferred_matches

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

    async def _open_preferred_site_tab(self, browser) -> Optional[Page]:
        """Open a new tab on the user's preferred site (fresh task only).

        Used when the user explicitly selected a site (browser:chatgpt) but no
        tab of that site is open — switching providers must open the requested
        site, not silently reuse whatever chat tab happens to exist.
        """
        url = self._site_launch_url()
        if not url:
            return None
        try:
            contexts = getattr(browser, "contexts", None)
            target = contexts[0] if contexts else browser
            page = await target.new_page()
            await page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(2.0)
            return page
        except Exception as e:
            logger.warning(
                f"Could not open a new {self._preferred_site} tab: {e}"
            )
            return None

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
                if re.match(r"^/(?:new|app|chat)?/?$", old_conv):
                    # A brand-new chat materialized its conversation id
                    # (chatgpt.com/ → /c/<id> after the first reply) — same
                    # conversation, the system prompt is already in it.
                    logger.debug(
                        f"New chat got its conversation id within {old_site}: "
                        f"{old_conv!r} -> {new_conv!r} (keeping system prompt)"
                    )
                else:
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
        for label in list(_POPUP_DISMISS_LABELS) + EXTRA_POPUP_DISMISS_LABELS:  # FIX 5: word-boundary-safe match.
            try:
                if await dismiss_popup_label(page, label):
                    logger.info(f"Dismissed popup button: {label!r}")
                    await asyncio.sleep(0.5)
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
    ) -> bool:
        """Paste an image into the chat editor. Returns True only when the
        clipboard write + paste keystroke both went through — callers must
        NOT tell the site model "a screenshot is attached" when this returns
        False (screenshot-honesty, 7.2/7.3)."""
        if not image_b64.startswith("data:"):
            data_uri = f"data:image/png;base64,{image_b64}"
        else:
            data_uri = image_b64

        input_sel = await self._find_selector(page, cfg["input_selectors"])
        if not input_sel:
            logger.warning("Cannot paste image — editor not found")
            return False

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
            return True
        except Exception as e:
            logger.warning(f"Clipboard image injection failed: {e}")
            return False

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
        base_counts = await collect_selector_baselines(page, cfg["response_selectors"], response_sel, initial_count)

        async def _submit() -> None:
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
                return
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

        # F-B-15: bounded so a composer that never renders (auth wall, wrong
        # tab, dead page) fails fast with a phase-named NEED_HELP instead of
        # silently burning up to 9 chained ~30s Playwright click timeouts
        # across _inject_text's 3 retries x 3 fallback strategies.
        await run_phase(
            _submit(), phase="submit",
            timeout_s=phase_timeout_s(self._config, "submit"), site=site, log=logger,
        )

        logger.info(f"[STATUS] Waiting for {site} to respond…")
        logger.info("Message sent, waiting for response…")

        started = await self._wait_for_new_response(
            page, response_sel, initial_count
        )
        if not started and await self._model_still_thinking(page, cfg):  # FIX 3: model may still be visibly reasoning.
            logger.info(f"[STATUS] {site} still appears to be reasoning — extending wait…")
            started = await self._wait_for_new_response(page, response_sel, initial_count, timeout_s=GENERATION_WAIT_S)
        if not started:
            # F-B-8: the prompt was already injected + submitted above, so this
            # timeout is POST-delivery. Signal a non-retryable delivered state
            # instead of a retryable TimeoutError, so the agent does not re-send
            # the same content (with a new hash) into the same live thread.
            raise _DeliveredNoResponse(site, RESPONSE_WAIT_S)

        logger.info(f"[STATUS] {site} is responding…")

        definitive = await self._wait_for_generation_complete(
            page,
            cfg["stop_selectors"],
            cfg["response_selectors"],
            completion_hash,
            site,
            min_index=base_counts,
        )

        # The sentinel is only emitted after the full response has rendered, so
        # a definitive completion needs no extra settle time. The long pause is
        # only for the heuristic exit, where late chunks may still be painting.
        await asyncio.sleep(0.2 if definitive else 1.5)

        logger.info(f"[STATUS] Reading {site}'s response…")
        return await self._scrape_last_response(
            page, cfg["response_selectors"], min_index=base_counts, as_markdown=True
        )

    async def _model_still_thinking(self, page: Page, cfg: dict) -> bool:  # FIX 3: still-reasoning probe.
        probe = build_thinking_probe(cfg.get("stop_selectors"))
        return bool(await self._find_selector(page, probe))

    @staticmethod
    async def _safe_page_title(page) -> str:
        """Best-effort page title for auth-wall detection (F-B-9).

        Returns "" when the page-like object exposes no title() (injected fakes,
        older drivers) or the call fails — the URL-based markers still apply.
        """
        getter = getattr(page, "title", None)
        if getter is None:
            return ""
        try:
            result = getter()
            if inspect.isawaitable(result):
                result = await result
            return str(result or "")
        except Exception:  # noqa: BLE001
            return ""

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
        self, page: Page, response_sel: str, initial_count: int, timeout_s: Optional[float] = None,
    ) -> bool:
        # Snapshot the last pre-send response element: some sites (and reused
        # threads) stream the new answer INTO an existing element instead of
        # appending one, so a count-only check would wait out the full timeout
        # while the reply renders on screen.
        initial_last_text = ""
        if initial_count > 0:
            try:
                initial_last_text = await (
                    page.locator(response_sel).nth(initial_count - 1).inner_text()
                )
            except Exception:
                pass

        deadline = asyncio.get_running_loop().time() + (RESPONSE_WAIT_S if timeout_s is None else timeout_s)
        while asyncio.get_running_loop().time() < deadline:
            count = await page.locator(response_sel).count()
            if count > initial_count:
                logger.debug(
                    f"Response element count: {initial_count} -> {count}"
                )
                return True
            if initial_count > 0:
                try:
                    current_last = await (
                        page.locator(response_sel).nth(initial_count - 1).inner_text()
                    )
                    if current_last != initial_last_text:
                        logger.debug(
                            "Response streaming into existing element "
                            f"(count still {count})"
                        )
                        return True
                except Exception:
                    pass
            await asyncio.sleep(0.5)
        return False

    async def _wait_for_generation_complete(
        self, page: Page, stop_selectors: list[str], response_selectors: list[str],
        completion_hash: Optional[str], site: str = "AI", min_index: "int | dict[str, int]" = 0,
        min_generation_s: float = 5.0,
    ) -> bool:
        """Wait until the site finishes generating.

        Returns True when the completion sentinel was found (definitive — the
        response is fully rendered), False when we fell back to the idle/stop
        heuristics or timed out (caller should allow extra settle time).
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + GENERATION_WAIT_S
        last_status = loop.time()
        min_generation_time = loop.time() + min_generation_s
        elapsed = 0

        norm_hash = _normalize_for_marker(completion_hash) if completion_hash else ""
        last_text_len = 0
        idle_count = 0
        # Stop-control transition tracking: the site's own UI is the most
        # reliable completion signal — the stop/streaming button appears while
        # generating and clears when done, no model cooperation required
        # (unlike the sentinel, which models often omit on short answers).
        saw_stop = False
        stop_cleared_polls = 0

        while loop.time() < deadline:
            current_text = ""
            hash_at_tail = False
            try:
                current_text = await self._scrape_last_response(page, response_selectors, min_index=min_index)
                logger.debug(
                    f"[DEBUG] _wait_for_generation_complete text (len={len(current_text)}): {current_text[-100:]!r}"
                )
                # F-B-7: the sentinel is only a real completion signal when it
                # sits at the TAIL of the model's OWN answer — never when the
                # scrape is the echoed prompt (which embeds the hash inside the
                # marker instruction), and never before min_generation_time.
                # This preserves the [END_OF_RESPONSE_{id}] protocol; it only
                # tightens WHEN the sentinel is accepted, killing the race where
                # the first poll scrapes the user bubble and exits instantly.
                is_prompt_echo = _MARKER_INSTRUCTION_SIGNATURE in current_text.lower()
                hash_at_tail = (
                    bool(norm_hash)
                    and not is_prompt_echo
                    and _hash_at_tail(_normalize_for_marker(current_text), norm_hash)
                )
                if hash_at_tail and loop.time() >= min_generation_time:
                    logger.debug("Generation complete (completion hash at tail)")
                    return True
            except Exception as e:
                logger.debug(f"[DEBUG] _scrape_last_response failed: {e}")

            if len(current_text) > last_text_len:
                idle_count = 0
                last_text_len = len(current_text)
            elif 0 < len(current_text) < last_text_len:
                # Text SHRANK (e.g. Gemini collapsing its "thinking" panel into
                # a shorter final answer). That's still activity — resync the
                # baseline. Without this the idle counter freezes at a length
                # the text can never reach again and the loop only exits via
                # the full timeout while the answer sits on screen.
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
                saw_stop = True
                stop_cleared_polls = 0
                idle_count = 0
            else:
                # Stop-control transition: it appeared during generation and
                # has now cleared — the site itself says generation finished.
                # Two clear polls with stable non-empty text confirm it; this
                # deliberately bypasses min_generation_time and the sentinel
                # wait (fast answers often omit the sentinel entirely).
                if saw_stop and current_text:
                    stop_cleared_polls += 1
                    if stop_cleared_polls >= 2 and idle_count >= 1:
                        logger.debug(
                            "Generation complete (stop control appeared then cleared)"
                        )
                        return True
                # The completion sentinel is the definitive "done" signal. When we
                # asked for one and it's still absent, the model is very likely
                # mid-generation — returning here scrapes a truncated answer (the
                # exact bug where a reply was cut off mid-sentence). So stay patient
                # while the hash is pending and only fall back to the text-settled
                # heuristic after a much longer idle.
                # F-B-7: "pending" means the sentinel is not yet at the tail of
                # the model's answer (a mid-text echo does not count as arrived).
                hash_pending = bool(norm_hash) and not hash_at_tail
                idle_needed = 20 if hash_pending else 8  # ~15s vs ~6s of stable text
                if idle_count > idle_needed and loop.time() > min_generation_time:
                    if hash_pending:
                        logger.warning(
                            "Completion hash still missing after ~%ds of settled text "
                            "— scraping anyway (response may be truncated)",
                            int(idle_count * 0.75),
                        )
                    else:
                        logger.debug("Generation complete (stop button hidden & text settled)")
                    return False
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
        return False

    async def _scrape_last_response(
        self,
        page: Page,
        response_selectors: list[str],
        min_index: "int | dict[str, int]" = 0,
        as_markdown: bool = False,
    ) -> str:
        for sel in response_selectors:  # FIX 2: min_index may be a per-selector dict now.
            try:
                elements = await page.locator(sel).all()
                candidates = response_candidates(elements, min_index, sel)
                for el in reversed(candidates):
                    text = None
                    if as_markdown:
                        # inner_text() renders code blocks as plain <pre> boxes,
                        # DROPPING the ```lang fences the codex-bridge salvage
                        # depends on. Reconstruct them from the DOM (language
                        # from the <code> class or the block header). Any
                        # failure falls through to plain inner_text — never
                        # worse than before.
                        text = await self._scrape_markdown(el)
                    if not text or not text.strip():
                        text = await el.inner_text()
                    if not text or not text.strip():
                        text = await el.text_content()
                    if text and text.strip():
                        logger.debug(f"Scraped {len(text)} chars from {sel}")
                        if os.environ.get("KIM_DEBUG_SCRAPE") == "1":
                            has_fence = "```" in text
                            print(
                                f"[STATUS] [scrape] {len(text)} chars, "
                                f"code-fence present: {has_fence}",
                                flush=True,
                            )
                        return text.strip()
            except Exception:
                continue
        raise RuntimeError(
            "Could not scrape response from any known selector"
        )

    # JS: rebuild markdown from a response element, restoring ```lang fences
    # around <pre> code blocks (which inner_text would flatten). Lives in
    # markdown_scraper.py; kept as a class attr for test/backcompat access.
    _MARKDOWN_SERIALIZER_JS = MARKDOWN_SERIALIZER_JS

    async def _scrape_markdown(self, el) -> Optional[str]:
        """Return the element's text with ```lang code fences reconstructed.

        Returns None on any failure so the caller falls back to inner_text.
        """
        try:
            result = await el.evaluate(self._MARKDOWN_SERIALIZER_JS)
            return result if isinstance(result, str) else None
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[scrape] markdown reconstruction failed: {e}")
            return None

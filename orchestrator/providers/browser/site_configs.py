"""
Per-site configuration for browser-based LLM chat UIs.

Contains CSS selectors for input, send, stop, response, and upload elements
for each supported site. Also defines shared constants used across the
browser provider modules.
"""

import os
import platform

# Modifier key: Cmd on Mac, Ctrl everywhere else
MOD_KEY = "Meta" if platform.system() == "Darwin" else "Control"

# CDP port for the user's real Chrome session.  Matches the env var read by
# mcp_server/tools/web.py (_REAL_BROWSER_CDP_PORT) so both sides agree on the
# port without extra config (#3).  Override via KIM_REAL_BROWSER_CDP_PORT.
_CDP_PORT = int(os.environ.get("KIM_REAL_BROWSER_CDP_PORT", "9222"))
CDP_URL = f"http://localhost:{_CDP_PORT}"
# How long to wait for a NEW response bubble to even START appearing after a
# submit. A model that has nothing new to say (e.g. codex sent a redundant
# follow-up relay) never produces one — a long wait here is the "frozen, had
# to Ctrl-C" hang. Kept short so a dead relay returns the prompt fast; the
# separate GENERATION_WAIT_S still allows a long, slow full answer once it
# has begun. Override via KIM_BROWSER_RESPONSE_WAIT_S.
RESPONSE_WAIT_S = int(os.environ.get("KIM_BROWSER_RESPONSE_WAIT_S", "60"))
GENERATION_WAIT_S = 600
_VERIFY_MIN_CHARS = 20
_INJECT_MAX_RETRIES = 3
_BRIDGE_TIMEOUT_S = 720

# ── Rb3: auth-wall detection ─────────────────────────────────────────────────
# URL substrings that mean the "chat tab" is actually a sign-in or bot-check
# wall. Sending into one of these hangs for the full generation wait (600s);
# detecting it up front turns that into an instant, actionable AUTH_REQUIRED.
_AUTH_WALL_URL_MARKERS = (
    "/login",
    "/signin",
    "/sign-in",
    "/auth/",
    "accounts.google.com",
    "auth.openai.com",
    "auth0.com",
    "challenges.cloudflare.com",
    "__cf_chl",  # Cloudflare challenge query marker
    "/cdn-cgi/challenge",
)

# Page titles of well-known interstitials (checked case-insensitively when a
# title is available; URL detection alone already covers the common cases).
_AUTH_WALL_TITLE_MARKERS = (
    "just a moment",          # Cloudflare
    "attention required",     # Cloudflare block page
    "sign in",
    "log in",
)


def detect_auth_wall(url: str, title: str = "") -> str | None:
    """Return a short human reason when *url*/*title* is an auth/bot wall.

    Pure and conservative: only well-known login and Cloudflare-challenge
    markers match — a false positive here would block a healthy send.
    """
    low_url = (url or "").lower()
    for marker in _AUTH_WALL_URL_MARKERS:
        if marker in low_url:
            return f"the tab is on a sign-in or verification page ({marker.strip('/')})"
    low_title = (title or "").strip().lower()
    if low_title:
        for marker in _AUTH_WALL_TITLE_MARKERS:
            if low_title.startswith(marker):
                return f"the tab shows a '{title.strip()}' page"
    return None


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

SITE_CONFIGS: dict[str, dict] = {
    "claude": {
        "url_pattern": "claude.ai",
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


def to_list(value) -> list[str]:
    """Normalise a selector value from config: string -> [string], list -> list."""
    if not value:
        return []
    if isinstance(value, list):
        return [str(s) for s in value if s]
    return [str(value)]

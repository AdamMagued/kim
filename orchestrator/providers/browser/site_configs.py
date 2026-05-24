"""
Per-site configuration for browser-based LLM chat UIs.

Contains CSS selectors for input, send, stop, response, and upload elements
for each supported site. Also defines shared constants used across the
browser provider modules.
"""

import platform

# Modifier key: Cmd on Mac, Ctrl everywhere else
MOD_KEY = "Meta" if platform.system() == "Darwin" else "Control"

CDP_URL = "http://localhost:9222"
RESPONSE_WAIT_S = 600
GENERATION_WAIT_S = 600
_VERIFY_MIN_CHARS = 20
_INJECT_MAX_RETRIES = 3
_BRIDGE_TIMEOUT_S = 720

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

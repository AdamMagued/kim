"""Popup-dismissal label matching for the browser provider (FIX 5).

"Continue" was dropped from ``site_configs._POPUP_DISMISS_LABELS`` entirely
(see the NOTE there) because the old XPath substring matcher
(``contains(translate(...), label)``) could not tell a standalone consent
"Continue" button from ChatGPT's "Continue generating" — clicking the latter
mid-answer truncates the reply. This module restores "Continue" as a
provider.py-local addition (site_configs.py is out of scope for this fix)
alongside a matcher that requires an exact (whole-button) text match or a
whole-word prefix NOT immediately followed by "generating".
"""
from __future__ import annotations

EXTRA_POPUP_DISMISS_LABELS = ["Continue"]


def popup_label_matches(button_text: str, label: str) -> bool:
    """True when *button_text* is a safe match for the configured *label*.

    A match requires the (trimmed, case-insensitive) button text to either
    equal *label* exactly, or start with *label* as a whole word not
    immediately followed by "generating" — so "Continue" matches a standalone
    Continue button but never "Continue generating".
    """
    text = " ".join((button_text or "").split()).strip().lower()
    target = (label or "").strip().lower()
    if not text or not target:
        return False
    if text == target:
        return True
    if not text.startswith(target):
        return False
    rest = text[len(target):]
    if not rest or not rest[0].isspace():
        return False  # e.g. "continues" — not a whole-word match
    return not rest.strip().startswith("generating")


async def dismiss_popup_label(page, label: str) -> bool:
    """Scan every visible ``<button>`` on *page* for a safe match against
    *label* (popup_label_matches) and click the first one found.

    Returns True when a button was clicked. Never raises — a selector/DOM
    hiccup here must not break the send that follows.
    """
    try:
        all_btns = page.locator("button")
        count = await all_btns.count()
    except Exception:
        return False
    for i in range(count):
        candidate = all_btns.nth(i)
        try:
            text = await candidate.inner_text()
        except Exception:
            continue
        if not popup_label_matches(text, label):
            continue
        try:
            await candidate.wait_for(state="visible", timeout=1500)
            await candidate.click()
            return True
        except Exception:
            return False
    return False

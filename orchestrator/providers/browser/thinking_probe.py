"""Best-effort "still actively reasoning" probe selectors (FIX 3).

DeepSeek's DeepThink (R1) mode renders a separate "Thought for Ns" reasoning
panel well before its ``div.ds-markdown`` answer element mounts, so the 60s
RESPONSE_WAIT_S "did anything start" check in provider.py's
``_send_and_wait`` can time out while the model is visibly still working.
``BrowserProvider._model_still_thinking`` combines the site's own
stop-selectors (already generation-in-progress signals) with these generic
text probes before giving up and raising ``_DeliveredNoResponse``.

UNVERIFIED against the live DOM (no live-site check was done for this task)
— matched by TEXT, not brittle class names, so on any site whose reasoning
UI differs (or has none) these simply never match and behavior is unchanged.
"""
from __future__ import annotations

THINKING_INDICATOR_SELECTORS = [
    "text=/Thought for/i",
    "text=/is thinking/i",
    "text=/Thinking\\.\\.\\./i",
]


def build_thinking_probe(stop_selectors: list[str] | None) -> list[str]:
    """Combine a site's own stop-selectors with the generic text probes."""
    return list(stop_selectors or []) + THINKING_INDICATOR_SELECTORS

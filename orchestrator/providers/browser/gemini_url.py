"""Gemini authuser URL threading for the CDP/Playwright path (FIX 6).

``self._gemini_authuser`` used to be wired only into the webview-bridge
payload (bridge_client.py) — the CDP/Playwright fallback (``_fresh_chat_url``
/ ``_site_launch_url``) ignored it and used whatever Google account the open
tab already had. This appends ``?authuser=N`` (or ``&authuser=N`` when the
URL already has a query string) the way Google's own apps (Gmail/Drive/
Calendar) do for account switching.

UNVERIFIED against the live site for this task — best-effort.
"""
from __future__ import annotations

from typing import Optional


def apply_gemini_authuser(site: str, url: str, authuser: Optional[int]) -> str:
    if site != "gemini" or authuser is None:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}authuser={authuser}"


def scrape_slice_start(cur_len: int, baseline: int) -> int:
    """FIX 2: the start index into a response selector's CURRENT element list.

    ``baseline`` is that selector's own pre-send element count. When the
    count has grown past it, new elements begin exactly at ``baseline``.
    When it hasn't (the site streamed the answer INTO an existing element
    instead of appending one), fall back to "the last element" — the same
    heuristic previously applied uniformly across every response selector,
    now scoped to each selector's own baseline (see _scrape_last_response).
    """
    return baseline if cur_len > baseline else max(cur_len - 1, 0)


def response_candidates(elements: list, min_index: "int | dict[str, int]", sel: str) -> list:
    """FIX 2: slice ``elements`` (a response selector's current element list)
    down to the ones that appeared since send-time. ``min_index`` may be a
    single int (legacy, applied uniformly) or a {selector: baseline} dict —
    see scrape_slice_start above for why per-selector baselines matter.
    """
    baseline = min_index.get(sel, 0) if isinstance(min_index, dict) else int(min_index)
    idx = scrape_slice_start(len(elements), baseline)
    return elements[idx:] if idx < len(elements) else []


async def collect_selector_baselines(
    page, response_selectors: list[str], response_sel: str, initial_count: int
) -> dict[str, int]:
    """FIX 2: pre-send element counts for EVERY configured response selector,
    not just ``response_sel`` (the one _find_selector happened to match) — a
    heterogeneous-DOM site can render the new answer under a different
    selector than the one that matched pre-send. Used by _send_and_wait /
    consumed by _scrape_last_response via scrape_slice_start above.
    """
    baselines: dict[str, int] = {response_sel: initial_count}
    for sel in response_selectors:
        if sel in baselines:
            continue
        try:
            baselines[sel] = await page.locator(sel).count()
        except Exception:
            baselines[sel] = 0
    return baselines

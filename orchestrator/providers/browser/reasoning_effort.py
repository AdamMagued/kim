"""
Reasoning-effort control for browser-driven LLM providers (K-EFFORT).

Kim's "effort" concept (low/medium/high) is a first-class wire param for the
OpenAI-Responses provider, but the browser-contract path (chatgpt.com,
gemini.google.com, chat.deepseek.com) previously dropped it entirely — there
is no `reasoning_effort` field these sites accept over HTTP; effort only
exists as an on-page model/thinking-mode control.

URL-parameter research (live Playwright probing, see the PR this shipped
with): none of the sites honor a query param to select reasoning mode.
ChatGPT strips `?model=...`, Gemini ignores it. So the only real mechanism is
clicking the on-page control — this module does that, defensively.

Design:
    ``apply_effort()`` opens the site's model/effort picker (if one is
    configured) and clicks the first visible item whose text matches the
    requested level's search terms. It NEVER raises — any failure (control
    not found, site not recognized, no term matched) is logged and treated
    as a no-op, leaving the site at whatever mode it already was in. A
    browser turn must never hard-fail just because a picker's DOM moved.

Selectors were captured live via Playwright accessibility-tree inspection
against unauthenticated sessions (2026-07):
    - chatgpt.com: trigger `button[data-testid="model-switcher-dropdown-button"]`
      (aria-label "Model selector"); the submenu is auth-gated so its item
      labels are best-effort ("Instant"/"Thinking"/"Pro", matching OpenAI's
      current three-tier picker).
    - gemini.google.com: trigger `button[data-test-id="bard-mode-menu-button"]`
      (aria-label "Open mode picker, currently {tier}"); this picker DOES
      render pre-auth, with menuitem labels like "3.5 Flash" / "3.5 Thinking"
      / "3.1 Pro" (Thinking/Pro disabled until signed in) — matched here by
      case-insensitive substring so a future version-number bump doesn't
      break the match.
    - chat.deepseek.com: UNVERIFIED (not probed live — see recipe comment
      below). DeepSeek's "effort" isn't a tiered picker like the others, it's
      a DeepThink (R1) on/off toggle, so this recipe maps low -> leave off,
      medium/high -> enable DeepThink.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

EFFORT_LEVELS = ("low", "medium", "high")

# Per-site recipe: how to open the picker, and which text substrings (in
# priority order) identify the item for each effort level.
_RECIPES: dict[str, dict] = {
    "chatgpt": {
        "trigger_selectors": [
            'button[data-testid="model-switcher-dropdown-button"]',
            'button[aria-label*="Model selector"]',
        ],
        "level_terms": {
            "low": ("instant",),
            "medium": ("thinking",),
            "high": ("pro", "thinking"),
        },
    },
    "gemini": {
        "trigger_selectors": [
            'button[data-test-id="bard-mode-menu-button"]',
            'button[aria-label*="mode picker"]',
        ],
        "level_terms": {
            "low": ("flash",),
            "medium": ("thinking",),
            "high": ("pro", "thinking"),
        },
    },
    "deepseek": {
        # UNVERIFIED (not probed live — see module docstring). DeepSeek's
        # effort control is a DeepThink (R1) on/off toggle in the composer,
        # not a tiered model picker like chatgpt/gemini — apply_effort()'s
        # generic open-trigger-then-click-matching-item flow is a loose fit
        # here, but is reused as-is to keep this module's mechanism uniform;
        # any mismatch degrades to a safe no-op (leaves DeepThink alone).
        "trigger_selectors": [
            'button[aria-label*="DeepThink"]',
            'button[aria-label*="R1"]',
            'button:has-text("DeepThink")',
        ],
        "level_terms": {
            # low: DeepSeek's own default (plain chat / V3, DeepThink off).
            # No item is expected to match this — that's the intended
            # no-op, since "off" means not clicking the toggle at all.
            "low": ("deepseek chat", "v3"),
            # medium/high both map to enabling DeepThink (R1) — DeepSeek
            # exposes reasoning as a single on/off toggle, not tiers, so
            # there's no further distinction available between the two.
            "medium": ("deepthink", "r1"),
            "high": ("deepthink", "r1"),
        },
    },
}

# Broad candidate set for the opened picker's items — real sites use a mix
# of role=menuitem / menuitemradio / option / switch for these controls.
_ITEM_SELECTOR = (
    '[role="menuitem"], [role="menuitemradio"], [role="option"], '
    '[role="switch"], [role="radio"]'
)


def resolve_effort(config: dict) -> Optional[str]:
    """KIM_BROWSER_EFFORT env > browser_provider.effort config > None (no-op).

    None means "leave the site at its own default" — never applied, no
    logging, zero behavioral change from before this feature existed.
    """
    raw = os.environ.get("KIM_BROWSER_EFFORT", "").strip().lower()
    if not raw:
        bp_cfg = config.get("browser_provider", {}) if isinstance(config, dict) else {}
        raw = str(bp_cfg.get("effort") or "").strip().lower()
    if not raw:
        return None
    if raw not in EFFORT_LEVELS:
        logger.warning(
            "reasoning_effort: ignoring invalid effort %r (must be one of %s)",
            raw, EFFORT_LEVELS,
        )
        return None
    return raw


async def _find_visible(page, selectors: list[str]) -> Optional[str]:
    for sel in selectors:
        try:
            loc = page.locator(sel)
            count = await loc.count()
            for i in range(count):
                try:
                    if await loc.nth(i).is_visible():
                        return sel
                except Exception:
                    continue
        except Exception:
            continue
    return None


async def apply_effort(page, site: str, effort: Optional[str], *, log=None) -> bool:
    """Best-effort: click ``site``'s picker into the mode matching ``effort``.

    Returns True when something was actually clicked, False for every no-op
    case (nothing requested, unknown site, control/item not found). Never
    raises — a picker that moved must degrade to "left at site default",
    not fail the turn.
    """
    log = log or logger
    if not effort:
        return False
    recipe = _RECIPES.get(site)
    if not recipe:
        log.debug("reasoning_effort: no picker recipe for site %r — leaving default", site)
        return False
    terms = recipe["level_terms"].get(effort)
    if not terms:
        return False

    trigger_sel = await _find_visible(page, recipe["trigger_selectors"])
    if trigger_sel is None:
        log.warning(
            "reasoning_effort: could not find %s's model/effort control — "
            "leaving effort at the site default (%s requested).", site, effort,
        )
        return False

    try:
        await page.locator(trigger_sel).first.click()
        await page.wait_for_timeout(350)
    except Exception as exc:  # noqa: BLE001
        log.warning("reasoning_effort: could not open %s's picker (%s) — no-op.", site, exc)
        return False

    # From here on the picker is OPEN. Every exit path below must close it
    # again (Escape) before returning — except the happy path, where
    # clicking the matched item already closes the picker itself. A
    # try/finally guarantees this even when reading/matching items raises,
    # so a picker never gets left open over the composer for the next
    # submit-phase click to collide with.
    picker_closed = False
    try:
        try:
            item_loc = page.locator(_ITEM_SELECTOR)
            count = await item_loc.count()
        except Exception as exc:  # noqa: BLE001
            log.warning("reasoning_effort: could not read %s's picker items (%s) — no-op.", site, exc)
            return False

        for term in terms:
            # Prefer an EXACT (case-insensitive) label match over a
            # substring match — e.g. chatgpt's "Thinking" and "Thinking
            # mini" both contain the substring "thinking", and a naive
            # first-substring-wins scan can click the wrong tier. Among
            # substring matches (no exact match found), prefer the
            # SHORTEST item text, since a longer/more-qualified label
            # ("Thinking mini") is a more specific, different option than
            # the bare term ("Thinking").
            exact_item = None
            exact_text = ""
            best_item = None
            best_text = ""
            for i in range(count):
                try:
                    item = item_loc.nth(i)
                    if not await item.is_visible():
                        continue
                    text = (await item.inner_text() or "").strip().lower()
                except Exception:
                    continue
                if not text:
                    continue
                if text == term:
                    exact_item, exact_text = item, text
                    break  # can't do better than an exact match
                if term in text and (best_item is None or len(text) < len(best_text)):
                    best_item, best_text = item, text

            chosen_item, chosen_text = (exact_item, exact_text) if exact_item is not None else (best_item, best_text)
            if chosen_item is not None:
                try:
                    await chosen_item.click()
                except Exception:
                    continue
                picker_closed = True
                log.info(
                    "reasoning_effort: set %s effort=%s via %r", site, effort, chosen_text[:60],
                )
                return True

        log.warning(
            "reasoning_effort: opened %s's picker but found no item matching %s "
            "for effort=%s — closing without change.", site, terms, effort,
        )
        return False
    finally:
        if not picker_closed:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

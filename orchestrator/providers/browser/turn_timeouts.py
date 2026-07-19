"""
Per-phase timeout enforcement for a browser LLM turn (K-TIMEOUT).

Bug this fixes: a browser:chatgpt/gemini/deepseek turn could sit on
"Working…" indefinitely with the tab NEVER actually driven — no timeout, no
error, no NEED_HELP, and no way to tell from the logs whether Kim was still
typing the prompt, waiting on the site, or simply stuck. The likely
mechanics: locating/clicking the composer uses Playwright's default
per-call actionability timeout (30s) with NO enclosing budget, and
``_inject_text`` retries three independent injection strategies up to three
times each — worst case is nine chained ~30s waits (four and a half
minutes) with the exact same "[STATUS] Preparing…" log the whole time,
which reads as a silent hang even though it is technically bounded.

Fix: every phase before "wait for the LLM's reply" now runs under an
explicit, tight, configurable deadline. A phase that blows its budget fails
FAST with a clear, phase-named NEED_HELP instead of retrying Playwright's
generic per-call timeout to exhaustion. The reply-wait phase is
deliberately NOT re-capped here — it already has its own, more generous
bounds (RESPONSE_WAIT_S / GENERATION_WAIT_S) tuned for legitimately slow
generations, plus the F-B-8 _DeliveredNoResponse safety net — recapping it
tighter here would kill a slow-but-working LLM prematurely. Composing
connect + submit + the existing reply-wait bounds already yields a hard,
finite ceiling on the whole turn without needing a fourth wrapper.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Awaitable, Optional, TypeVar

T = TypeVar("T")

# Chosen so a slow-but-real page load / retry sequence is not killed
# prematurely, while a step that will NEVER complete (dead CDP connection, a
# composer that never renders, a click stuck on a decoy element) fails fast
# instead of silently eating Playwright's default 30s-per-call timeout many
# times over. All are overridable via env for slow networks/CI.
DEFAULT_TIMEOUTS = {
    "connect": 45.0,  # CDP connect + locate/scan the chat tab
    "submit": 90.0,   # popups, attachments, locate composer, inject, send
}

_PHASE_LABELS = {
    "connect": "connecting to the browser and locating the chat tab",
    "submit": "preparing and submitting the prompt",
}


class PhaseTimeout(Exception):
    """A browser-turn phase exceeded its budget. Maps to a NEED_HELP that
    names the exact stuck phase instead of a silent hang or a generic,
    blindly-retried TimeoutError."""

    def __init__(self, phase: str, timeout_s: float, site: str = "AI"):
        self.phase = phase
        self.timeout_s = timeout_s
        self.site = site
        super().__init__(f"{phase} phase exceeded {timeout_s:.0f}s on {site}")


def phase_timeout_s(config: dict, phase: str) -> float:
    """KIM_BROWSER_TIMEOUT_<PHASE> env > browser_provider.timeouts.<phase>
    config > built-in default."""
    env_val = os.environ.get(f"KIM_BROWSER_TIMEOUT_{phase.upper()}", "").strip()
    if env_val:
        try:
            return float(env_val)
        except ValueError:
            pass
    bp_cfg = config.get("browser_provider", {}) if isinstance(config, dict) else {}
    cfg_timeouts = bp_cfg.get("timeouts") if isinstance(bp_cfg, dict) else None
    if isinstance(cfg_timeouts, dict) and phase in cfg_timeouts:
        try:
            return float(cfg_timeouts[phase])
        except (TypeError, ValueError):
            pass
    return DEFAULT_TIMEOUTS[phase]


async def run_phase(
    coro: Awaitable[T], *, phase: str, timeout_s: float,
    site: str = "AI", log: Optional[logging.Logger] = None,
) -> T:
    """Run ``coro`` under a hard deadline, logging entry so a stuck turn's
    logs show WHICH phase it is in — not just a generic spinner."""
    log = log or logging.getLogger(__name__)
    log.info("[STATUS] %s…", _PHASE_LABELS.get(phase, phase))
    try:
        return await asyncio.wait_for(coro, timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        log.warning(
            "browser turn stuck: %s phase exceeded %.0fs on %s — failing the "
            "turn instead of hanging silently.", phase, timeout_s, site,
        )
        raise PhaseTimeout(phase, timeout_s, site) from exc


# Per-phase closing instruction for the NEED_HELP text. "submit" gets its
# own wording: the timeout can land just after Enter was pressed, meaning
# the message may already have posted — telling the user to blindly resend
# risks duplicating it, so they're told to check the chat first.
_PHASE_RESEND_HINTS = {
    "submit": (
        "check the browser window and see whether the message already "
        "posted — it may have gone out right before the timeout fired — "
        "and only resend if it did not"
    ),
}
_DEFAULT_RESEND_HINT = "check the browser window, sign in or clear the block if needed, then resend"


def phase_timeout_response(exc: PhaseTimeout) -> dict:
    resend_hint = _PHASE_RESEND_HINTS.get(exc.phase, _DEFAULT_RESEND_HINT)
    return {
        "type": "text",
        "content": (
            f"NEED_HELP: The {exc.site} browser turn got stuck while "
            f"{_PHASE_LABELS.get(exc.phase, exc.phase)} and did not finish "
            f"within {exc.timeout_s:.0f}s. This usually means the tab is on "
            "an unexpected page (signed out, a bot/CAPTCHA check, or a stale "
            f"tab) rather than the actual chat — {resend_hint}."
        ),
    }

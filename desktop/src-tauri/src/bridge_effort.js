// bridge_effort.js — best-effort on-page "reasoning effort" picker for the
// in-app webview bridge (K-EFFORT / FIX 1: the GUI webview-bridge path used
// to compute `resolve_effort()` in Python and then throw it away — it never
// reached the site UI). Concatenated after bridge.js into ONE injected
// script (see PERSISTENT_BRIDGE_JS in browser_bridge.rs); bridge.js's
// send() calls `window.__kimApplyEffort(site, effort)` before injecting the
// prompt.
//
// Mirrors the INTENT of orchestrator/providers/browser/reasoning_effort.py's
// `_RECIPES` for the CDP/Playwright path — JS cannot import that Python
// module, so the trigger selectors + level->text terms are duplicated here.
// Keep them in sync by hand if the CDP-path recipes change.
//
// Selectors are UNVERIFIED against the live sites (no live-site check was
// done for this task; see reasoning_effort.py's own docstring for the same
// caveat on its copy). Every failure mode here degrades to a safe no-op: a
// trigger/item that can't be found just leaves the site at its own default,
// and this function NEVER throws — a picker that moved must not break the
// send.
(() => {
  const EFFORT_RECIPES = {
    chatgpt: {
      trigger_selectors: [
        'button[data-testid="model-switcher-dropdown-button"]',
        'button[aria-label*="Model selector"]',
      ],
      level_terms: {
        low: ['instant'],
        medium: ['thinking'],
        high: ['pro', 'thinking'],
      },
    },
    gemini: {
      trigger_selectors: [
        'button[data-test-id="bard-mode-menu-button"]',
        'button[aria-label*="mode picker"]',
      ],
      level_terms: {
        low: ['flash'],
        medium: ['thinking'],
        high: ['pro', 'thinking'],
      },
    },
    deepseek: {
      // UNVERIFIED: DeepSeek's "effort" is a DeepThink (R1) on/off toggle,
      // not a tiered picker like chatgpt/gemini. low intentionally matches
      // nothing (no-op = DeepThink left off); medium/high both map to
      // enabling DeepThink since there's no further tier to distinguish them.
      trigger_selectors: [
        'button[aria-label*="DeepThink"]',
        'button[aria-label*="R1"]',
      ],
      level_terms: {
        low: ['deepseek chat', 'v3'],
        medium: ['deepthink', 'r1'],
        high: ['deepthink', 'r1'],
      },
    },
  };

  const ITEM_SELECTOR =
    '[role="menuitem"], [role="menuitemradio"], [role="option"], [role="switch"], [role="radio"]';

  const isVisible = (el) => {
    if (!el) return false;
    try {
      const style = getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity || '1') === 0) return false;
      if (el.offsetParent !== null) return true;
      return style.position === 'fixed';
    } catch (_) {
      return false;
    }
  };

  const findVisible = (selectors) => {
    for (const sel of selectors || []) {
      let nodes = [];
      try { nodes = Array.from(document.querySelectorAll(sel)); } catch (_) { continue; }
      for (const el of nodes) {
        if (isVisible(el)) return el;
      }
    }
    return null;
  };

  window.__kimApplyEffort = async (site, effort) => {
    try {
      if (!effort) return false;
      const recipe = EFFORT_RECIPES[site];
      if (!recipe) return false;
      const terms = recipe.level_terms[effort];
      if (!terms || !terms.length) return false;

      const trigger = findVisible(recipe.trigger_selectors);
      if (!trigger) {
        console.warn(`[KimBridge] effort: no picker trigger for ${site} (${effort} requested) — leaving default`);
        return false;
      }

      trigger.click();
      await new Promise((r) => setTimeout(r, 350));

      let matched = false;
      try {
        const items = Array.from(document.querySelectorAll(ITEM_SELECTOR)).filter(isVisible);
        for (const term of terms) {
          let exact = null;
          let best = null;
          let bestLen = Infinity;
          for (const item of items) {
            const text = (item.textContent || '').trim().toLowerCase();
            if (!text) continue;
            if (text === term) { exact = item; break; }
            if (text.includes(term) && text.length < bestLen) { best = item; bestLen = text.length; }
          }
          const chosen = exact || best;
          if (chosen) {
            chosen.click();
            matched = true;
            console.log(`[KimBridge] effort: set ${site} effort=${effort} via ${(chosen.textContent || '').trim().slice(0, 60)}`);
            return true;
          }
        }
        console.warn(`[KimBridge] effort: opened ${site} picker but no item matched ${effort} — closing without change`);
        return false;
      } finally {
        if (!matched) {
          try { document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); } catch (_) {}
          try { document.activeElement && document.activeElement.blur && document.activeElement.blur(); } catch (_) {}
        }
      }
    } catch (e) {
      try { console.warn('[KimBridge] effort: unexpected failure, no-op', e); } catch (_) {}
      return false;
    }
  };
})();

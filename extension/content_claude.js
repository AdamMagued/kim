/**
 * Kim Bridge — Claude.ai content script
 *
 * Selectors:
 *   response : [data-testid^='conversation-turn']  (last one)
 *   input    : div[contenteditable='true']  (ProseMirror)
 *   send btn : button[aria-label*='Send']
 */

(function kimClaudeBridge() {
  "use strict";

  const SITE = "claude";

  const SEL = {
    response:   "[data-testid^='conversation-turn']",
    input:      "div[contenteditable='true'].ProseMirror, div[contenteditable='true']",
    sendBtn:    "button[aria-label*='Send']",
    stopBtn:    "button[aria-label*='Stop']",
  };

  // ── Runtime state ──────────────────────────────────────────────────────────
  let loopEnabled  = false;
  let maxRetries   = 3;
  let retryCount   = 0;
  let busy         = false;
  let lastSeenText = "";

  // ── Load persisted loop state for this tab ────────────────────────────────
  chrome.storage.local.get(
    [`loop_${chrome.runtime.id}`, "loop_state"],
    () => {} // actual state pushed by background via KIM_LOOP_STATE
  );

  // ── Listen for popup/background commands ─────────────────────────────────
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === "KIM_LOOP_STATE") {
      loopEnabled = !!msg.enabled;
      maxRetries  = msg.maxRetries ?? 3;
      console.log(`[Kim/claude] loop=${loopEnabled} maxRetries=${maxRetries}`);
    }
  });

  // ── Helpers ───────────────────────────────────────────────────────────────

  function getLastResponseText() {
    const nodes = document.querySelectorAll(SEL.response);
    if (!nodes.length) return "";
    return nodes[nodes.length - 1].innerText || "";
  }

  function isSendReady() {
    const stop = document.querySelector(SEL.stopBtn);
    if (stop && stop.offsetParent !== null) return false; // stop button visible → generating
    const send = document.querySelector(SEL.sendBtn);
    return !!(send && !send.disabled && send.offsetParent !== null);
  }

  /** Inject text into Claude's ProseMirror contenteditable. */
  function setInput(text) {
    const el = document.querySelector(SEL.input);
    if (!el) return false;
    el.focus();
    document.execCommand("selectAll", false, null);
    document.execCommand("delete", false, null);
    document.execCommand("insertText", false, text);
    // Fallback if execCommand didn't take
    if (!el.textContent.trim()) {
      el.textContent = text;
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }
    return true;
  }

  function clickSend() {
    const btn = document.querySelector(SEL.sendBtn);
    if (btn && !btn.disabled) { btn.click(); return true; }
    return false;
  }

  // ── Core auto-loop handler ────────────────────────────────────────────────

  async function handleResponse() {
    if (!loopEnabled || busy) return;
    if (!isSendReady()) return;

    const text = getLastResponseText();
    if (!text || text === lastSeenText) return;
    lastSeenText = text;

    busy = true;
    try {
      const result = await chrome.runtime.sendMessage({ type: "KIM_SYNC", text, site: SITE });

      if (result.has_error && retryCount < maxRetries) {
        retryCount++;
        const errMsg =
          `The previous code had errors. Please fix them.\n\nError output:\n${result.error || result.output || "unknown error"}\n\nRetry ${retryCount}/${maxRetries}`;
        console.warn(`[Kim/claude] Error detected, retry ${retryCount}/${maxRetries}`);
        await sleep(800);
        if (setInput(errMsg)) {
          await sleep(300);
          clickSend();
        }
      } else if (!result.has_error) {
        retryCount = 0;
        if (result.synced) {
          console.log("[Kim/claude] Sync OK");
        }
      } else {
        console.error("[Kim/claude] Max retries reached, stopping loop");
        loopEnabled = false;
      }
    } catch (e) {
      console.error("[Kim/claude] sendMessage error:", e);
    }
    busy = false;
  }

  function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

  // ── MutationObserver ─────────────────────────────────────────────────────

  let observerTimer = null;
  const observer = new MutationObserver(() => {
    if (loopEnabled && !busy && isSendReady()) {
      if (observerTimer) clearTimeout(observerTimer);
      observerTimer = setTimeout(() => {
        observerTimer = null;
        handleResponse();
      }, 600);
    }
  });

  observer.observe(document.body, { childList: true, subtree: true });

  console.log("[Kim] Claude.ai content script loaded");
})();

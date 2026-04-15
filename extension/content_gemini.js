/**
 * Kim Bridge — Gemini content script
 *
 * Selectors:
 *   response : model-response  (last one)
 *   input    : rich-textarea > div[contenteditable]
 *   send btn : button[aria-label*='Send']
 *   stop btn : button[aria-label*='Stop']
 */

(function kimGeminiBridge() {
  "use strict";

  const SITE = "gemini";

  const SEL = {
    response: "model-response",
    input:    "rich-textarea > div[contenteditable], rich-textarea div[contenteditable]",
    sendBtn:  "button[aria-label*='Send message'], button[aria-label*='Send']",
    stopBtn:  "button[aria-label*='Stop']",
  };

  // ── Runtime state ──────────────────────────────────────────────────────────
  let loopEnabled  = false;
  let maxRetries   = 3;
  let retryCount   = 0;
  let busy         = false;
  let lastSeenText = "";

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === "KIM_LOOP_STATE") {
      loopEnabled = !!msg.enabled;
      maxRetries  = msg.maxRetries ?? 3;
      console.log(`[Kim/gemini] loop=${loopEnabled} maxRetries=${maxRetries}`);
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
    if (stop && stop.offsetParent !== null) return false;
    const send = document.querySelector(SEL.sendBtn);
    return !!(send && !send.disabled && send.offsetParent !== null);
  }

  /** Gemini uses a custom <rich-textarea> with a nested contenteditable. */
  function setInput(text) {
    const el = document.querySelector(SEL.input);
    if (!el) return false;
    el.focus();
    document.execCommand("selectAll", false, null);
    document.execCommand("delete", false, null);
    document.execCommand("insertText", false, text);
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
        console.warn(`[Kim/gemini] Error detected, retry ${retryCount}/${maxRetries}`);
        await sleep(800);
        if (setInput(errMsg)) {
          await sleep(300);
          clickSend();
        }
      } else if (!result.has_error) {
        retryCount = 0;
      } else {
        console.error("[Kim/gemini] Max retries reached, stopping loop");
        loopEnabled = false;
      }
    } catch (e) {
      console.error("[Kim/gemini] sendMessage error:", e);
    }
    busy = false;
  }

  function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

  // ── MutationObserver ─────────────────────────────────────────────────────

  const observer = new MutationObserver(() => {
    if (loopEnabled && !busy && isSendReady()) {
      setTimeout(handleResponse, 600);
    }
  });

  observer.observe(document.body, { childList: true, subtree: true });

  console.log("[Kim] Gemini content script loaded");
})();

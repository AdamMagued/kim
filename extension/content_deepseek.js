/**
 * Kim Bridge — DeepSeek content script
 *
 * Selectors:
 *   response : div.ds-markdown  (last one)
 *   input    : textarea
 *   send btn : div[role='button']  (last one)
 */

(function kimDeepSeekBridge() {
  "use strict";

  const SITE = "deepseek";

  const SEL = {
    response: "div.ds-markdown",
    input:    "textarea#chat-input, textarea",
    // DeepSeek has multiple div[role='button'] — the send button is the last
    sendBtnAll: "div[role='button']",
    stopBtn:    "div[role='button'][class*='stop'], div[aria-label*='Stop']",
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
      console.log(`[Kim/deepseek] loop=${loopEnabled} maxRetries=${maxRetries}`);
    }
  });

  // ── Helpers ───────────────────────────────────────────────────────────────

  function getLastResponseText() {
    const nodes = document.querySelectorAll(SEL.response);
    if (!nodes.length) return "";
    return nodes[nodes.length - 1].innerText || "";
  }

  function getSendButton() {
    // Last div[role='button'] on the page is typically the send button
    const btns = document.querySelectorAll(SEL.sendBtnAll);
    return btns.length ? btns[btns.length - 1] : null;
  }

  function isSendReady() {
    const stop = document.querySelector(SEL.stopBtn);
    if (stop && stop.offsetParent !== null) return false;
    // Detect "generating" state: if the response count is increasing,
    // we check whether the send button is interactive.
    // DeepSeek disables/replaces the send button during generation.
    const send = getSendButton();
    if (!send) return false;
    const style = window.getComputedStyle(send);
    return style.cursor !== "not-allowed" && style.pointerEvents !== "none";
  }

  /**
   * DeepSeek uses a plain <textarea>.
   * React listens on the native value setter, so we must use it.
   */
  function setInput(text) {
    const el = document.querySelector(SEL.input);
    if (!el) return false;
    el.focus();
    // Use native setter to trigger React's onChange
    const nativeSetter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype, "value"
    ).set;
    nativeSetter.call(el, text);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }

  function clickSend() {
    const btn = getSendButton();
    if (btn) { btn.click(); return true; }
    const el = document.querySelector(SEL.input);
    if (el) el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", keyCode: 13, bubbles: true }));
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
        console.warn(`[Kim/deepseek] Error detected, retry ${retryCount}/${maxRetries}`);
        await sleep(800);
        if (setInput(errMsg)) {
          await sleep(300);
          clickSend();
        }
      } else if (!result.has_error) {
        retryCount = 0;
      } else {
        console.error("[Kim/deepseek] Max retries reached, stopping loop");
        loopEnabled = false;
      }
    } catch (e) {
      console.error("[Kim/deepseek] sendMessage error:", e);
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

  console.log("[Kim] DeepSeek content script loaded");
})();

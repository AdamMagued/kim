/**
 * Kim Bridge — ChatGPT content script
 *
 * Selectors:
 *   response : div.markdown  (last one)
 *   input    : div#prompt-textarea
 *   send btn : button[data-testid='send-button']
 *   stop btn : button[data-testid='stop-button']
 */

(function kimChatGPTBridge() {
  "use strict";

  const SITE = "chatgpt";

  const SEL = {
    response: "div.markdown",
    input:    "div#prompt-textarea",
    sendBtn:  "button[data-testid='send-button']",
    stopBtn:  "button[data-testid='stop-button']",
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
      console.log(`[Kim/chatgpt] loop=${loopEnabled} maxRetries=${maxRetries}`);
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

  /**
   * ChatGPT uses a React-controlled contenteditable.
   * We simulate keyboard input to trigger React's synthetic event system.
   */
  function setInput(text) {
    const el = document.querySelector(SEL.input);
    if (!el) return false;
    el.focus();
    // Clear existing content
    document.execCommand("selectAll", false, null);
    document.execCommand("delete", false, null);
    // Insert new text
    document.execCommand("insertText", false, text);
    // Fallback: directly set innerHTML and fire input event
    if (!el.textContent.trim()) {
      const p = document.createElement("p");
      p.textContent = text;
      el.innerHTML = "";
      el.appendChild(p);
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }
    return true;
  }

  function clickSend() {
    const btn = document.querySelector(SEL.sendBtn);
    if (btn && !btn.disabled) { btn.click(); return true; }
    // Fallback: press Enter in the input
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
        console.warn(`[Kim/chatgpt] Error detected, retry ${retryCount}/${maxRetries}`);
        await sleep(800);
        if (setInput(errMsg)) {
          await sleep(300);
          clickSend();
        }
      } else if (!result.has_error) {
        retryCount = 0;
      } else {
        console.error("[Kim/chatgpt] Max retries reached, stopping loop");
        loopEnabled = false;
      }
    } catch (e) {
      console.error("[Kim/chatgpt] sendMessage error:", e);
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

  console.log("[Kim] ChatGPT content script loaded");
})();

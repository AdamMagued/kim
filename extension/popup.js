/**
 * Kim Bridge — popup.js
 *
 * Handles:
 *   - Detecting the active AI tab and displaying the site badge
 *   - Auto-loop toggle (communicates to background + content script)
 *   - Relay server connection dot
 *   - Settings (bridge URL, relay URL)
 */

"use strict";

// ── Site detection ────────────────────────────────────────────────────────────

const SITE_PATTERNS = {
  claude:   "claude.ai",
  chatgpt:  "chatgpt.com",
  gemini:   "gemini.google.com",
  deepseek: "chat.deepseek.com",
};

const SITE_LABELS = {
  claude:   "Claude.ai",
  chatgpt:  "ChatGPT",
  gemini:   "Gemini",
  deepseek: "DeepSeek",
};

function detectSite(url) {
  for (const [key, pattern] of Object.entries(SITE_PATTERNS)) {
    if (url.includes(pattern)) return key;
  }
  return null;
}

// ── DOM refs ──────────────────────────────────────────────────────────────────

const siteBadge    = document.getElementById("site-badge");
const loopStatus   = document.getElementById("loop-status");
const loopToggle   = document.getElementById("loop-toggle");
const retriesInput = document.getElementById("retries-input");
const relayDot     = document.getElementById("relay-dot");
const relayLabel   = document.getElementById("relay-label");
const settingsBtn  = document.getElementById("settings-btn");
const settingsPanel = document.getElementById("settings-panel");
const settingsArrow = document.getElementById("settings-arrow");
const bridgeUrlInput = document.getElementById("bridge-url-input");
const relayUrlInput  = document.getElementById("relay-url-input");
const saveBtn        = document.getElementById("save-btn");

// ── State ─────────────────────────────────────────────────────────────────────

let activeTabId   = null;
let activeSite    = null;

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {
  // Find the active AI tab
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab) {
    activeTabId = tab.id;
    activeSite  = detectSite(tab.url || "");
  }

  // Update site badge
  if (activeSite) {
    siteBadge.textContent = SITE_LABELS[activeSite] || activeSite;
    siteBadge.className   = `site-badge ${activeSite}`;
  } else {
    siteBadge.textContent = "No AI tab";
    siteBadge.className   = "site-badge unknown";
  }

  // Load state from background
  const state = await chrome.runtime.sendMessage({
    type:  "KIM_GET_STATE",
    tabId: activeTabId,
  });

  // Loop toggle
  loopToggle.checked  = state.loopEnabled ?? false;
  retriesInput.value  = state.maxRetries ?? 3;
  updateLoopStatus(state.loopEnabled);

  // Relay dot
  updateRelayDot(state.relayConnected);

  // Settings fields
  bridgeUrlInput.value = state.bridgeUrl || "http://localhost:3000";
  relayUrlInput.value  = state.relayUrl  || "";
}

function updateLoopStatus(enabled) {
  loopStatus.textContent = enabled ? "● loop on" : "loop off";
  loopStatus.className   = enabled ? "loop-status active" : "loop-status";
}

function updateRelayDot(connected) {
  relayDot.className = `relay-dot ${connected ? "connected" : "disconnected"}`;
  relayDot.title     = connected ? "Relay connected" : "Relay disconnected";
  relayLabel.innerHTML = `relay: <span>${connected ? "online" : "offline"}</span>`;
  relayLabel.style.color = connected ? "#a6e3a1" : "#f38ba8";
}

// ── Loop toggle ───────────────────────────────────────────────────────────────

loopToggle.addEventListener("change", async () => {
  if (!activeTabId) return;
  const enabled = loopToggle.checked;
  const maxRetries = parseInt(retriesInput.value, 10) || 3;

  await chrome.runtime.sendMessage({
    type:       "KIM_SET_LOOP",
    tabId:      activeTabId,
    enabled,
    maxRetries,
  });

  updateLoopStatus(enabled);
});

retriesInput.addEventListener("change", async () => {
  if (!activeTabId || !loopToggle.checked) return;
  await chrome.runtime.sendMessage({
    type:       "KIM_SET_LOOP",
    tabId:      activeTabId,
    enabled:    loopToggle.checked,
    maxRetries: parseInt(retriesInput.value, 10) || 3,
  });
});

// ── Settings panel ────────────────────────────────────────────────────────────

settingsBtn.addEventListener("click", () => {
  const open = settingsPanel.classList.toggle("open");
  settingsArrow.textContent = open ? "▾" : "▸";
});

saveBtn.addEventListener("click", async () => {
  await chrome.runtime.sendMessage({
    type:      "KIM_SAVE_SETTINGS",
    bridgeUrl: bridgeUrlInput.value.trim(),
    relayUrl:  relayUrlInput.value.trim(),
  });
  saveBtn.textContent = "Saved ✓";
  setTimeout(() => { saveBtn.textContent = "Save settings"; }, 1500);
});

// ── Boot ──────────────────────────────────────────────────────────────────────

init().catch(console.error);

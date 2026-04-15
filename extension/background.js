/**
 * Kim Bridge — background service worker
 *
 * Responsibilities:
 *   1. Parse ## FILE: and ## CMD: blocks from LLM responses
 *   2. POST parsed blocks to the local Kim HTTP bridge (/sync)
 *   3. Track per-tab auto-loop state
 *   4. Poll relay /status for connection dot in popup
 *   5. Handle write_file calls from drag-and-drop (overlay.js)
 *
 * The Kim HTTP bridge (http://localhost:3000) is a thin adapter that
 * forwards calls to the Kim MCP server over stdio.
 * It is started automatically with the MCP server or can run standalone.
 *
 * /sync payload:
 *   { files: [{path, content}], commands: [string], raw_text: string }
 *
 * /sync response:
 *   { ok: boolean, has_error: boolean, error: string, output: string }
 */

// ─── Constants ────────────────────────────────────────────────────────────────

const DEFAULT_BRIDGE_URL = "http://localhost:3000";
const RELAY_STATUS_INTERVAL_MS = 10_000; // poll relay /status every 10 s

// ─── State ────────────────────────────────────────────────────────────────────

let bridgeUrl = DEFAULT_BRIDGE_URL;
let relayUrl = "";
let relayConnected = false;

// Load persisted settings on startup
chrome.storage.local.get(["bridgeUrl", "relayUrl"], (data) => {
  if (data.bridgeUrl) bridgeUrl = data.bridgeUrl;
  if (data.relayUrl) relayUrl = data.relayUrl;
});

chrome.storage.onChanged.addListener((changes) => {
  if (changes.bridgeUrl) bridgeUrl = changes.bridgeUrl.newValue;
  if (changes.relayUrl)  relayUrl  = changes.relayUrl.newValue;
});

// ─── ## FILE: / ## CMD: parser ─────────────────────────────────────────────

/**
 * Parse ## FILE: and ## CMD: blocks from raw LLM response text.
 * Preserved exactly from Bridge V3.
 *
 * @param {string} text
 * @returns {{ files: Array<{path:string, content:string}>, commands: string[] }}
 */
function parseBlocks(text) {
  const files = [];
  const commands = [];

  const lines = text.split("\n");
  let currentPath = null;
  let contentLines = [];

  for (const line of lines) {
    if (line.startsWith("## FILE: ")) {
      // Save previous file block if any
      if (currentPath !== null) {
        files.push({ path: currentPath, content: contentLines.join("\n") });
      }
      currentPath = line.slice("## FILE: ".length).trim();
      contentLines = [];
    } else if (line.startsWith("## CMD: ")) {
      // Save pending file block first
      if (currentPath !== null) {
        files.push({ path: currentPath, content: contentLines.join("\n") });
        currentPath = null;
        contentLines = [];
      }
      const cmd = line.slice("## CMD: ".length).trim();
      if (cmd) commands.push(cmd);
    } else if (currentPath !== null) {
      contentLines.push(line);
    }
  }

  // Flush last file block
  if (currentPath !== null) {
    files.push({ path: currentPath, content: contentLines.join("\n") });
  }

  return { files, commands };
}

/** Return true if the text contains any ## FILE: or ## CMD: markers. */
function hasBlocks(text) {
  return text.includes("## FILE: ") || text.includes("## CMD: ");
}

// ─── HTTP bridge calls ────────────────────────────────────────────────────────

async function postSync(files, commands, rawText) {
  const resp = await fetch(`${bridgeUrl}/sync`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ files, commands, raw_text: rawText }),
  });
  if (!resp.ok) throw new Error(`Bridge /sync returned ${resp.status}`);
  return await resp.json();
  // Expected: { ok: boolean, has_error: boolean, error: string, output: string }
}

async function postWriteFile(path, content) {
  const resp = await fetch(`${bridgeUrl}/write_file`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, content }),
  });
  if (!resp.ok) throw new Error(`Bridge /write_file returned ${resp.status}`);
  return await resp.json();
}

// ─── Message handler ──────────────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  handleMessage(message, sender)
    .then(sendResponse)
    .catch((err) => {
      console.error("[Kim background] error:", err);
      sendResponse({ ok: false, has_error: true, error: String(err) });
    });
  return true; // keep channel open for async response
});

async function handleMessage(message, sender) {
  const { type } = message;

  // ── Content script: new LLM response ready ──────────────────────────────
  if (type === "KIM_SYNC") {
    const text = message.text || "";

    if (!hasBlocks(text)) {
      // Nothing to sync; signal no error so loop can stop
      return { ok: true, has_error: false, synced: false };
    }

    const { files, commands } = parseBlocks(text);
    console.log(
      `[Kim] Syncing ${files.length} file(s), ${commands.length} command(s)`
    );

    try {
      const result = await postSync(files, commands, text);
      return { ok: true, has_error: result.has_error, error: result.error, output: result.output, synced: true };
    } catch (err) {
      // Bridge not reachable — return error so content script can show it
      return { ok: false, has_error: true, error: `Bridge unreachable: ${err.message}` };
    }
  }

  // ── Drag-and-drop: write a file via the bridge ──────────────────────────
  if (type === "KIM_WRITE_FILE") {
    try {
      await postWriteFile(message.path, message.content);
      return { ok: true };
    } catch (err) {
      return { ok: false, error: String(err) };
    }
  }

  // ── Popup: get auto-loop state for the current tab ──────────────────────
  if (type === "KIM_GET_STATE") {
    const tabId = message.tabId;
    const stored = await chrome.storage.local.get([`loop_${tabId}`, `retries_${tabId}`]);
    return {
      loopEnabled: stored[`loop_${tabId}`] ?? false,
      maxRetries:  stored[`retries_${tabId}`] ?? 3,
      relayConnected,
      bridgeUrl,
      relayUrl,
    };
  }

  // ── Popup: set auto-loop on/off ─────────────────────────────────────────
  if (type === "KIM_SET_LOOP") {
    const { tabId, enabled, maxRetries } = message;
    await chrome.storage.local.set({
      [`loop_${tabId}`]: enabled,
      [`retries_${tabId}`]: maxRetries ?? 3,
    });
    // Notify the content script in that tab
    await chrome.tabs.sendMessage(tabId, {
      type: "KIM_LOOP_STATE",
      enabled,
      maxRetries: maxRetries ?? 3,
    }).catch(() => {}); // tab may not have content script yet
    return { ok: true };
  }

  // ── Popup: save settings ─────────────────────────────────────────────────
  if (type === "KIM_SAVE_SETTINGS") {
    await chrome.storage.local.set({
      bridgeUrl: message.bridgeUrl || DEFAULT_BRIDGE_URL,
      relayUrl:  message.relayUrl  || "",
    });
    return { ok: true };
  }

  return { ok: false, error: `Unknown message type: ${type}` };
}

// ─── Relay status poller ─────────────────────────────────────────────────────

async function pollRelayStatus() {
  if (!relayUrl) {
    relayConnected = false;
    return;
  }
  try {
    const resp = await fetch(`${relayUrl}/status`, { signal: AbortSignal.timeout(4000) });
    relayConnected = resp.ok;
  } catch {
    relayConnected = false;
  }
}

// Poll on startup and then every 10 s
pollRelayStatus();
setInterval(pollRelayStatus, RELAY_STATUS_INTERVAL_MS);

(() => {
  // Guard: only install once per page load.
  if (window.__kimBridge && window.__kimBridge._v >= 10) return;

  const SITE_CONFIGS = {
    chatgpt: {
      input_selectors: ["div#prompt-textarea", "div[contenteditable='true']"],
      send_selectors: ["button[data-testid='send-button']", "button[aria-label*='Send']"],
      stop_selectors: ["button[data-testid='stop-button']", "button[aria-label*='Stop']"],
      response_selectors: ["div.markdown", "article div.prose"],
      upload_button_selectors: ["button[aria-label*='Attach']", "button[data-testid*='upload']"],
      file_input_selectors: ["input[type='file']"],
    },
    gemini: {
      input_selectors: ["rich-textarea div[contenteditable]", "rich-textarea [contenteditable='true']", "div[contenteditable='true']"],
      send_selectors: ["button[aria-label*='Send message']", "button[aria-label*='Send']", "button[data-testid*='send']", "button[mattooltip*='Send']", "button[aria-label*='Submit prompt']", "button[aria-label*='Submit']"],
      stop_selectors: ["button[aria-label*='Stop']", "button[aria-label*='Stop generating']", "button[data-testid*='stop']"],
      response_selectors: ["model-response", "model-response message-content", "model-response .response-content", "message-content", "div.response-content", "div.markdown"],
      upload_button_selectors: ["button[aria-label*='Upload']", "button[aria-label*='Add image']"],
      file_input_selectors: ["input[type='file']"],
    },
    deepseek: {
      input_selectors: ["textarea#chat-input", "textarea"],
      send_selectors: ["button[aria-label*='Send']", "button[type='submit']"],
      stop_selectors: ["button[aria-label*='Stop']", "div[role='button'][class*='stop']"],
      response_selectors: ["div.ds-markdown"],
      upload_button_selectors: ["button[aria-label*='Upload']", "button[aria-label*='Attach']"],
      file_input_selectors: ["input[type='file']"],
    },
  };

  // ── Helpers ──────────────────────────────────────────────────────────
  const normalizeText = (v) => String(v || '')
    .replace(/[\u200b-\u200d\u2060\ufeff]/g, '')
    .replace(/\u00a0/g, ' ')
    .replace(/[“”]/g, '"')
    .replace(/[‘’]/g, "'")
    .replace(/[—–]/g, '--')
    .replace(/…/g, '...')
    .trim();
  const canonicalText = (v) => normalizeText(v).replace(/\s+/g, ' ').trim();

  const isVisible = (el) => {
    if (!el) return false;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity || '1') === 0) return false;
    if (el.offsetParent !== null) return true;
    return style.position === 'fixed';
  };

  const isEnabled = (el) => {
    if (!el) return false;
    if ('disabled' in el && el.disabled) return false;
    if (el.getAttribute && el.getAttribute('aria-disabled') === 'true') return false;
    return true;
  };

  const findElement = (selectors, opts = {}) => {
    for (const sel of selectors || []) {
      let nodes = [];
      try { nodes = Array.from(document.querySelectorAll(sel)); } catch (_) { continue; }
      for (const el of nodes) {
        if (opts.visible && !isVisible(el)) continue;
        if (opts.enabled && !isEnabled(el)) continue;
        return el;
      }
    }
    return null;
  };

  // Shadow-DOM-aware input finder for Gemini's rich-textarea component.
  // document.querySelectorAll cannot pierce shadow roots, so we check both
  // the light DOM children and the shadow root of each rich-textarea host.
  const findGeminiInput = () => {
    for (const host of Array.from(document.querySelectorAll('rich-textarea'))) {
      if (!isVisible(host)) continue;
      let inner = host.querySelector('[contenteditable]');
      if (inner && isVisible(inner)) return inner;
      if (host.shadowRoot) {
        inner = host.shadowRoot.querySelector('[contenteditable]');
        if (inner) return inner;
      }
    }
    // Broad fallback: any visible contenteditable on the page
    for (const el of Array.from(document.querySelectorAll('[contenteditable]'))) {
      if (isVisible(el)) return el;
    }
    return null;
  };

  const dismissPopups = async () => {
    const labels = ['i agree', 'agree', 'got it', 'continue', 'accept', 'ok', 'dismiss', 'close', 'no thanks'];
    for (const btn of document.querySelectorAll('button')) {
      if (!isVisible(btn)) continue;
      const text = normalizeText(btn.textContent).toLowerCase();
      if (labels.includes(text)) {
        try { btn.click(); await new Promise(r => setTimeout(r, 200)); } catch (_) {}
      }
    }
  };

  const hasStopSemantics = (el) => {
    const label = String((el && el.getAttribute && el.getAttribute('aria-label')) || (el && el.textContent) || '').toLowerCase().trim();
    if (!label) return false;
    return /(^|\b)stop(\b|$)/i.test(label) || label.includes('stop generating');
  };

  const isAnyStopVisible = (cfg) => {
    for (const sel of cfg.stop_selectors || []) {
      try {
        const nodes = Array.from(document.querySelectorAll(sel));
        for (const el of nodes) {
          if (el && isVisible(el) && hasStopSemantics(el)) return true;
        }
      } catch (_) {}
    }
    return false;
  };

  const isLikelyUserNode = (node) => {
    if (!node) return false;
    try {
      if (node.closest(
        'user-query, [data-message-author-role="user"], [data-role="user"], [data-author="user"], '
        + '.user-message, .from-user, .query-content, .prompt-bubble, [data-testid*="user-message"]'
      )) return true;
    } catch (_) {}
    try {
      const author = String(node.getAttribute?.('data-message-author-role') || node.getAttribute?.('data-role') || node.getAttribute?.('data-author') || '').toLowerCase();
      if (author === 'user') return true;
    } catch (_) {}
    return false;
  };

  // Read what the user-VISIBLE editor actually contains. For contenteditable
  // editors (Gemini's rich-textarea, Claude's ProseMirror) this must be the
  // editor's own rendered text — NEVER Gemini's hidden mirror <textarea>.
  // The old code preferred the mirror, but injectPromptText also force-synced
  // that mirror to the full prompt, so verification always "passed" against a
  // value we wrote ourselves while the real editor could hold a truncated
  // fragment (or nothing). Reading the visible editor makes every check
  // downstream (promptMatchesInput / inputCleared / inputStuck) honest.
  const readInputText = (inputEl) => {
    if (!inputEl) return '';
    if (inputEl instanceof HTMLTextAreaElement || inputEl instanceof HTMLInputElement) {
      return normalizeText(inputEl.value || '');
    }
    return normalizeText(inputEl.innerText || inputEl.textContent || '');
  };

  const promptMatchesInput = (inputEl, promptText) => {
    const expectedRaw = String(promptText || '');
    const actual = readInputText(inputEl);
    const expected = normalizeText(expectedRaw);
    const actualCanonical = canonicalText(actual);
    const expectedCanonical = canonicalText(expected);
    if (!expected) return true;
    if (actual === expected || actualCanonical === expectedCanonical) return true;
    if (expected.length < 500) return false;
    if (actualCanonical.length < Math.floor(expectedCanonical.length * 0.9)) return false;
    
    const fuzzyMatch = (a, b) => a.replace(/\W+/g, '') === b.replace(/\W+/g, '');
    return fuzzyMatch(actualCanonical.slice(0, 220), expectedCanonical.slice(0, 220))
      && fuzzyMatch(actualCanonical.slice(-220), expectedCanonical.slice(-220));
  };

  // ── IPC emit (Tauri native) ──────────────────────────────────────────
  const ipcEmit = (payload) => {
    try {
      if (window.__TAURI_INTERNALS__ && window.__TAURI_INTERNALS__.invoke) {
        window.__TAURI_INTERNALS__.invoke('plugin:event|emit', {
          event: 'kim-bridge-ipc',
          payload: payload,
        });
        return true;
      }
    } catch (_) {}

    try {
      // Direct WebKit IPC fallback for external domains where __TAURI_INTERNALS__ is not injected
      if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.ipc) {
        window.webkit.messageHandlers.ipc.postMessage({
          cmd: "plugin:event|emit",
          callback: 0,
          error: 0,
          payload: {
            event: "kim-bridge-ipc",
            payload: payload
          }
        });
        return true;
      }
    } catch (e) {
      console.warn('[KimBridge] WebKit IPC emit failed:', e);
    }

    return false;
  };

  // ── Response extraction ──────────────────────────────────────────────
  const getLatestResponseText = (cfg, siteKey) => {
    if (siteKey === 'gemini') {
        const modelResponses = Array.from(document.querySelectorAll('model-response')).filter(node => {
            return !!node && isVisible(node) && !isLikelyUserNode(node);
        });

        let bestText = '';
        let bestModelNode = null;
        let bestModelIndex = -1;

        const chooseBest = (text, modelNode, modelIndex) => {
            if (!text) return;
            if (modelIndex > bestModelIndex) {
                bestText = text;
                bestModelNode = modelNode;
                bestModelIndex = modelIndex;
                return;
            }
            if (modelIndex === bestModelIndex && text.length > bestText.length) {
                bestText = text;
                bestModelNode = modelNode;
                bestModelIndex = modelIndex;
            }
        };

        for (const sel of [
            'model-response',
            'model-response message-content',
            'model-response .response-content',
            'message-content',
            'div.response-content',
        ]) {
            let nodes = [];
            try {
                nodes = Array.from(document.querySelectorAll(sel));
            } catch (_) {
                continue;
            }
            for (const node of nodes) {
                if (!node || !isVisible(node) || isLikelyUserNode(node)) continue;

                const modelNode = (node.matches && node.matches('model-response'))
                    ? node
                    : (node.closest ? node.closest('model-response') : null);
                if (!modelNode || !isVisible(modelNode) || isLikelyUserNode(modelNode)) continue;

                const modelIndex = modelResponses.indexOf(modelNode);
                if (modelIndex < 0) continue;

                const text = normalizeText(node.innerText || node.textContent || '');
                chooseBest(text, modelNode, modelIndex);
            }
        }

        if (!bestText && modelResponses.length > 0) {
            const lastIndex = modelResponses.length - 1;
            const lastNode = modelResponses[lastIndex];
            bestText = normalizeText(lastNode.innerText || lastNode.textContent || '');
        }

        return bestText;
    }

    // Generic: find last visible non-user response node
    for (const sel of cfg.response_selectors || []) {
      try {
        const nodes = Array.from(document.querySelectorAll(sel));
        // Iterate backwards to get the most recent message bubble
        for (let i = nodes.length - 1; i >= 0; i--) {
          const node = nodes[i];
          if (!node || !isVisible(node) || isLikelyUserNode(node)) continue;
          const text = normalizeText(node.innerText || node.textContent || '');
          if (text) return text;
        }
      } catch (_) {}
    }
    return '';
  };

  // Return the ordered list of visible, non-user response container nodes.
  // This is the authoritative source for "which DOM elements are responses",
  // used to detect *new* turns by element identity rather than by text/count
  // alone (text/count are fragile when a reused thread re-renders).
  const getResponseNodes = (cfg, siteKey) => {
    if (siteKey === 'gemini') {
      return Array.from(document.querySelectorAll('model-response')).filter(n => !!n && isVisible(n) && !isLikelyUserNode(n));
    }
    const seen = new Set();
    const out = [];
    for (const sel of cfg.response_selectors || []) {
      try {
        for (const n of Array.from(document.querySelectorAll(sel))) {
          if (!n || seen.has(n) || !isVisible(n) || isLikelyUserNode(n)) continue;
          seen.add(n);
          out.push(n);
        }
      } catch (_) {}
    }
    return out;
  };

  const nodeText = (n) => (n ? normalizeText(n.innerText || n.textContent || '') : '');

  const countResponseNodes = (cfg, siteKey) => getResponseNodes(cfg, siteKey).length;

  // ── Prompt injection ─────────────────────────────────────────────────
  const injectPromptText = async (inputEl, promptText) => {
    const target = String(promptText || '');
    try {
      inputEl.focus();
      document.execCommand('selectAll', false);
    } catch (_) {}
    if (inputEl instanceof HTMLTextAreaElement || inputEl instanceof HTMLInputElement) {
      const proto = Object.getPrototypeOf(inputEl);
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      if (setter) setter.call(inputEl, ''); else inputEl.value = '';
      inputEl.dispatchEvent(new Event('input', { bubbles: true }));
      if (setter) setter.call(inputEl, target); else inputEl.value = target;
      inputEl.dispatchEvent(new Event('input', { bubbles: true }));
      inputEl.dispatchEvent(new Event('change', { bubbles: true }));
      return readInputText(inputEl).length;
    }
    // Contenteditable editors (Gemini rich-textarea, Claude ProseMirror,
    // ChatGPT prompt div). document.execCommand('insertText') TRUNCATES AT
    // NEWLINES in these rich editors (see orchestrator/providers/browser/
    // provider.py module docstring) — and Kim's chat-mode first turn is a
    // large multi-line prompt, so insertText alone silently sends a fragment.
    // Strategy ladder, each step VERIFIED against the visible editor text:
    //   1. execCommand('insertText')  — fine for single-line prompts.
    //   2. document.execCommand('paste') — trusted paste from the system
    //      clipboard, which Rust pre-staged with the FULL prompt
    //      (write_text_prompt_to_clipboard) before evaluating this script.
    //   3. Synthetic ClipboardEvent('paste') carrying the full text — rich
    //      editors implement their own paste handler that reads
    //      event.clipboardData, which preserves newlines.
    // NOTE: we deliberately do NOT write Gemini's hidden mirror <textarea>
    // anymore. Force-syncing it made verification circular (we read back the
    // value we ourselves wrote) and left stale "full prompt" text that
    // Gemini never clears, producing false "Send did not register" errors
    // and duplicate re-submits after a message HAD gone out.
    const clearEditor = () => {
      try {
        inputEl.focus();
        document.execCommand('selectAll', false);
      } catch (_) {}
    };
    const accepted = () => promptMatchesInput(inputEl, target);
    try {
      document.execCommand('insertText', false, target);
    } catch (_) {}
    if (!accepted()) {
      clearEditor();
      let pasted = false;
      try { pasted = document.execCommand('paste'); } catch (_) {}
      if (pasted && !accepted()) {
        // Paste ran but delivered something else (stale clipboard) — clear it.
        clearEditor();
        try { document.execCommand('delete', false); } catch (_) {}
      }
    }
    if (!accepted()) {
      clearEditor();
      try {
        const dt = new DataTransfer();
        dt.setData('text/plain', target);
        const pasteEvent = new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: dt });
        inputEl.dispatchEvent(pasteEvent);
      } catch (e) {
        console.warn('[KimBridge] Synthetic paste failed:', e);
      }
    }
    try {
      inputEl.dispatchEvent(new InputEvent('input', { data: target, inputType: 'insertText', bubbles: true, cancelable: true }));
    } catch (_) {}
    inputEl.dispatchEvent(new Event('input', { bubbles: true }));
    inputEl.dispatchEvent(new Event('change', { bubbles: true }));
    return readInputText(inputEl).length;
  };

  // ── Attachment injection ──────────────────────────────────────────────
  const decodeBase64 = (b64) => {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return bytes;
  };

  const inferExtension = (mime) => {
    const m = String(mime || '').toLowerCase();
    const map = { 'image/png':'png', 'image/jpeg':'jpg', 'image/webp':'webp', 'image/gif':'gif', 'application/pdf':'pdf', 'text/plain':'txt' };
    return map[m] || (m.includes('/') ? m.split('/')[1].split('+')[0] : 'bin');
  };

  // Returns { count, strategy, thumb } — count>0 ONLY when a real attachment chip
  // actually appeared in the page. Each strategy is verified so a programmatic
  // upload that the web UI silently ignores is not falsely reported as success.
  const injectAttachments = async (cfg, inputEl, attachments, requestNativePaste) => {
    if (!attachments || !attachments.length) return { count: 0, strategy: 'none', thumb: false };
    const files = [];
    for (let i = 0; i < attachments.length; i++) {
      try {
        const att = attachments[i];
        const mime = String(att?.mime_type || 'application/octet-stream');
        const b64 = String(att?.data_base64 || '');
        if (!b64) continue;
        const bytes = decodeBase64(b64);
        const blob = new Blob([bytes], { type: mime });
        const name = String(att?.name || `attachment_${i+1}.${inferExtension(mime)}`).trim();
        files.push(new File([blob], name, { type: mime }));
      } catch (_) {}
    }
    if (!files.length) return { count: 0, strategy: 'none', thumb: false };

    // A real attachment chip / thumbnail appearing is the only proof the upload took.
    const THUMB_SEL = 'img[src^="blob:"], img[src^="data:"], file-attachment, thumbnail-view, '
      + '[data-test-id*="image"], [aria-label*="Remove file"], [aria-label*="Remove attachment"]';
    // Count thumbnails inside the COMPOSER region only when we can identify it.
    // Counting document-wide made ANY blob/data image (avatars, earlier-turn
    // inline images, site artwork) a false "upload succeeded" signal, which
    // silently dropped the not-attached honesty note (audit 7.2). Fall back to
    // the whole document only when no composer ancestor is recognizable, so
    // unknown site layouts keep the old behavior rather than false-negating.
    const COMPOSER_SEL = 'input-area-v2, .input-area-container, .input-area, form, '
      + '[class*="composer"], [class*="input-area"]';
    const composerScope = (() => {
      try {
        const scope = inputEl && inputEl.closest ? inputEl.closest(COMPOSER_SEL) : null;
        if (scope) return scope;
      } catch (_) {}
      return document;
    })();
    const countThumbs = () => {
      try { return composerScope.querySelectorAll(THUMB_SEL).length; } catch (_) { return 0; }
    };
    const baseThumbs = countThumbs();
    const thumbAppeared = async (ms) => {
      let waited = 0;
      while (waited < ms) {
        await new Promise(r => setTimeout(r, 150));
        waited += 150;
        if (countThumbs() > baseThumbs) return true;
      }
      return false;
    };

    const imageFile = files.find(f => String(f.type || '').startsWith('image/'));

    // Strategy 1 (preferred for the in-app webview): ask Rust to fire a REAL Cmd+V.
    // WKWebView blocks execCommand('paste') and rejects synthetic ClipboardEvent, but a
    // genuine keystroke is trusted and accepted. Do this FIRST — before touching the
    // file input / upload button, which can steal focus or open a picker and make the
    // keystroke miss the editor. The PNG is already on the clipboard and (for image
    // sends) the webview is visible + frontmost + key.
    // Only PNG: Rust stages only PNG to the system clipboard, so firing Cmd+V for a
    // non-PNG image would paste whatever was already on the clipboard (stale/wrong).
    if (imageFile && imageFile.type === 'image/png' && inputEl && typeof requestNativePaste === 'function') {
      try { inputEl.focus(); } catch (_) {}
      await new Promise(r => setTimeout(r, 150));
      try { inputEl.focus(); } catch (_) {}            // re-assert focus right before the paste
      const preThumbs = countThumbs();
      try { requestNativePaste(); } catch (_) {}
      // The keystroke only fires ~400-500ms after this (IPC round-trip + Rust settle
      // sleep), so wait PAST that before looking — otherwise we catch a pre-paste
      // transient and declare success before the image is actually pasted (the race we
      // diagnosed: thumb reported at +151ms while Cmd+V fired at +413ms).
      await new Promise(r => setTimeout(r, 800));
      let pasted = false;
      for (let i = 0; i < 25; i++) {                   // poll up to ~5s more
        if (countThumbs() > preThumbs) { pasted = true; break; }
        await new Promise(r => setTimeout(r, 200));
      }
      if (pasted) {
        // Let Gemini UPLOAD the image to its backend before we inject text and send —
        // otherwise the message goes out without the picture attached.
        await new Promise(r => setTimeout(r, 2000));
        await dismissPopups();
        return { count: 1, strategy: 'native_paste', thumb: true };
      }
      // native paste didn't visibly land — fall through to the legacy strategies.
    }

    // Strategy 2: hidden file input (other UIs). Verify a chip appears before trusting it.
    let fileInput = null;
    for (const sel of cfg.file_input_selectors || []) {
      try { const el = document.querySelector(sel); if (el && el instanceof HTMLInputElement && el.type === 'file') { fileInput = el; break; } } catch (_) {}
    }
    if (!fileInput) {
      for (const sel of cfg.upload_button_selectors || []) {
        try {
          const btn = document.querySelector(sel);
          if (btn) { btn.click(); await new Promise(r => setTimeout(r, 120)); }
          for (const fsel of cfg.file_input_selectors || []) {
            const fi = document.querySelector(fsel);
            if (fi && fi instanceof HTMLInputElement && fi.type === 'file') { fileInput = fi; break; }
          }
          if (fileInput) break;
        } catch (_) {}
      }
    }
    if (fileInput) {
      try {
        const dt = new DataTransfer();
        for (const f of files) dt.items.add(f);
        try { fileInput.files = dt.files; } catch (_) { try { Object.defineProperty(fileInput, 'files', { value: dt.files, configurable: true }); } catch (_) {} }
        fileInput.dispatchEvent(new Event('input', { bubbles: true }));
        fileInput.dispatchEvent(new Event('change', { bubbles: true }));
        if (await thumbAppeared(2500)) { await dismissPopups(); return { count: files.length, strategy: 'file_input', thumb: true }; }
      } catch (_) {}
    }

    // Strategy 3/4: execCommand / synthetic paste (other webviews that honour them).
    if (imageFile && inputEl) {
      try { inputEl.focus(); await new Promise(r => setTimeout(r, 120)); } catch (_) {}
      let execPasted = false;
      try { execPasted = document.execCommand('paste'); } catch (_) {}
      if (await thumbAppeared(3000)) { await dismissPopups(); return { count: 1, strategy: 'execCommand_paste', thumb: true, execPasted }; }

      try {
        const dt = new DataTransfer();
        dt.items.add(imageFile);
        inputEl.dispatchEvent(new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: dt }));
      } catch (_) {}
      if (await thumbAppeared(2500)) { await dismissPopups(); return { count: 1, strategy: 'synthetic_paste', thumb: true }; }

      return { count: 0, strategy: 'all_failed', thumb: false, execPasted };
    }
    return { count: 0, strategy: 'no_image', thumb: false };
  };

  // ── Main send function ───────────────────────────────────────────────
  const send = async (prompt, reqId, site, attachments, callbackUrl, completionHash, modelTier, effort) => {
    window.__kimModelTier = modelTier;
    const siteKey = SITE_CONFIGS[site] ? site : 'chatgpt';
    const cfg = SITE_CONFIGS[siteKey];
    const baselineNodes = getResponseNodes(cfg, siteKey);
    const baselineNodeSet = new Set(baselineNodes);
    const baselineResponseCount = baselineNodes.length;
    const baselineResponseText = getLatestResponseText(cfg, siteKey) || '';
    const HARD_TIMEOUT = 120000;
    const hardDeadlineAt = Date.now() + HARD_TIMEOUT;

    const reportResult = (payload) => {
      // 1. Try Tauri/WebKit IPC
      if (!ipcEmit(payload)) {
        // 2. Fallback: Image Ping to the Rust bridge orchestrator (CSP-safe)
        if (callbackUrl) {
          try {
            const pingUrl = callbackUrl.replace('/callback', '/ping');
            const encoded = btoa(unescape(encodeURIComponent(JSON.stringify(payload))));
            const img = new Image();
            img.src = `${pingUrl}?req_id=${encodeURIComponent(reqId)}&data=${encodeURIComponent(encoded)}`;
          } catch (e) {
            console.warn('[KimBridge] ping fallback failed:', e);
          }
        }
      }

      // 3. Fallback: Legacy store (for title polling). Only final payloads
      // go here; live progress is display-only and must never satisfy Rust's
      // completion collector.
      if (!payload.event || payload.event === 'done' || payload.event === 'error') {
        try {
          const encoded = btoa(unescape(encodeURIComponent(JSON.stringify(payload))));
          if (typeof window.__kimBridgeStore !== 'object' || window.__kimBridgeStore === null) window.__kimBridgeStore = {};
          window.__kimBridgeStore[reqId] = { ready: true, data: encoded, err: null, ts: Date.now() };
        } catch (_) {}
      }
    };

    const decodeJsonString = (value) => {
      try { return JSON.parse('"' + String(value || '').replace(/"/g, '\\"') + '"'); } catch (_) {}
      return String(value || '').replace(/\\"/g, '"').replace(/\\n/g, '\n').replace(/\\t/g, '\t');
    };

    // extractBridgeTextField removed (was dead code returning empty string)

    const cleanProgressText = (raw) => {
      let text = String(raw || '');
      if (completionHash) text = text.replace(completionHash, '');
      text = text.replace(/KIM_[a-f0-9]{8}/gi, '').trim();
      return text;
    };

    let lastProgressText = '';
    let lastProgressAt = 0;
    const reportProgress = (raw) => {
      const text = cleanProgressText(raw);
      if (!text || text === lastProgressText) return;
      const now = Date.now();
      if (now - lastProgressAt < 800 && text.length <= lastProgressText.length + 12) return;
      lastProgressText = text;
      lastProgressAt = now;
      const payload = { ok: true, event: 'progress', req_id: reqId, site: siteKey, text };
      ipcEmit(payload);
    };

    // Mark this as the current in-flight request. Any earlier send() call
    // still running will detect that it's been superseded and bail out
    // before clobbering shared state (input box, _lastHash, response polling).
    window.__kimBridge._currentReqId = reqId;
    const isSuperseded = () => window.__kimBridge._currentReqId !== reqId;

    try {
      // 0. Auto-enable DeepThink (R1) expert mode for DeepSeek
      if (siteKey === 'deepseek') {
        try {
          if (!window.__kimBridge._expertToggled) {
            const allEls = Array.from(document.querySelectorAll('*'));
            const expertText = allEls.find(el => {
              // Ensure we get the actual leaf element (like the span), not a giant wrapper div
              if (el.children.length > 0) return false;
              const text = el.textContent ? el.textContent.trim().toLowerCase() : '';
              return text === 'expert' || text === 'deepthink' || text === 'deepthink (r1)';
            });
            
            if (expertText) {
              console.log('[KimBridge] Enabling Expert Mode (first turn)...');
              
              // Many React/Vue apps ignore raw .click() in favor of pointer events
              const rect = expertText.getBoundingClientRect();
              const evOpts = { 
                bubbles: true, cancelable: true, 
                clientX: rect.x + rect.width/2, 
                clientY: rect.y + rect.height/2 
              };
              
              expertText.dispatchEvent(new PointerEvent('pointerdown', evOpts));
              expertText.dispatchEvent(new MouseEvent('mousedown', evOpts));
              expertText.dispatchEvent(new PointerEvent('pointerup', evOpts));
              expertText.dispatchEvent(new MouseEvent('mouseup', evOpts));
              expertText.click();
              
              window.__kimBridge._expertToggled = true;
              // Brief pause to let UI react
              await new Promise(r => setTimeout(r, 300));
            }
          }
        } catch (e) {
          console.warn('[KimBridge] Error toggling Expert mode:', e);
        }
      }

      if (siteKey === 'gemini') {
        const rawTier = (window.__kimModelTier == null ? '' : String(window.__kimModelTier)).trim().toLowerCase();
        const tier = rawTier === 'advanced' ? 'pro' : (rawTier === 'fast' ? 'flash' : rawTier);
        const hasExplicitTier = tier === 'flash' || tier === 'pro' || tier === 'thinking';
        if (!hasExplicitTier) {
          console.log('[KimBridge] Gemini model auto-switch skipped (no explicit model tier)');
        } else {
        const selectGeminiPro = async () => {
            let modelBtn = null;
            const walker1 = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
            while (walker1.nextNode()) {
                const node = walker1.currentNode;
                const txt = (node.textContent || '').replace(/[\\s\\u00A0]+/g, ' ').toLowerCase().trim();
                const words = txt.split(' ');
                
                if (words.includes('gemini') || words.includes('flash') || words.includes('fast') || words.includes('pro') || words.includes('advanced') || words.includes('thinking')) {
                    if (!txt.includes('upgrade') && !txt.includes('try') && !txt.includes('learn') && !txt.includes('help') && !txt.includes('chat')) {
                        const el = node.parentElement;
                        if (el && isVisible(el)) {
                            const clickable = el.closest('button, [role="button"], [role="combobox"], [aria-haspopup], .mat-mdc-button, .mat-mdc-menu-trigger');
                            if (clickable) {
                                modelBtn = clickable;
                                break;
                            }
                        }
                    }
                }
            }
            
            if (modelBtn) {
                const currentTxt = (modelBtn.textContent || '').replace(/[\\s\\u00A0]+/g, ' ').toLowerCase();
                const isTargetFlash = tier === 'flash' || tier === 'fast';
                const isTargetPro = tier === 'pro' || tier === 'advanced';
                const isTargetThinking = tier === 'thinking';
                
                let needsSwitch = false;
                if (isTargetFlash && !currentTxt.includes('flash') && !currentTxt.includes('fast')) {
                    if (currentTxt !== 'gemini') needsSwitch = true;
                }
                if (isTargetPro && !currentTxt.includes('advanced') && !currentTxt.includes('pro')) needsSwitch = true;
                if (isTargetThinking && !currentTxt.includes('thinking')) needsSwitch = true;
                
                if (needsSwitch) {
                    console.log(`[KimBridge] Switching Gemini Model Tier to ${tier}`);
                    modelBtn.click();
                    await new Promise(r => setTimeout(r, 700));
                    
                    let targetClicked = false;
                    const targetTerms = isTargetThinking ? ['thinking'] : (isTargetFlash ? ['flash', 'fast', 'gemini'] : ['advanced', 'pro']);
                    
                    const overlays = document.querySelectorAll('.cdk-overlay-container, [role="menu"], [role="listbox"], [role="dialog"]');
                    const menuRoot = Array.from(overlays).find(o => isVisible(o)) || document.body;
                    
                    const walker2 = document.createTreeWalker(menuRoot, NodeFilter.SHOW_TEXT, null, false);
                    let targetNode = null;
                    while (walker2.nextNode()) {
                        const node = walker2.currentNode;
                        const txt = (node.textContent || '').replace(/[\\s\\u00A0]+/g, ' ').toLowerCase();
                        if (targetTerms.some(t => txt.includes(t)) && !txt.includes('upgrade') && !txt.includes('learn')) {
                            if (isTargetFlash && (txt.includes('advanced') || txt.includes('pro') || txt.includes('thinking'))) continue;
                            const el = node.parentElement;
                            if (el && isVisible(el)) {
                                targetNode = el;
                                break;
                            }
                        }
                    }
                    
                    if (targetNode) {
                        const clickable = targetNode.closest('button, [role="button"], [role="menuitem"], [role="option"], a, li, mat-option') || targetNode;
                        clickable.click();
                        targetClicked = true;
                        await new Promise(r => setTimeout(r, 1200));
                    }

                    if (!targetClicked) {
                        console.warn(`[KimBridge] Failed to find target Gemini model '${tier}' in menu`);
                        document.body.click();
                        await new Promise(r => setTimeout(r, 200));
                    }
                } else {
                    console.log(`[KimBridge] Gemini Model already correct (${tier})`);
                }
            } else {
                console.warn('[KimBridge] Gemini Model selector button not found');
            }
        };
        await selectGeminiPro();
        }
      }

      // FIX 1 (K-EFFORT): best-effort on-page picker, defined in bridge_effort.js.
      await window.__kimApplyEffortIfNeeded(siteKey, effort);

      // 1. Find input (use shadow-DOM-aware finder for Gemini)
      const inputEl = (siteKey === 'gemini' ? findGeminiInput() : null) || findElement(cfg.input_selectors, { visible: true });
      if (!inputEl) {
        throw new Error(`Could not find input selector for ${siteKey}`);
      }
      inputEl.focus();

      const attachResult = await injectAttachments(cfg, inputEl, attachments,
        () => ipcEmit({ ok: true, event: 'native_paste', req_id: reqId, site: siteKey }));
      const uploadedCount = (attachResult && attachResult.count) || 0;
      // Instrument which upload strategy ran + whether a chip actually appeared + the
      // live bridge version, so success/failure is diagnosable from bridge_debug.log.
      // (Unknown 'attach_diag' event is logged by Rust then ignored — harmless.)
      if (attachments && attachments.length) {
        try {
          ipcEmit({ ok: true, event: 'attach_diag', req_id: reqId, site: siteKey,
            error: 'ATTACH _v=' + (window.__kimBridge && window.__kimBridge._v) + ' ' + JSON.stringify(attachResult) });
        } catch (_) {}
      }

      // 3. Inject text — verify with rAF loop instead of hardcoded sleep.
      // When attachments were expected but couldn't be injected (uploadedCount === 0),
      // append an explanatory note so the model can respond gracefully instead of
      // looping with repeated take_screenshot calls.
      const effectivePrompt = (attachments && attachments.length > 0 && uploadedCount === 0)
        ? prompt + '\n[System note: The screenshot could not be attached to this message (image upload unavailable in current interface). Please respond with what you can based on text context, or NEED_HELP if you truly cannot proceed without the image.]'
        : prompt;
      const injectedLen = await injectPromptText(inputEl, effectivePrompt);
      if (injectedLen < Math.min(8, normalizeText(effectivePrompt).length) || !promptMatchesInput(inputEl, effectivePrompt)) {
        console.warn('[KimBridge] Prompt text may not have been fully accepted by the chat input. Proceeding anyway.');
      }

      // Allow UI framework to sync state before sending
      await new Promise(r => setTimeout(r, 80));

      // 4a. Wait for the PREVIOUS request's completion hash to appear in the
      //     DOM before clicking Send.  This ensures we never interrupt an
      //     in-progress generation.  The hash is provider-agnostic — it lives
      //     in the response text itself, so it works regardless of UI changes.
      {
        const prevHash = window.__kimBridge._lastHash;
        if (prevHash) {
          const PRE_SEND_TIMEOUT = 120000; // up to 120s for long responses
          const preDeadline = Date.now() + PRE_SEND_TIMEOUT;
          let noStopCount = 0;
          while (Date.now() < preDeadline) {
            if (isSuperseded()) { console.log('[KimBridge] send superseded during pre-send wait, bailing'); ipcEmit({ ok: false, event: 'error', req_id: reqId, error: 'Request superseded by newer send', site: siteKey }); return; }
            const pageText = getLatestResponseText(cfg, siteKey) || '';
            if (pageText.includes(prevHash)) break;

            if (!isAnyStopVisible(cfg)) {
              noStopCount++;
              // If stop button is missing for ~1.5 seconds, assume done
              if (noStopCount >= 5) {
                console.log('[KimBridge] prevHash not found, but stop button is gone. Proceeding.');
                break;
              }
            } else {
              noStopCount = 0;
            }
            await new Promise(r => setTimeout(r, 300));
          }
          // Short settle delay removed for latency
        }
      }

      if (!promptMatchesInput(inputEl, effectivePrompt)) {
        throw new Error('Prompt changed after injection. Refusing to send a partial prompt.');
      }

      // 4b/4c. Submit and verify by the one reliable signal: the editor clearing.
      //   IMPORTANT: do NOT use the stop button as a "sent" signal. On a reused
      //   thread it reads as visible even when idle (confirmed via diag.stop=true
      //   while nothing was generating) — that false positive is exactly what made
      //   the old code skip its retry and hang the full 120s (issue #4). A successful
      //   submit clears the contenteditable; if our prompt is still sitting there the
      //   send never registered (send button disabled / synthetic Enter ignored), so
      //   re-attempt until it clears. Turn-1 (working path) clears immediately.
      const minPromptLen = Math.min(8, normalizeText(effectivePrompt).length);
      const inputCleared = () => {
        try { return readInputText(inputEl).length < minPromptLen && !promptMatchesInput(inputEl, effectivePrompt); }
        catch (_) { return false; }
      };
      const attemptSubmit = () => {
        inputEl.focus();
        // Prefer an enabled Send button; fall back to any visible one (the disabled
        // flag often lags right after programmatic injection); then an Enter keypress.
        const btn = findElement(cfg.send_selectors || [], { visible: true, enabled: true })
                 || findElement(cfg.send_selectors || [], { visible: true });
        if (btn) { try { btn.click(); return 'button'; } catch (_) {} }
        for (const type of ['keydown', 'keypress', 'keyup']) {
          inputEl.dispatchEvent(new KeyboardEvent(type, { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true }));
        }
        return 'enter';
      };

      let submitVia = attemptSubmit();
      let sendConfirmed = inputCleared();
      for (let v = 0; v < 6 && !sendConfirmed; v++) {
        await new Promise(r => setTimeout(r, 350));
        if (isSuperseded()) { ipcEmit({ ok: false, event: 'error', req_id: reqId, error: 'Request superseded by newer send', site: siteKey }); return; }
        if (inputCleared()) { sendConfirmed = true; break; }
        console.warn('[KimBridge] input not cleared after submit — re-attempting (try ' + (v + 1) + ')');
        submitVia = attemptSubmit();
      }
      console.log('[KimBridge] Message submitted via', submitVia, '— confirmed=' + sendConfirmed);

      // 4d. Record THIS request's hash AFTER submit so the NEXT request can wait for it
      if (completionHash) {
        window.__kimBridge._lastHash = completionHash;
      }

      // 5. Emit "sent" immediately via IPC only — NOT via the legacy store,
      //    because the store is used by Rust's title-pull fallback which can't
      //    distinguish 'sent' from 'done' (BridgeCompleteResponse has no event
      //    field).  Writing 'sent' to the store would cause the collector to
      //    return response=null before the LLM has finished.
      ipcEmit({ ok: true, event: 'sent', req_id: reqId, site: siteKey });

      // 6. Poll DOM for the completion hash
      //    The LLM was instructed to append the hash at the very end of its
      //    response.  When we see it in the page text, the response is complete
      //    and we know exactly which text belongs to this request.
      const POLL_MS = 300;
      let responseText = '';
      let lastTextLength = 0;
      let idleCount = 0;
      let sawNewResponse = false;
      let loops = 0;

      // Identify the response that belongs to THIS turn by element identity:
      // the newest visible response node that was not present at send time.
      // Falling back to the generic latest-text only when no new node appears.
      const findNewNode = () => {
        const nodes = getResponseNodes(cfg, siteKey);
        for (let i = nodes.length - 1; i >= 0; i--) {
          if (!baselineNodeSet.has(nodes[i])) return nodes[i];
        }
        return null;
      };
      // Extract the ANSWER text from a response container. Reading a node's bare
      // innerText is wrong on Gemini: <model-response> wraps a visually-hidden
      // "Gemini said" a11y heading around the real content, so innerText yields
      // "Gemini said …" (or just "Gemini said" before the body streams in). Drill
      // into the content sub-node and strip any "<Provider> said" label.
      const ANSWER_SUBSELECTORS = {
        gemini: ['message-content', '.response-content', '.markdown-main-panel', '.markdown'],
        chatgpt: ['div.markdown', '.prose'],
        deepseek: ['div.ds-markdown'],
      };
      const stripSaidLabel = (t) =>
        (t || '').replace(/^\s*(?:Gemini|Claude|ChatGPT|Grok|DeepSeek|Assistant)\s+said:?\s*/i, '').trim();
      const extractAnswerText = (node) => {
        if (!node) return '';
        const subs = ANSWER_SUBSELECTORS[siteKey];
        if (subs) {
          let best = '';
          for (const s of subs) {
            try {
              for (const el of Array.from(node.querySelectorAll(s))) {
                if (!isVisible(el)) continue;
                const t = nodeText(el);
                if (t.length > best.length) best = t;
              }
            } catch (_) {}
          }
          if (best) return stripSaidLabel(best);
        }
        return stripSaidLabel(nodeText(node));
      };

      // Authoritative text for this turn: prefer the new node's content; only fall
      // back to generic latest-text if it differs from the baseline answer (so we
      // never echo the PREVIOUS turn's response — the root cause of the dup bug).
      const turnText = () => {
        const nn = findNewNode();
        if (nn) {
          const t = extractAnswerText(nn);
          // While the body is still streaming the node may only hold the "Gemini
          // said" label; report empty so detection keeps waiting for real content.
          return { text: t, fromNewNode: true };
        }
        const lt = getLatestResponseText(cfg, siteKey) || '';
        return { text: (lt && lt !== baselineResponseText) ? lt : '', fromNewNode: false };
      };

      // Compact DOM snapshot for diagnostics — surfaced in the timeout error and
      // logged periodically so a recurrence is debuggable from logs alone.
      const diag = () => {
        const nodes = getResponseNodes(cfg, siteKey);
        const nn = findNewNode();
        const txt = nn ? extractAnswerText(nn) : (getLatestResponseText(cfg, siteKey) || '');
        let inputLen = -1;
        try { inputLen = readInputText(inputEl).length; } catch (_) {}
        return {
          loops,
          baseCount: baselineResponseCount,
          nowCount: nodes.length,
          newNode: !!nn,
          rawLen: nn ? nodeText(nn).length : 0,
          ansLen: txt.length,
          hash: !!(completionHash && txt.includes(completionHash)),
          stop: isAnyStopVisible(cfg),
          sawNew: sawNewResponse,
          idle: idleCount,
          inputLen, // >0 after send ⇒ message never left the input (send failed)
          head: txt.slice(0, 60),
        };
      };

      while (Date.now() < hardDeadlineAt) {
        await new Promise(r => setTimeout(r, POLL_MS));
        loops++;
        if (isSuperseded()) { console.log('[KimBridge] send superseded during response wait, bailing'); ipcEmit({ ok: false, event: 'error', req_id: reqId, error: 'Request superseded by newer send', site: siteKey }); return; }

        const { text: latestText, fromNewNode } = turnText();
        const latestCount = countResponseNodes(cfg, siteKey);
        if (fromNewNode || latestCount > baselineResponseCount || (latestText && latestText !== baselineResponseText)) {
          sawNewResponse = true;
        }
        reportProgress(latestText);
        if (loops % 17 === 0) console.log('[KimBridge] poll diag', JSON.stringify(diag()));

        // Fail fast rather than burn the full 120s hard deadline on dead air. Keyed
        // off real signals only (NOT the stop button, which false-positives on reused
        // threads). Only trips when NOTHING has appeared yet (sawNewResponse stays
        // false) — a genuine generation flips sawNewResponse within a second or two of
        // streaming, so this never aborts a real (even slow) response.
        if (!sawNewResponse) {
          let inputStuck = false;
          try { inputStuck = readInputText(inputEl).length >= minPromptLen; } catch (_) {}
          if (inputStuck && loops >= 33) {
            // ~10s: our prompt is still sitting in the editor → the send never registered.
            throw new Error(`Send did not register — prompt still in the input after ${loops} polls; diag=${JSON.stringify(diag())}`);
          }
          if (loops >= 200) {
            // ~60s: input cleared (message left) but no reply node ever appeared.
            throw new Error(`No response turn detected — nothing generated after ${loops} polls; diag=${JSON.stringify(diag())}`);
          }
        }

        if (completionHash && latestText.includes(completionHash)) {
          responseText = latestText.replace(completionHash, '').trim();
          break;
        }

        if (latestText.length > lastTextLength) {
          idleCount = 0;
          lastTextLength = latestText.length;
        } else if (latestText.length > 0) {
          idleCount++;
        }

        // Idle fallback. When the stop button is gone, settle quickly (3s of no
        // growth). The stop-button selector is per-site and can be unreliable on
        // a reused thread, so we ALSO have a stop-independent backstop: if a new
        // turn has clearly produced text and it has been stable for ~9s, accept
        // it rather than hanging for the full 120s.
        if (sawNewResponse && latestText.length > 0) {
          if (!isAnyStopVisible(cfg) && idleCount >= 10) {
            console.log('[KimBridge] stop gone + text idle — accepting turn text.', JSON.stringify(diag()));
            responseText = latestText;
            break;
          }
          if (idleCount >= 30) {
            console.log('[KimBridge] text idle ~9s (stop-button ignored) — accepting turn text.', JSON.stringify(diag()));
            responseText = latestText;
            break;
          }
        }
      }

      if (!responseText) {
        // Last-ditch: grab the new turn's text directly if present.
        const { text: lastTry } = turnText();
        if (completionHash && lastTry.includes(completionHash)) {
          responseText = lastTry.replace(completionHash, '').trim();
        } else if (lastTry.length > 0) {
          responseText = lastTry;
        } else {
          throw new Error(`Timed out waiting for completion hash in response (${HARD_TIMEOUT}ms); no new response turn detected — diag=${JSON.stringify(diag())}`);
        }
      }

      // 7. Emit "done" with full response
      reportResult({ ok: true, event: 'done', req_id: reqId, response: responseText, site: siteKey, attachments_uploaded: uploadedCount || 0 });

    } catch (err) {
      const message = (err && err.message) ? err.message : String(err);
      reportResult({ ok: false, event: 'error', req_id: reqId, error: message, site: site || 'unknown' });
    }
  };

  // ── Detect which site we're on ────────────────────────────────────────
  const detectSite = () => {
    const href = window.location.href.toLowerCase();
    if (href.includes('gemini.google.com')) return 'gemini';
    if (href.includes('chatgpt.com')) return 'chatgpt';
    if (href.includes('chat.deepseek.com')) return 'deepseek';
    return null;
  };

  // ── Check if page is ready (input element exists) ─────────────────────
  const checkReady = () => {
    const site = detectSite();
    if (!site) return false;
    const cfg = SITE_CONFIGS[site];
    if (!cfg) return false;
    if (site === 'gemini') return !!findGeminiInput();
    return !!findElement(cfg.input_selectors, { visible: true });
  };

  // ── Clear _lastHash when URL changes (new chat detected) ──────────────
  let _lastObservedUrl = window.location.href;
  setInterval(() => {
    const currentUrl = window.location.href;
    if (currentUrl !== _lastObservedUrl) {
      _lastObservedUrl = currentUrl;
      if (window.__kimBridge) {
        window.__kimBridge._lastHash = null;
        console.log('[KimBridge] URL changed, cleared _lastHash');
      }
    }
  }, 1000);

  // ── Public API ─────────────────────────────────────────────────────────
  window.__kimBridge = {
    _v: 19,
    _lastHash: null, // Tracks the completion hash of the most recent request
    _currentReqId: null, // Tracks the in-flight req_id; older send()s bail when this changes
    send,
    detectSite,
    checkReady,
    SITE_CONFIGS,
    // Legacy compat
    getLatestResponseText: (site) => {
      const siteKey = SITE_CONFIGS[site] ? site : 'chatgpt';
      return getLatestResponseText(SITE_CONFIGS[siteKey], siteKey);
    },
  };

  console.log('[KimBridge] Persistent bridge v2 installed for', detectSite() || 'unknown site');
})();

//! In-app webview bridge engine: the persistent bridge JS, the browser
//! sign-in window, payload collection, and one-shot bridge completion.
//! Extracted from lib.rs (file-split restructure) — behavior unchanged.

use std::collections::HashMap;
use std::time::{Duration, Instant};
use base64::Engine as _;
use tauri::{Emitter, Listener, Manager};

use crate::*;

pub(crate) const PERSISTENT_BRIDGE_JS: &str = include_str!("bridge.js");

pub(crate) fn open_browser_signin_window_impl(
    url: &str,
    provider_name: Option<String>,
    app_handle: &tauri::AppHandle,
) -> Result<String, String> {
    open_browser_signin_window_with_visibility(url, provider_name, true, app_handle)
}

/// Underlying implementation that lets callers create the window in a hidden
/// offscreen state. Used by `restore_browser_for_session` (we want the webview
/// alive so cookies/session URL hydrate, but the user should not see a popup
/// appear every time they click a chat in the sidebar) and by the auth-probe
/// path (we want to silently fetch /api/auth/session in the background).
pub(crate) fn open_browser_signin_window_with_visibility(
    url: &str,
    provider_name: Option<String>,
    initially_visible: bool,
    app_handle: &tauri::AppHandle,
) -> Result<String, String> {
    let trimmed = url.trim();
    if trimmed.is_empty() {
        return Err("URL cannot be empty.".to_string());
    }

    let parsed = tauri::Url::parse(trimmed)
        .map_err(|e| format!("Invalid URL: {}", e))?;
    match parsed.scheme() {
        "https" | "http" => {}
        _ => return Err("Only http:// or https:// URLs are allowed.".to_string()),
    }

    let label = "kim-browser-signin";
    if let Some(existing) = app_handle.get_webview_window(label) {
        let task_running = is_bridge_task_running();

        if task_running {
            return Ok(
                "Reused existing Kim browser window without navigation because a task is active."
                    .to_string(),
            );
        }

        let js_url = serde_json::to_string(trimmed).map_err(|e| e.to_string())?;
        let _ = existing.eval(format!("window.location.href = {};", js_url));
        // Preserve current visibility: if the caller wants invisible and the
        // window is currently shown, hide it; if visible and currently hidden,
        // bring it back into view.
        if initially_visible && is_browser_window_offscreen(&existing) {
            show_browser_window_impl(app_handle);
        } else if !initially_visible && !is_browser_window_offscreen(&existing) {
            hide_browser_window_offscreen(&existing);
        }
        return Ok("Navigated existing Kim browser window".to_string());
    }

    if is_bridge_task_running() {
        return Err(
            "Cannot open the provider browser while Kim is running a task; this would lose LLM context."
                .to_string(),
        );
    }

    let title = provider_name
        .map(|name| format!("Kim Browser - {}", name))
        .unwrap_or_else(|| "Kim Browser".to_string());

    let window = tauri::WebviewWindowBuilder::new(
        app_handle,
        label,
        tauri::WebviewUrl::External(parsed),
    )
    .title(title)
    .inner_size(1280.0, 860.0)
    .resizable(true)
    .visible(initially_visible)
    .initialization_script(PERSISTENT_BRIDGE_JS)
    .build()
    .map_err(|e| format!("Failed to open Kim browser window: {}", e))?;

    // If we built it invisible, immediately move it offscreen so that later
    // calls to `is_browser_window_offscreen` recognise it as hidden and so the
    // page still has a real viewport for layout (required for the bridge JS).
    if !initially_visible {
        hide_browser_window_offscreen(&window);
    }

    let window_for_close = window.clone();
    let app_for_close = app_handle.clone();
    window.on_window_event(move |event| {
        if let tauri::WindowEvent::CloseRequested { api, .. } = event {
            // Keep the webview session alive for background/headless execution.
            // Hide the window instead of closing it so the DOM keeps rendering.
            api.prevent_close();
            hide_browser_window_offscreen(&window_for_close);

            // Refocus the main app window so the user isn't left staring at nothing.
            if let Some(main_win) = app_for_close.get_webview_window("main") {
                let _ = main_win.show();
                let _ = main_win.set_focus();
            }
            // Notify the frontend that the browser window was dismissed.
            let _ = app_for_close.emit("kim-browser-hidden", true);
        }
    });

    // Listen for IPC events from the persistent JS bridge only once per app instance.
    IPC_LISTENER_REGISTERED.get_or_init(|| {
        let app_for_listener = app_handle.clone();
        let app_for_event = app_for_listener.clone();
        app_for_listener.listen("kim-bridge-ipc", move |event| {
            let payload_str = event.payload();
            match serde_json::from_str::<BridgeIpcEvent>(payload_str) {
                Ok(ipc_event) => {
                    handle_bridge_ipc_event(ipc_event, &app_for_event);
                }
                Err(e) => {
                    eprintln!(
                        "[Kim] Failed to parse bridge IPC event: {} — payload: {}",
                        e,
                        &payload_str[..payload_str.len().min(200)]
                    );
                }
            }
        });
    });

    if let Some(existing) = app_handle.get_webview_window(label) {
        // Only steal focus if the window is actually visible on-screen.
        // In headless mode (offscreen at -10000,-10000) we must NOT focus
        // because it would pull the user away from their current page.
        if !is_browser_window_offscreen(&existing) {
            let _ = existing.set_focus();
        }
    }

    Ok("Opened in Kim browser window".to_string())
}

#[tauri::command]
pub(crate) async fn add_custom_provider_capability(url: String, app_handle: tauri::AppHandle) -> Result<(), String> {
    use tauri::Manager;
    use tauri::ipc::CapabilityBuilder;

    let parsed = tauri::Url::parse(&url).map_err(|e| e.to_string())?;
    let origin = parsed.origin().ascii_serialization();
    let capability_url = format!("{}/*", origin);

    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let cap_id = format!("kim-custom-bridge-{}", nanos);

    let cap = CapabilityBuilder::new(cap_id)
        .remote(capability_url)
        .window("kim-browser-signin")
        .permission("core:default")
        .permission("core:event:allow-emit");

    app_handle.add_capability(cap).map_err(|e| format!("Failed to add runtime capability: {}", e))?;
    Ok(())
}

/// Handle an IPC event from the persistent JS bridge.
/// Inserts results into the shared store and wakes up any waiting collector.
pub(crate) fn clean_bridge_progress_text(text: &str) -> Option<String> {
    let mut cleaned = text
        .replace('\u{0000}', "")
        .replace("\r\n", "\n")
        .trim()
        .to_string();

    while cleaned.contains("\n\n\n") {
        cleaned = cleaned.replace("\n\n\n", "\n\n");
    }
    cleaned = cleaned
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .collect::<Vec<_>>()
        .join(" ");

    let lower = cleaned.to_lowercase();
    if cleaned.len() < 8
        || lower.starts_with("error:")
        || lower.starts_with("need_help:")
        || lower.starts_with("{text")
        || lower.starts_with("{\"text")
        || lower.starts_with("{ \"text")
        || lower.starts_with('{')
        || lower.contains("\"tool_calls\"")
        || lower.contains("\"content\"")
        || lower.contains("<!doctype")
        || lower.contains("<html")
        || lower.contains("<style")
        || lower.contains("kim_")
    {
        return None;
    }

    if cleaned.chars().filter(|ch| *ch == '{' || *ch == '}' || *ch == '\\').count() > 8 {
        return None;
    }

    if cleaned.len() > 280 {
        cleaned.truncate(277);
        cleaned.push_str("...");
    }

    Some(cleaned)
}

pub(crate) fn emit_bridge_progress(app_handle: &tauri::AppHandle, req_id: &str, text: &str) {
    let Some(cleaned) = clean_bridge_progress_text(text) else {
        return;
    };

    let progress_store = WEBVIEW_BRIDGE_PROGRESS.get_or_init(|| StdMutex::new(HashMap::new()));
    if let Ok(mut guard) = progress_store.lock() {
        if guard.get(req_id).map(|last| last == &cleaned).unwrap_or(false) {
            return;
        }
        guard.insert(req_id.to_string(), cleaned.clone());
    }

    let _ = app_handle.emit("kim-agent-output", format!("[STATUS] {}", cleaned));
}

pub(crate) fn handle_bridge_ipc_event(ipc_event: BridgeIpcEvent, app_handle: &tauri::AppHandle) {
    agent_debug_log(
        "H1",
        "bridge IPC event received",
        serde_json::json!({
            "event": ipc_event.event,
            "reqId": ipc_event.req_id,
            "hasResponse": ipc_event.response.is_some(),
            "hasError": ipc_event.error.is_some(),
            "hasText": ipc_event.text.is_some(),
        }),
    );

    match ipc_event.event.as_str() {
        "sent" => {
            // Prompt was injected and Enter pressed. Store a "sent" marker
            // so the /v1/send endpoint can return immediately.
            let store = WEBVIEW_BRIDGE_RESULTS.get_or_init(|| StdMutex::new(HashMap::new()));
            if let Ok(mut guard) = store.lock() {
                let sent_key = format!("{}_sent", ipc_event.req_id);
                guard.insert(sent_key, BridgeCompleteResponse {
                    ok: true,
                    response: None,
                    error: None,
                    site: ipc_event.site,
                });
            }
            // Wake up anyone waiting for the "sent" signal.
            notify_bridge_result();
        }
        "done" => {
            if let Ok(mut guard) = WEBVIEW_BRIDGE_PROGRESS
                .get_or_init(|| StdMutex::new(HashMap::new()))
                .lock()
            {
                guard.remove(&ipc_event.req_id);
            }
            let store = WEBVIEW_BRIDGE_RESULTS.get_or_init(|| StdMutex::new(HashMap::new()));
            if let Ok(mut guard) = store.lock() {
                guard.insert(ipc_event.req_id.clone(), BridgeCompleteResponse {
                    ok: true,
                    response: ipc_event.response,
                    error: None,
                    site: ipc_event.site,
                });
            }
            notify_bridge_result();
        }
        "progress" => {
            if let Some(text) = ipc_event.text.as_deref() {
                emit_bridge_progress(app_handle, &ipc_event.req_id, text);
            }
        }
        "error" => {
            if let Ok(mut guard) = WEBVIEW_BRIDGE_PROGRESS
                .get_or_init(|| StdMutex::new(HashMap::new()))
                .lock()
            {
                guard.remove(&ipc_event.req_id);
            }
            let store = WEBVIEW_BRIDGE_RESULTS.get_or_init(|| StdMutex::new(HashMap::new()));
            if let Ok(mut guard) = store.lock() {
                guard.insert(ipc_event.req_id.clone(), BridgeCompleteResponse {
                    ok: false,
                    response: None,
                    error: ipc_event.error,
                    site: ipc_event.site,
                });
            }
            notify_bridge_result();
        }
        other => {
            eprintln!("[Kim] Unknown bridge IPC event type: {}", other);
        }
    }
}

/// Wake up any thread waiting on WEBVIEW_BRIDGE_NOTIFY.
pub(crate) fn notify_bridge_result() {
    let (_, condvar) = WEBVIEW_BRIDGE_NOTIFY.get_or_init(|| {
        (StdMutex::new(()), Condvar::new())
    });
    condvar.notify_all();
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn build_bridge_complete_script(
    site: &str,
    prompt: &str,
    req_id: &str,
    attachments: &[BridgeAttachment],
    callback_url: &str,
    callback_token: &str,
    completion_hash: Option<&str>,
    model_tier: Option<&str>,
) -> Result<String, String> {
    let site_json = serde_json::to_string(site).map_err(|e| e.to_string())?;
    let prompt_json = serde_json::to_string(prompt).map_err(|e| e.to_string())?;
    let req_json = serde_json::to_string(req_id).map_err(|e| e.to_string())?;
    let attachments_json = serde_json::to_string(attachments).map_err(|e| e.to_string())?;
    let callback_url_json = serde_json::to_string(callback_url).map_err(|e| e.to_string())?;
    let callback_token_json = serde_json::to_string(callback_token).map_err(|e| e.to_string())?;
    let hash_json = serde_json::to_string(&completion_hash).unwrap_or_else(|_| "null".to_string());

    let header = format!(r#"(() => {{
    setTimeout(async () => {{
    try {{
  const __kimSite = {site};
  const __kimPrompt = {prompt};
  const __kimReqId = {req_id};
  const __kimAttachments = {attachments};
  const __kimCallbackUrl = {callback_url};
  const __kimCallbackToken = {callback_token};
  const __kimCompletionHash = {hash};
  window.__kimModelTier = {tier};
    let __kimFinished = false;
    let __kimWatchdog = null;
"#,
        site=site_json,
        prompt=prompt_json,
        req_id=req_json,
        attachments=attachments_json,
        callback_url=callback_url_json,
        callback_token=callback_token_json,
        hash=hash_json,
        tier=serde_json::to_string(&model_tier).unwrap_or_else(|_| "null".to_string())
    );

    let body = r#"
  const SITE_CONFIGS = {
    claude: {
      input_selectors: ["div[contenteditable='true'].ProseMirror", "div[contenteditable='true']"],
            send_selectors: ["button[aria-label*='Send message']", "button[aria-label*='Send']", "button[aria-label*='send']"],
    stop_selectors: ["button[aria-label*='Stop']"],
      response_selectors: ["[data-testid^='conversation-turn']", ".font-claude-message"],
            upload_button_selectors: ["button[aria-label*='Attach']", "button[aria-label*='Upload']"],
            file_input_selectors: ["input[type='file']"],
    },
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
        send_selectors: ["button[aria-label*='Send message']", "button[aria-label*='Send']", "button[data-testid*='send']", "button[mattooltip*='Send']"],
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
    grok: {
      input_selectors: ["textarea", "div[contenteditable='true']"],
      send_selectors: ["button[aria-label*='Send']", "button[type='submit']"],
      stop_selectors: ["button[aria-label*='Stop']"],
      response_selectors: ["article", "div.markdown", "[data-testid*='message']"],
            upload_button_selectors: ["button[aria-label*='Upload']", "button[aria-label*='Attach']"],
            file_input_selectors: ["input[type='file']"],
    },
  };

  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const RESPONSE_WAIT_MS = 45000;
    const STOP_APPEAR_WAIT_MS = 5000;
    const GENERATION_DONE_WAIT_MS = 60000;
    const READ_WAIT_MS = 10000;
    const HARD_SCRIPT_DEADLINE_MS = 90000;
    const hardDeadlineAt = Date.now() + HARD_SCRIPT_DEADLINE_MS;

    const ensureWithinDeadline = (stage) => {
        if (Date.now() > hardDeadlineAt) {
            throw new Error(`Hard timeout at ${stage} after ${HARD_SCRIPT_DEADLINE_MS}ms`);
        }
    };

  // No-op: previously fetched to localhost:7243 which CSP blocks on every provider page,
  // serializing the script on dozens of blocked network calls.  Silent no-op is safe
  // because Rust already logs bridge lifecycle via agent_debug_log.
  const __kimDbg = () => {};

  const emitPayload = async (payload) => {
        if (__kimFinished) return;
        __kimFinished = true;
        if (__kimWatchdog) {
            clearTimeout(__kimWatchdog);
            __kimWatchdog = null;
        }
    // #region agent log
    __kimDbg('H3', 'emitPayload called', { ok: !!payload?.ok, hasError: !!payload?.error, site: payload?.site || __kimSite || 'unknown' });
    // #endregion
    try {
      const encoded = btoa(unescape(encodeURIComponent(JSON.stringify(payload))));
            // Store payload in a per-request map so retries/cancellations cannot
            // leak stale data from a previous req_id.
            if (typeof window.__kimBridgeStore !== 'object' || window.__kimBridgeStore === null) {
                window.__kimBridgeStore = {};
            }
            window.__kimBridgeStore[__kimReqId] = {
                data: encoded,
                err: null,
                ts: Date.now(),
            };
    } catch (err) {
            if (typeof window.__kimBridgeStore !== 'object' || window.__kimBridgeStore === null) {
                window.__kimBridgeStore = {};
            }
            window.__kimBridgeStore[__kimReqId] = {
                data: null,
                err: String(err),
                ts: Date.now(),
            };
    }

    // Payload delivery: JS just stores the result in window.__kimBridgeStore[reqId].
    // Rust reads it via a tight eval+title roundtrip (see pull_payload_from_js_store).
    // No title pulsing, no Angular race, no CSP-blocked HTTP callback.
  };

    const __KIM_WATCHDOG_MS = 95000;
    __kimWatchdog = setTimeout(() => {
        if (__kimFinished) return;
        emitPayload({
            ok: false,
            error: `Bridge script watchdog timeout after ${__KIM_WATCHDOG_MS}ms`,
            site: __kimSite || 'unknown',
        });
    }, __KIM_WATCHDOG_MS);

  const findSelector = (selectors) => {
    for (const sel of selectors || []) {
      try {
        if (document.querySelector(sel)) return sel;
      } catch (_) {}
    }
    return null;
  };

    const isVisible = (el) => {
        if (!el) return false;
        const style = getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity || '1') === 0) {
            return false;
        }
        if (el.offsetParent !== null) return true;
        return style.position === 'fixed';
    };

    const isEnabled = (el) => {
        if (!el) return false;
        if ('disabled' in el && el.disabled) return false;
        if (el.getAttribute && el.getAttribute('aria-disabled') === 'true') return false;
        return true;
    };

    const findElement = (selectors, opts = { visible: false, enabled: false, last: false }) => {
        for (const sel of selectors || []) {
            let nodes = [];
            try {
                nodes = Array.from(document.querySelectorAll(sel));
            } catch (_) {
                continue;
            }
            if (opts.last) {
                nodes = nodes.reverse();
            }
            for (const el of nodes) {
                if (opts.visible && !isVisible(el)) continue;
                if (opts.enabled && !isEnabled(el)) continue;
                return el;
            }
        }
        return null;
    };

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
                try { btn.click(); await sleep(200); } catch (_) {}
            }
        }
    };

    const inferExtension = (mime) => {
        const m = String(mime || '').toLowerCase();
        const extMap = {
            'image/png': 'png',
            'image/jpeg': 'jpg',
            'image/jpg': 'jpg',
            'image/webp': 'webp',
            'image/gif': 'gif',
            'image/svg+xml': 'svg',
            'application/pdf': 'pdf',
            'text/plain': 'txt',
            'text/markdown': 'md',
            'application/json': 'json',
            'application/zip': 'zip',
        };
        if (extMap[m]) return extMap[m];
        if (m.includes('/')) {
            const tail = m.split('/')[1].split('+')[0];
            if (tail) return tail;
        }
        return 'bin';
    };

    const decodeBase64 = (b64) => {
        const binary = atob(b64);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) {
            bytes[i] = binary.charCodeAt(i);
        }
        return bytes;
    };

    const makeFileFromAttachment = (att, idx) => {
        const mime = String(att?.mime_type || 'application/octet-stream');
        const dataB64 = String(att?.data_base64 || '');
        if (!dataB64) return null;
        const bytes = decodeBase64(dataB64);
        const blob = new Blob([bytes], { type: mime });
        const fallbackName = `attachment_${idx + 1}.${inferExtension(mime)}`;
        const name = String(att?.name || fallbackName).trim() || fallbackName;
        return new File([blob], name, { type: mime });
    };

    const injectAttachments = async (cfg, inputEl) => {
        const source = Array.isArray(__kimAttachments) ? __kimAttachments : [];
        if (!source.length) return 0;

        const files = [];
        for (let i = 0; i < source.length; i++) {
            try {
                const file = makeFileFromAttachment(source[i], i);
                if (file) files.push(file);
            } catch (_) {}
        }
        if (!files.length) return 0;

        const findFileInput = () => {
            for (const sel of cfg.file_input_selectors || []) {
                try {
                    const el = document.querySelector(sel);
                    if (el && el instanceof HTMLInputElement && el.type === 'file') {
                        return el;
                    }
                } catch (_) {}
            }
            return null;
        };

        let fileInput = findFileInput();
        if (!fileInput) {
            const uploadBtn = findElement(cfg.upload_button_selectors, { visible: true, enabled: true, last: true });
            if (uploadBtn) {
                try {
                    uploadBtn.click();
                    await sleep(280);
                    fileInput = findFileInput();
                } catch (_) {}
            }
        }

        if (fileInput) {
            const dt = new DataTransfer();
            for (const file of files) {
                dt.items.add(file);
            }

            try {
                fileInput.files = dt.files;
            } catch (_) {
                try {
                    Object.defineProperty(fileInput, 'files', {
                        value: dt.files,
                        configurable: true,
                    });
                } catch (_) {}
            }

            fileInput.dispatchEvent(new Event('input', { bubbles: true }));
            fileInput.dispatchEvent(new Event('change', { bubbles: true }));
            await sleep(700);
            return files.length;
        }

        // Fallback: image clipboard paste via ClipboardEvent('paste').
        // Synthetic KeyboardEvent('keydown', {key:'v'}) does NOT trigger the
        // browser's native paste handler — only a real ClipboardEvent with
        // clipboardData will be picked up by ProseMirror / React editors.
        const imageFile = files.find(f => String(f.type || '').startsWith('image/'));
        if (imageFile && inputEl) {
            try {
                inputEl.focus();
                await sleep(300);

                // Build a DataTransfer with the image file
                const dt2 = new DataTransfer();
                dt2.items.add(imageFile);

                // Dispatch a real ClipboardEvent('paste') — this is what editors listen for
                const pasteEvent = new ClipboardEvent('paste', {
                    bubbles: true,
                    cancelable: true,
                    clipboardData: dt2,
                });
                inputEl.dispatchEvent(pasteEvent);
                __kimDbg('H1', 'Dispatched ClipboardEvent paste with image', { name: imageFile.name, type: imageFile.type });

                // Give the UI time to process the pasted image (thumbnail render, upload)
                let waited = 0;
                while (waited < 5000) {
                    await sleep(200);
                    waited += 200;
                    if (document.querySelector('img[src^="blob:"], img[src^="data:"], file-attachment, thumbnail-view')) {
                        break;
                    }
                }

                // Dismiss any consent popups (like Gemini's "I agree" dialog)
                await dismissPopups();

                return 1;
            } catch (e) {
                __kimDbg('H1', 'ClipboardEvent paste fallback failed', { error: String(e) });
            }
        }

        return 0;
    };

    const normalizeText = (value) => String(value || '').replace(/\u00a0/g, ' ').replace(/[“”]/g, '"').replace(/[‘’]/g, "'").replace(/[—–]/g, '--').replace(/…/g, '...').trim();

    const selectorCounts = (selectors) => {
        const out = {};
        for (const sel of selectors || []) {
            try {
                out[sel] = document.querySelectorAll(sel).length;
            } catch (_) {
                out[sel] = -1;
            }
        }
        return out;
    };

    const isLikelyUserNode = (node) => {
        if (!node) return false;
        try {
            if (node.closest(
                'user-query, [data-message-author-role="user"], [data-role="user"], [data-author="user"], '
                + '.user-message, .from-user, .query-content, .prompt-bubble, [data-testid*="user-message"]'
            )) {
                return true;
            }
        } catch (_) {}
        try {
            const author = String(
                node.getAttribute?.('data-message-author-role')
                || node.getAttribute?.('data-role')
                || node.getAttribute?.('data-author')
                || ''
            ).toLowerCase();
            if (author === 'user') return true;
        } catch (_) {}
        try {
            const cls = String(node.className || '').toLowerCase();
            if (cls.includes('user') && !cls.includes('assistant')) return true;
        } catch (_) {}
        return false;
    };

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
        if (!expected) return true;
        if (actual === expected) return true;
        if (expected.length < 500) return false;
        if (actual.length < Math.floor(expected.length * 0.98)) return false;
        
        const fuzzyMatch = (a, b) => a.replace(/\W+/g, '') === b.replace(/\W+/g, '');
        return fuzzyMatch(actual.slice(0, 220), expected.slice(0, 220))
            && fuzzyMatch(actual.slice(-220), expected.slice(-220));
    };

    const injectPromptText = async (inputEl, promptText) => {
        const target = String(promptText || '');
        try {
            inputEl.focus();
            document.execCommand('selectAll', false);
            if (document.execCommand('paste')) {
                await sleep(120);
                if (promptMatchesInput(inputEl, target)) return readInputText(inputEl).length;
            }
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

        let inserted = false;
        try {
            inputEl.focus();
            document.execCommand('selectAll', false);
            inserted = document.execCommand('insertText', false, target);
        } catch (_) {}

        const currentText = readInputText(inputEl);
        if (!inserted || currentText.length < Math.min(8, target.length)) {
            inputEl.textContent = '';
            const lines = target.split('\n');
            for (const line of lines) {
                const div = document.createElement('div');
                div.textContent = line;
                inputEl.appendChild(div);
            }
        }

        // Gemini rich-textarea keeps its source-of-truth in a shadow-DOM textarea.
        // querySelector on the host element cannot reach shadow DOM; check shadowRoot too.
        try {
            const rich = inputEl.closest('rich-textarea');
            if (rich) {
                const mirror = rich.querySelector('textarea, input')
                    || (rich.shadowRoot && rich.shadowRoot.querySelector('textarea, input'));
                if (mirror && (mirror instanceof HTMLTextAreaElement || mirror instanceof HTMLInputElement)) {
                    const proto = Object.getPrototypeOf(mirror);
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                    if (setter) setter.call(mirror, target); else mirror.value = target;
                    mirror.dispatchEvent(new Event('input', { bubbles: true }));
                    mirror.dispatchEvent(new Event('change', { bubbles: true }));
                    mirror.dispatchEvent(new Event('input', { bubbles: true }));
                }
            }
        } catch (_) {}

        try {
            inputEl.dispatchEvent(new InputEvent('beforeinput', {
                data: target,
                inputType: 'insertText',
                bubbles: true,
                cancelable: true,
            }));
        } catch (_) {}
        try {
            inputEl.dispatchEvent(new InputEvent('input', {
                data: target,
                inputType: 'insertText',
                bubbles: true,
                cancelable: true,
            }));
        } catch (_) {}
        inputEl.dispatchEvent(new Event('input', { bubbles: true }));
        inputEl.dispatchEvent(new Event('change', { bubbles: true }));
        await sleep(120);
        return readInputText(inputEl).length;
    };

    const gatherResponseCandidates = (cfg, siteKey) => {
        const candidates = [];
        const seenNodes = new Set();
        for (const sel of cfg.response_selectors || []) {
            let nodes = [];
            try {
                nodes = Array.from(document.querySelectorAll(sel));
            } catch (_) {
                continue;
            }
            for (const node of nodes) {
                if (!node || seenNodes.has(node)) continue;
                seenNodes.add(node);
                if (!isVisible(node)) continue;
                if (isLikelyUserNode(node)) continue;

                // Gemini is especially noisy: only trust model-response subtree.
                if (siteKey === 'gemini') {
                    const isModelResponse = (
                        (node.matches && node.matches('model-response'))
                        || (node.closest && node.closest('model-response'))
                    );
                    if (!isModelResponse) continue;
                }

                const text = normalizeText(node.innerText || node.textContent || '');
                if (!text || text.length < 3) continue;

                candidates.push({
                    node,
                    selector: sel,
                    text,
                    key: `${sel}::${text.length}::${text.slice(0, 200)}::${text.slice(-200)}`,
                });
            }
        }

        candidates.sort((a, b) => {
            if (a.node === b.node) return 0;
            const pos = a.node.compareDocumentPosition(b.node);
            if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
            if (pos & Node.DOCUMENT_POSITION_PRECEDING) return 1;
            return 0;
        });

        return candidates;
    };

    const getGeminiLatestResponseText = () => {
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
            bestModelNode = lastNode;
            bestModelIndex = lastIndex;
        }

        return {
            text: bestText,
            modelNode: bestModelNode,
            modelIndex: bestModelIndex,
        };
    };

    const captureResponseState = (cfg, siteKey) => {
        if (siteKey === 'gemini') {
            const snapshot = getGeminiLatestResponseText();
            const latestText = snapshot?.text || '';
            const latestNodeIndex = Number.isInteger(snapshot?.modelIndex) ? snapshot.modelIndex : -1;
            return {
                count: latestNodeIndex >= 0 ? latestNodeIndex + 1 : (latestText ? 1 : 0),
                keys: latestNodeIndex >= 0 ? ['gemini-node-index::' + latestNodeIndex] : [],
                latestText,
                latestNodeIndex,
            };
        }
        const candidates = gatherResponseCandidates(cfg, siteKey);
        return {
            count: candidates.length,
            keys: candidates.map(c => c.key),
            latestText: candidates.length > 0 ? candidates[candidates.length - 1].text : '',
        };
    };

    const hasNewResponseSince = (baselineState, currentState) => {
        // A valid current Gemini node index means we have a response node now.
        // Treat it as new if baseline had no valid node, or if the index advanced.
        if (
            Number.isInteger(currentState?.latestNodeIndex) &&
            currentState.latestNodeIndex >= 0 &&
            (
                !Number.isInteger(baselineState?.latestNodeIndex) ||
                baselineState.latestNodeIndex < 0 ||
                currentState.latestNodeIndex > baselineState.latestNodeIndex
            )
        ) return true;

        if ((currentState?.count || 0) > (baselineState?.count || 0)) return true;
        const baselineKeys = new Set((baselineState?.keys || []));
        if ((currentState?.keys || []).some(k => !baselineKeys.has(k))) return true;
        const baselineText = normalizeText(baselineState?.latestText || '');
        const currentText = normalizeText(currentState?.latestText || '');
        return !!currentText && currentText !== baselineText;
    };

    const extractLatestResponseText = (cfg, siteKey) => {
        return captureResponseState(cfg, siteKey).latestText;
    };

    const hasStopSemantics = (el) => {
        const label = String(
            (el && el.getAttribute && el.getAttribute('aria-label'))
            || (el && el.textContent)
            || ''
        ).toLowerCase().trim();
        if (!label) return false;
        return /(^|\b)stop(\b|$)/i.test(label) || label.includes('stop generating');
    };

    const isAnyStopVisible = (cfg) => {
        for (const sel of cfg.stop_selectors || []) {
            try {
                const nodes = Array.from(document.querySelectorAll(sel));
                for (const el of nodes) {
                    if (el && isVisible(el) && hasStopSemantics(el)) {
                        return true;
                    }
                }
            } catch (_) {}
        }
        return false;
    };

  try {
    const siteKey = SITE_CONFIGS[__kimSite] ? __kimSite : 'claude';
    const cfg = SITE_CONFIGS[siteKey];
        const selectorDiag = {
            input: selectorCounts(cfg.input_selectors),
            send: selectorCounts(cfg.send_selectors),
            stop: selectorCounts(cfg.stop_selectors),
            response: selectorCounts(cfg.response_selectors),
        };
        const selectorDiagText = JSON.stringify(selectorDiag);

        __kimDbg('H2', 'selector diagnostics', {
            siteKey,
            ...selectorDiag,
        });

        const baselineState = captureResponseState(cfg, siteKey);
        const initialResponseText = baselineState.latestText;
    // #region agent log
        __kimDbg('H2', 'bridge run start', {
            siteKey,
            baselineCount: baselineState.count,
            initialResponseTextLen: (initialResponseText || '').length,
        });
    // #endregion

                const inputEl = (siteKey === 'gemini' ? findGeminiInput() : null) || findElement(cfg.input_selectors, { visible: true, enabled: false });
        if (!inputEl) {
            throw new Error(`Could not find input selector for ${siteKey}. selectorDiag=${selectorDiagText}`);
    }
    inputEl.focus();

        if (siteKey === 'gemini') {
            const rawTier = (window.__kimModelTier == null ? '' : String(window.__kimModelTier)).trim().toLowerCase();
            const tier = rawTier === 'advanced' ? 'pro' : (rawTier === 'fast' ? 'flash' : rawTier);
            const hasExplicitTier = tier === 'flash' || tier === 'pro' || tier === 'thinking';
            if (!hasExplicitTier) {
                __kimDbg('H1', 'Gemini model auto-switch skipped (no explicit model tier)');
            } else {
            const selectGeminiPro = async () => {
                let modelBtn = null;
                const walker1 = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
                while (walker1.nextNode()) {
                    const node = walker1.currentNode;
                    const txt = normalizeText(node.textContent).toLowerCase().trim();
                    const words = txt.split(/\s+/);
                    
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
                    const currentTxt = normalizeText(modelBtn.textContent || '').toLowerCase();
                    const isTargetFlash = tier === 'flash' || tier === 'fast';
                    const isTargetPro = tier === 'pro' || tier === 'advanced';
                    const isTargetThinking = tier === 'thinking';
                    
                    let needsSwitch = false;
                    if (isTargetFlash && !currentTxt.includes('flash') && !currentTxt.includes('fast')) {
                        // If it's just 'gemini', it's likely flash.
                        if (currentTxt !== 'gemini') needsSwitch = true;
                    }
                    if (isTargetPro && !currentTxt.includes('advanced') && !currentTxt.includes('pro')) needsSwitch = true;
                    if (isTargetThinking && !currentTxt.includes('thinking')) needsSwitch = true;
                    
                    if (needsSwitch) {
                        __kimDbg('H1', 'Switching Gemini Model Tier', { current: currentTxt, target: tier });
                        modelBtn.click();
                        await sleep(700);
                        
                        let targetClicked = false;
                        // For Flash, if "fast" or "flash" is missing, clicking the basic "gemini" option is correct
                        const targetTerms = isTargetThinking ? ['thinking'] : (isTargetFlash ? ['flash', 'fast', 'gemini'] : ['advanced', 'pro']);
                        
                        // Search overlays first (dropdown menus usually attach to body end or cdk-overlay)
                        const overlays = document.querySelectorAll('.cdk-overlay-container, [role="menu"], [role="listbox"], [role="dialog"]');
                        const menuRoot = Array.from(overlays).find(o => isVisible(o)) || document.body;
                        
                        const walker2 = document.createTreeWalker(menuRoot, NodeFilter.SHOW_TEXT, null, false);
                        let targetNode = null;
                        while (walker2.nextNode()) {
                            const node = walker2.currentNode;
                            const txt = normalizeText(node.textContent).toLowerCase();
                            if (targetTerms.some(t => txt.includes(t)) && !txt.includes('upgrade') && !txt.includes('learn')) {
                                // For flash targeting "gemini", avoid matching "gemini advanced"
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
                            await sleep(1200); // wait for page to reload/react
                        }

                        if (!targetClicked) {
                            __kimDbg('H1', 'Failed to find target Gemini model in menu', { target: tier });
                            document.body.click(); // Close menu
                            await sleep(200);
                        }
                    } else {
                        __kimDbg('H1', 'Gemini Model already correct', { current: currentTxt, target: tier });
                    }
                } else {
                    __kimDbg('H1', 'Gemini Model selector button not found');
                }
            };
            await selectGeminiPro();
            }
        }

        const uploadedCount = await injectAttachments(cfg, inputEl);

        const injectedLen = await injectPromptText(inputEl, __kimPrompt);
        if (injectedLen < Math.min(8, normalizeText(__kimPrompt).length) || !promptMatchesInput(inputEl, __kimPrompt)) {
            throw new Error('Prompt text was not fully accepted by the chat input. Refusing to send an incomplete prompt.');
        }

    await sleep(80);

        // Wait for the PREVIOUS request's completion hash before pressing
        // Enter.  This is the most reliable guard — the hash is embedded in
        // the response text itself and is provider-agnostic.
        {
            const prevHash = (window.__kimBridge && window.__kimBridge._lastHash) || null;
            if (prevHash) {
                const PRE_SEND_TIMEOUT = 120000;
                const preDeadline = Date.now() + PRE_SEND_TIMEOUT;
                while (Date.now() < preDeadline) {
                    const pageText = extractLatestResponseText(cfg, siteKey) || '';
                    if (pageText.includes(prevHash)) break;
                    await sleep(300);
                }
                // Short settle delay removed for latency
            }
        }

        // Record THIS request's hash so the NEXT request can wait for it
        if (__kimCompletionHash && window.__kimBridge) {
            window.__kimBridge._lastHash = __kimCompletionHash;
        }

        const stateBeforeSend = captureResponseState(cfg, siteKey);

        if (!promptMatchesInput(inputEl, __kimPrompt)) {
            throw new Error('Prompt changed after injection. Refusing to send a partial prompt.');
        }

        // Submit by clicking the provider's enabled Send button. Do not use
        // synthetic Enter events here: long prompts with newlines can be split
        // into partial provider messages if the editor has not fully committed.
        inputEl.focus();
        let sent = false;
        for (let attempt = 0; attempt < 20; attempt++) {
            const sendBtn = findElement(cfg.send_selectors, { visible: true, enabled: true });
            if (sendBtn) {
                try {
                    sendBtn.click();
                    sent = true;
                    break;
                } catch (_) {}
            }
            await sleep(200);
        }
        if (!sent) {
            throw new Error(`Could not find an enabled send button for ${siteKey}. Refusing to submit with Enter because it can split long prompts. selectorDiag=${selectorDiagText}`);
        }
        await sleep(400);
        // #region agent log
        __kimDbg('H1', 'send stage finished', { sent, hasForm: !!(inputEl && inputEl.closest && inputEl.closest('form')), inputTextLen: readInputText(inputEl).length });
        // #endregion

        const responseDeadline = Date.now() + RESPONSE_WAIT_MS;
                let brokeOnResponseSignal = false;
        while (Date.now() < responseDeadline) {
                ensureWithinDeadline('wait_response_start');
                        const currentState = captureResponseState(cfg, siteKey);
                        const hasNewText = hasNewResponseSince(baselineState, currentState);
                if (hasNewText) {
                brokeOnResponseSignal = true;
                break;
            }
            await sleep(450);
    }
        if (!brokeOnResponseSignal) {
            // Some providers update an existing node in-place without changing
            // the overall response count. Continue into fallback scraping.
            __kimDbg('H2', 'response signal timeout; continuing with fallback scrape', {
                baselineCount: baselineState.count,
                latestLen: (extractLatestResponseText(cfg, siteKey) || '').length,
            });
        }
    // #region agent log
        __kimDbg('H2', 'response wait finished', {
            brokeOnResponseSignal,
            latestLen: (extractLatestResponseText(cfg, siteKey) || '').length,
        });
    // #endregion

        let sawStop = isAnyStopVisible(cfg);
        const stopAppearDeadline = Date.now() + STOP_APPEAR_WAIT_MS;
        while (!sawStop && Date.now() < stopAppearDeadline) {
            ensureWithinDeadline('wait_stop_appear');
            const currentState = captureResponseState(cfg, siteKey);
            if ((currentState.latestText || '').includes(__kimCompletionHash || '__KIMBRIDGE_DONE__')) break;
            await sleep(250);
            sawStop = isAnyStopVisible(cfg);
        }

        if (sawStop || (captureResponseState(cfg, siteKey).latestText || '').includes(__kimCompletionHash || '__KIMBRIDGE_DONE__')) {
            const doneDeadline = Date.now() + GENERATION_DONE_WAIT_MS;
            while (Date.now() < doneDeadline) {
                ensureWithinDeadline('wait_generation_done');
                const currentState = captureResponseState(cfg, siteKey);
                if ((currentState.latestText || '').includes(__kimCompletionHash || '__KIMBRIDGE_DONE__')) break;
                if (!isAnyStopVisible(cfg)) break;
                await sleep(700);
            }
        }

    await sleep(400);

        let text = '';
        const readDeadline = Date.now() + READ_WAIT_MS;
        while (Date.now() < readDeadline) {
                        ensureWithinDeadline('read_response_text');
                        const currentState = captureResponseState(cfg, siteKey);
                        const candidate = currentState.latestText;
                                                const changed = candidate && (
                                                        hasNewResponseSince(baselineState, currentState)
                                                        || normalizeText(candidate) !== normalizeText(initialResponseText)
                                                );
                                                if (changed) {
                text = candidate;
                break;
            }
            await sleep(650);
    }

    if (!text) {
            const fallback = extractLatestResponseText(cfg, siteKey);
            if (fallback && normalizeText(fallback) !== normalizeText(initialResponseText)) {
                text = fallback;
            }
        }

        if (!text) {
      // #region agent log
            __kimDbg('H2', 'read failed empty text', { responseSelectors: cfg.response_selectors || [], initialResponseTextLen: (initialResponseText || '').length, finalCandidateLen: (extractLatestResponseText(cfg, siteKey) || '').length, sent });
      // #endregion
            throw new Error(`Could not read model response from page. selectorDiag=${selectorDiagText}`);
    }

    await emitPayload({ ok: true, response: text, site: siteKey, attachments_uploaded: uploadedCount || 0 });
  } catch (err) {
    const message = (err && err.message) ? err.message : String(err);
    // #region agent log
    __kimDbg('H3', 'bridge script catch', { message });
    // #endregion
    await emitPayload({ ok: false, error: message, site: __kimSite || 'unknown' });
  }
    } catch (fatalErr) {
        try {
            document.title = '__KIMBRIDGE_FATAL__:' + String((fatalErr && fatalErr.message) ? fatalErr.message : fatalErr);
        } catch (_) {}
    }
    }, 0);
})();
"#;
    let script = format!("{}{}", header, body);
    Ok(script)
}

/// Condvar-based bridge payload collector.
///
/// Primary path: waits on WEBVIEW_BRIDGE_NOTIFY condvar which is notified
/// instantly when the JS bridge sends an IPC event.  No polling, no title
/// hacks.  Falls back to legacy title-polling if IPC hasn't delivered after
/// a few seconds (for backward compat with old JS scripts).
pub(crate) fn collect_bridge_payload(
    window: &tauri::WebviewWindow,
    req_id: &str,
    timeout: Duration,
) -> Result<BridgeCompleteResponse, String> {
    let started = Instant::now();
    let result_store = WEBVIEW_BRIDGE_RESULTS.get_or_init(|| StdMutex::new(HashMap::new()));
    let (lock, condvar) = WEBVIEW_BRIDGE_NOTIFY.get_or_init(|| {
        (StdMutex::new(()), Condvar::new())
    });

    // For legacy fallback: if IPC isn't delivering, fall back to title polling
    let req_id_json = serde_json::to_string(req_id)
        .map_err(|e| format!("Failed to encode req_id for JS: {}", e))?;
    let mut ipc_wait_loops: u64 = 0;

    loop {
        // Check if result is already in the store (from IPC or HTTP callback).
        match result_store.lock() {
            Ok(mut guard) => {
                if let Some(payload) = guard.get(req_id).cloned() {
                    // Remove result and associated markers
                    guard.remove(req_id);
                    guard.remove(&format!("{}_sent", req_id));
                    agent_debug_log(
                        "H1",
                        "collect: result found via IPC",
                        serde_json::json!({ "reqId": req_id, "loops": ipc_wait_loops }),
                    );
                    // Also clean up progress and hidden-state entries
                    if let Ok(mut pg) = WEBVIEW_BRIDGE_PROGRESS.get_or_init(|| StdMutex::new(HashMap::new())).lock() {
                        pg.remove(req_id);
                    }
                    if let Ok(mut hg) = WEBVIEW_WAS_HIDDEN.get_or_init(|| StdMutex::new(std::collections::HashSet::new())).lock() {
                        hg.remove(req_id);
                    }
                    return Ok(payload);
                }
            }
            Err(_) => return Err("Bridge results lock poisoned.".to_string()),
        }

        // Check for a fatal title
        if let Ok(title) = window.title() {
            if let Some(msg) = title.strip_prefix("__KIMBRIDGE_FATAL__:") {
                return Err(format!("Bridge script fatal error: {}", msg.trim()));
            }
        }

        if started.elapsed() >= timeout {
            // Clean up leaked entries on timeout
            if let Ok(mut guard) = result_store.lock() {
                guard.remove(req_id);
                guard.remove(&format!("{}_sent", req_id));
            }
            if let Ok(mut pg) = WEBVIEW_BRIDGE_PROGRESS.get_or_init(|| StdMutex::new(HashMap::new())).lock() {
                pg.remove(req_id);
            }
            if let Ok(mut hg) = WEBVIEW_WAS_HIDDEN.get_or_init(|| StdMutex::new(std::collections::HashSet::new())).lock() {
                hg.remove(req_id);
            }
            agent_debug_log(
                "H3",
                "collect timeout waiting payload",
                serde_json::json!({
                    "reqId": req_id,
                    "ipcWaitLoops": ipc_wait_loops,
                }),
            );
            return Err("Timed out waiting for in-app browser completion response.".to_string());
        }

        ipc_wait_loops += 1;

        // Wait briefly before the next title-pull attempt.
        // Since IPC doesn't work on external pages, the title-pull on each
        // iteration is our real collection mechanism (~500ms cadence).
        let wait_duration = Duration::from_millis(500);
        if let Ok(guard) = lock.lock() {
            let _ = condvar.wait_timeout(guard, wait_duration);
        }

        // Always try legacy title-pull on every iteration.
        // Tauri IPC (emit) does NOT work on external pages — __TAURI_INTERNALS__
        // is not injected even with remote IPC capabilities. The JS bridge stores
        // results in window.__kimBridgeStore which we poll via title-pull.
        match pull_payload_from_js_store_legacy(window, &req_id_json) {
            Ok(Some(payload)) => {
                agent_debug_log(
                    "H2",
                    "collect: result found via title-pull",
                    serde_json::json!({ "reqId": req_id, "loops": ipc_wait_loops }),
                );
                return Ok(payload);
            }
            Ok(None) => {}
            Err(e) => {
                agent_debug_log(
                    "H3",
                    "collect title-pull failed",
                    serde_json::json!({ "reqId": req_id, "error": e }),
                );
            }
        }
    }
}

/// Legacy title-polling fallback — only used when IPC isn't available.
/// Kept for backward compatibility with pages that don't have IPC access.
pub(crate) fn pull_payload_from_js_store_legacy(
    window: &tauri::WebviewWindow,
    req_id_json: &str,
) -> Result<Option<BridgeCompleteResponse>, String> {
    const NULL_MARKER: &str = "__KIMBRIDGE_NONE__";

    let write_js = format!(
        r#"(() => {{
            try {{
                const store = window.__kimBridgeStore || {{}};
                const entry = store[{req_id_json}];
                const data = (entry && typeof entry.data === 'string' && entry.data.length > 0)
                    ? entry.data : '{null_marker}';
                document.title = data;
            }} catch (_) {{
                document.title = '{null_marker}';
            }}
        }})()"#,
        req_id_json = req_id_json,
        null_marker = NULL_MARKER,
    );
    window.eval(write_js).map_err(|e| e.to_string())?;

    std::thread::sleep(Duration::from_millis(80));

    let title = window.title().map_err(|e| e.to_string())?;

    agent_debug_log(
        "H2",
        "title pull read title",
        serde_json::json!({ "reqId": req_id_json, "title": title }),
    );

    if title == NULL_MARKER || title.trim().is_empty() || title.contains(' ') {
        return Ok(None);
    }

    let decoded = match base64::engine::general_purpose::STANDARD.decode(&title) {
        Ok(b) => b,
        Err(_) => return Ok(None),
    };
    let decoded_str = match String::from_utf8(decoded) {
        Ok(s) => s,
        Err(_) => return Ok(None),
    };
    let payload: BridgeCompleteResponse = match serde_json::from_str(&decoded_str) {
        Ok(p) => p,
        Err(_) => return Ok(None),
    };

    let clear_js = format!(
        "try {{ delete (window.__kimBridgeStore || {{}})[{req_id_json}]; }} catch(_) {{}}",
        req_id_json = req_id_json,
    );
    let _ = window.eval(clear_js);

    Ok(Some(payload))
}

/// Run a completion using the persistent bridge (preferred) or the legacy
/// full-script approach (fallback).
///
/// The persistent bridge is already loaded via initialization_script:
///   window.__kimBridge.send(prompt, reqId, site, attachments)
/// This is a ~100 byte eval call vs the previous ~30KB script injection.
#[allow(clippy::too_many_arguments)]
pub(crate) fn run_bridge_completion_once(
    window: &tauri::WebviewWindow,
    site: &str,
    prompt: &str,
    attachments: &[BridgeAttachment],
    callback_url: &str,
    callback_token: &str,
    completion_hash: Option<&str>,
    model_tier: Option<&str>,
) -> Result<BridgeCompleteResponse, String> {
    let app_config = window.state::<config::AppConfig>();
    let timeout_secs = app_config.bridge_timeout_secs;

    let req_id = format!(
        "r-{}-{}",
        std::process::id(),
        WEBVIEW_BRIDGE_REQ_COUNTER.fetch_add(1, Ordering::Relaxed)
    );
    agent_debug_log(
        "H1",
        "run_bridge_completion_once start",
        serde_json::json!({
            "reqId": req_id,
            "site": site,
            "promptLen": prompt.len(),
            "attachments": attachments.len(),
            "collectorMode": "sentinel_v1",
            "collectorTimeoutS": timeout_secs,
        }),
    );

    // Clear any stale result for this req_id.
    if let Ok(mut guard) = WEBVIEW_BRIDGE_RESULTS
        .get_or_init(|| StdMutex::new(HashMap::new()))
        .lock()
    {
        guard.remove(&req_id);
    }
    if let Ok(mut guard) = WEBVIEW_BRIDGE_PROGRESS
        .get_or_init(|| StdMutex::new(HashMap::new()))
        .lock()
    {
        guard.remove(&req_id);
    }

    // Try the persistent bridge first (tiny eval call).
    let prompt_json = serde_json::to_string(prompt).map_err(|e| e.to_string())?;
    let req_id_json = serde_json::to_string(&req_id).map_err(|e| e.to_string())?;
    let site_json = serde_json::to_string(site).map_err(|e| e.to_string())?;
    let attachments_json = serde_json::to_string(attachments).map_err(|e| e.to_string())?;
    let hash_json = serde_json::to_string(&completion_hash).unwrap_or_else(|_| "null".to_string());

    let tier_json = serde_json::to_string(&model_tier).unwrap_or_else(|_| "null".to_string());

    let bridge_call = format!(
        r#"(() => {{
            if (window.__kimBridge && window.__kimBridge._v >= 2) {{
                window.__kimBridge.send({prompt}, {req_id}, {site}, {attachments}, null, {hash}, {tier});
            }} else {{
                // Persistent bridge not installed — signal to fall back
                document.title = '__KIMBRIDGE_NO_PERSISTENT__';
            }}
        }})()"#,
        prompt = prompt_json,
        req_id = req_id_json,
        site = site_json,
        attachments = attachments_json,
        hash = hash_json,
        tier = tier_json,
    );

    agent_debug_log(
        "H1",
        "bridge eval begin (persistent)",
        serde_json::json!({ "reqId": req_id, "scriptLen": bridge_call.len() }),
    );

    if let Err(e) = window.eval(&bridge_call) {
        agent_debug_log(
            "H3",
            "bridge eval failed",
            serde_json::json!({ "reqId": req_id, "error": e.to_string() }),
        );
        return Err(format!("Failed to evaluate in-app script: {}", e));
    }

    // Check if persistent bridge wasn't available (title marker)
    std::thread::sleep(Duration::from_millis(100));
    let title_check = window.title().unwrap_or_default();
    if title_check == "__KIMBRIDGE_NO_PERSISTENT__" {
        // Restore title and fall back to the legacy full-script approach.
        let _ = window.eval("document.title = '';");
        agent_debug_log(
            "H2",
            "persistent bridge not available, falling back to legacy script",
            serde_json::json!({ "reqId": req_id }),
        );

        // Clear old req_id and generate a new one for the legacy path
        if let Ok(mut guard) = WEBVIEW_BRIDGE_RESULTS
            .get_or_init(|| StdMutex::new(HashMap::new()))
            .lock()
        {
            guard.remove(&req_id);
        }

        let script = build_bridge_complete_script(
            site, prompt, &req_id, attachments, callback_url, callback_token, completion_hash, model_tier,
        ).map_err(|e| format!("Script build failed: {}", e))?;

        if let Err(e) = window.eval(&script) {
            return Err(format!("Failed to evaluate legacy script: {}", e));
        }
    }

    agent_debug_log(
        "H1",
        "bridge collect begin",
        serde_json::json!({
            "reqId": req_id,
            "timeoutS": timeout_secs,
            "mode": "sentinel_v1",
        }),
    );

    let result = collect_bridge_payload(
        window,
        &req_id,
        Duration::from_secs(timeout_secs),
    );

    agent_debug_log(
        "H1",
        "bridge collect returned",
        serde_json::json!({ "reqId": req_id, "ok": result.is_ok() }),
    );
    match &result {
        Ok(payload) => agent_debug_log(
            "H2",
            "bridge completion collected payload",
            serde_json::json!({
                "reqId": req_id,
                "ok": payload.ok,
                "hasResponse": payload.response.as_ref().map(|s| !s.is_empty()).unwrap_or(false),
                "error": payload.error,
            }),
        ),
        Err(e) => agent_debug_log(
            "H3",
            "bridge completion collect failed",
            serde_json::json!({ "reqId": req_id, "error": e }),
        ),
    }
    result
}


// ---------------------------------------------------------------------------
// Tauri commands
// ---------------------------------------------------------------------------


#[tauri::command]
pub(crate) async fn open_browser_signin_window(
    url: String,
    provider_name: Option<String>,
    app_handle: tauri::AppHandle,
) -> Result<String, String> {
    open_browser_signin_window_impl(&url, provider_name, &app_handle)
}


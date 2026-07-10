//! Result-store delivery for the split send/receive bridge flow:
//! `POST /v1/callback` (bridge JS posts the payload back) and
//! `GET /v1/result/{reqId}` (Python long-polls for it).
//!
//! Extracted verbatim from the monolithic `http_bridge.rs` route match —
//! behavior unchanged.

use std::collections::HashMap;
use std::sync::Mutex as StdMutex;
use std::time::Duration;

use tauri::Manager;
use tiny_http::Request;

use crate::browser_bridge::{collect_bridge_payload, mark_bridge_entry_seen, notify_bridge_result};
use crate::http_util::{agent_debug_log, respond_json};
use crate::{
    hide_browser_window_offscreen, BridgeCallbackRequest, WEBVIEW_BRIDGE_RESULTS,
    WEBVIEW_WAS_HIDDEN,
};

use super::read_body_capped;

/// `POST /v1/callback` — store a bridge payload and wake any waiting collector.
pub(super) fn callback(mut request: Request) {
    let body = match read_body_capped(&mut request) {
        Ok(b) => b,
        Err(e) => {
            respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
            return;
        }
    };

    let parsed: BridgeCallbackRequest = match serde_json::from_str(&body) {
        Ok(v) => v,
        Err(e) => {
            respond_json(
                request,
                400,
                serde_json::json!({"ok": false, "error": format!("Invalid JSON: {}", e)}),
            );
            return;
        }
    };

    agent_debug_log(
        "H2",
        "callback received",
        serde_json::json!({
            "reqId": parsed.req_id,
            "ok": parsed.payload.ok,
            "hasResponse": parsed.payload.response.as_ref().map(|s| !s.is_empty()).unwrap_or(false),
            "error": parsed.payload.error,
        }),
    );

    // AUDIT FIX #3: timestamp this req_id so the periodic GC sweep
    // (browser_bridge::sweep_stale_bridge_entries) can evict it if the
    // caller dies before ever polling /v1/result.
    mark_bridge_entry_seen(&parsed.req_id);

    let store = WEBVIEW_BRIDGE_RESULTS.get_or_init(|| StdMutex::new(HashMap::new()));
    match store.lock() {
        Ok(mut guard) => {
            guard.insert(parsed.req_id.clone(), parsed.payload);
        }
        Err(_) => {
            respond_json(
                request,
                500,
                serde_json::json!({"ok": false, "error": "Bridge results lock poisoned."}),
            );
            return;
        }
    }
    // Wake up any Condvar-waiting collector.
    notify_bridge_result();

    respond_json(request, 200, serde_json::json!({"ok": true}));
}

pub(super) fn handle_bridge_result_request(
    request: Request,
    path: &str,
    _token: &str,
    app_handle: tauri::AppHandle,
) {
    let req_id = path.trim_start_matches("/v1/result/").to_string();
    if req_id.is_empty() {
        respond_json(
            request,
            400,
            serde_json::json!({"ok": false, "error": "Missing req_id"}),
        );
        return;
    }

    let window = if let Some(w) = app_handle.get_webview_window("kim-browser-signin") {
        w
    } else {
        respond_json(
            request,
            500,
            serde_json::json!({"ok": false, "error": "Browser window closed."}),
        );
        return;
    };

    agent_debug_log(
        "H1",
        "result collector start (persistent bridge)",
        serde_json::json!({
            "reqId": req_id,
        }),
    );

    let app_config = app_handle.state::<crate::config::AppConfig>();
    let timeout_secs = app_config.bridge_timeout_secs;

    let result = collect_bridge_payload(&window, &req_id, Duration::from_secs(timeout_secs));

    let mut should_hide = false;
    if let Ok(mut guard) = WEBVIEW_WAS_HIDDEN
        .get_or_init(|| StdMutex::new(std::collections::HashSet::new()))
        .lock()
    {
        should_hide = guard.remove(&req_id);
    }
    if should_hide {
        hide_browser_window_offscreen(&window);
    }

    match result {
        Ok(payload) => {
            if payload.ok {
                // Forward the bridge's verified upload count (bridge.js done
                // payload → BridgeIpcEvent → BridgeCompleteResponse) so the
                // Python side's screenshot-honesty gating actually receives
                // it. Omitted (not null) when the bridge did not report one,
                // matching the "older bridge" behavior bridge_client.py
                // expects.
                let mut body = serde_json::json!({
                    "ok": true,
                    "response": payload.response,
                    "site": payload.site.unwrap_or_else(|| "unknown".to_string()),
                    "req_id": req_id,
                });
                if let Some(uploaded) = payload.attachments_uploaded {
                    body["attachments_uploaded"] = serde_json::json!(uploaded);
                }
                respond_json(request, 200, body);
            } else {
                respond_json(
                    request,
                    500,
                    serde_json::json!({
                        "ok": false,
                        "error": payload.error.unwrap_or_else(|| "Unknown bridge error".to_string()),
                        "req_id": req_id,
                    }),
                );
            }
        }
        Err(e) => {
            respond_json(
                request,
                504,
                serde_json::json!({
                    "ok": false,
                    "error": e,
                    "req_id": req_id,
                }),
            );
        }
    }
}

//! Per-session browser thread metadata routes:
//! `GET/POST /v1/browser/meta`, `POST /v1/browser/commit-url` and
//! `POST /v1/browser/restore`.
//!
//! Extracted verbatim from the monolithic `http_bridge.rs` route match —
//! behavior unchanged.

use serde::Deserialize;
use tauri::Manager;
use tiny_http::Request;

use crate::browser_bridge::open_browser_signin_window_with_visibility;
use crate::http_util::{query_param, respond_json};
use crate::provider_url::{
    apply_browser_meta_writes, browser_url_allowed_for_restore, browser_url_is_bad_for_commit,
    browser_url_site, fresh_site_url, normalize_site,
};
use crate::session_store::{
    now_ms, read_browser_session_meta_from_dir, resolve_session_date_dir, session_base_dir,
    update_browser_session_meta, validate_session_id,
};
use crate::{
    is_bridge_task_running, webview_current_href, BrowserRestoreResult, BrowserSessionMeta,
};

use super::{capitalize, read_body_capped};

/// `GET /v1/browser/meta` — read the stored browser-session metadata.
pub(super) fn get_meta(request: Request) {
    let raw_url = request.url().to_string();
    let Some(session_id) = query_param(&raw_url, "session_id") else {
        respond_json(
            request,
            400,
            serde_json::json!({"ok": false, "error": "session_id is required."}),
        );
        return;
    };
    if let Err(e) = validate_session_id(&session_id) {
        respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
        return;
    }
    let session_type = query_param(&raw_url, "session_type").unwrap_or_else(|| "kim".to_string());
    let session_date = query_param(&raw_url, "session_date");
    let kim_dir = query_param(&raw_url, "kim_dir");
    let codex_dir = query_param(&raw_url, "codex_dir");
    let base = session_base_dir(&session_type, kim_dir, codex_dir);
    match resolve_session_date_dir(&base, &session_id, session_date.as_deref()) {
        Ok(date_dir) => {
            let meta =
                read_browser_session_meta_from_dir(&date_dir, &session_id).unwrap_or_default();
            respond_json(request, 200, serde_json::json!({"ok": true, "meta": meta}));
        }
        Err(e) => respond_json(request, 400, serde_json::json!({"ok": false, "error": e})),
    }
}

/// `POST /v1/browser/meta` — merge caller-supplied fields into the metadata.
pub(super) fn write_meta(mut request: Request) {
    let body = match read_body_capped(&mut request) {
        Ok(b) => b,
        Err(e) => {
            respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
            return;
        }
    };

    #[derive(Deserialize)]
    struct BrowserMetaWriteRequest {
        session_id: String,
        #[serde(default)]
        session_date: Option<String>,
        #[serde(default)]
        session_type: Option<String>,
        #[serde(default)]
        site: Option<String>,
        #[serde(default)]
        url: Option<String>,
        #[serde(default)]
        browser_last_site: Option<String>,
        #[serde(default)]
        last_llm_provider: Option<String>,
        #[serde(default)]
        kim_dir: Option<String>,
        #[serde(default)]
        codex_dir: Option<String>,
    }

    let parsed: BrowserMetaWriteRequest = match serde_json::from_str(&body) {
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
    if let Err(e) = validate_session_id(&parsed.session_id) {
        respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
        return;
    }

    let stype = parsed.session_type.unwrap_or_else(|| "kim".to_string());
    let base = session_base_dir(&stype, parsed.kim_dir, parsed.codex_dir);
    let date_dir =
        match resolve_session_date_dir(&base, &parsed.session_id, parsed.session_date.as_deref()) {
            Ok(v) => v,
            Err(e) => {
                respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
                return;
            }
        };
    // Input validation first (errors depend only on the inputs, not on
    // the stored meta) so bad requests still get a 400.
    if let Err(e) = apply_browser_meta_writes(
        &mut BrowserSessionMeta::default(),
        parsed.browser_last_site.clone(),
        parsed.site.clone(),
        parsed.url.clone(),
        parsed.last_llm_provider.clone(),
    ) {
        respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
        return;
    }
    // M-STORE-1: read-modify-write under the process-wide meta lock so
    // a concurrent UI/bridge commit can't discard this write's fields.
    match update_browser_session_meta(&date_dir, &parsed.session_id, |meta| {
        apply_browser_meta_writes(
            meta,
            parsed.browser_last_site.clone(),
            parsed.site.clone(),
            parsed.url.clone(),
            parsed.last_llm_provider.clone(),
        )
    }) {
        Ok(meta) => respond_json(request, 200, serde_json::json!({"ok": true, "meta": meta})),
        Err(e) => respond_json(request, 500, serde_json::json!({"ok": false, "error": e})),
    }
}

/// `POST /v1/browser/commit-url` — persist the webview's current thread URL.
pub(super) fn commit_url(mut request: Request, app_handle: tauri::AppHandle) {
    let body = match read_body_capped(&mut request) {
        Ok(b) => b,
        Err(e) => {
            respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
            return;
        }
    };

    #[derive(Deserialize)]
    struct BrowserCommitRequest {
        session_id: String,
        #[serde(default)]
        session_date: Option<String>,
        #[serde(default)]
        session_type: Option<String>,
        #[serde(default)]
        preferred_site: Option<String>,
        #[serde(default)]
        kim_dir: Option<String>,
        #[serde(default)]
        codex_dir: Option<String>,
    }

    let parsed: BrowserCommitRequest = match serde_json::from_str(&body) {
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
    if let Err(e) = validate_session_id(&parsed.session_id) {
        respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
        return;
    }

    let Some(win) = app_handle.get_webview_window("kim-browser-signin") else {
        respond_json(
            request,
            200,
            serde_json::json!({"ok": true, "committed": false, "reason": "no_browser_window"}),
        );
        return;
    };
    let current_url = webview_current_href(&win);
    let site = parsed
        .preferred_site
        .as_deref()
        .map(normalize_site)
        .filter(|s| !s.is_empty())
        .or_else(|| browser_url_site(&current_url))
        .unwrap_or_else(|| "claude".to_string());

    let stype = parsed.session_type.unwrap_or_else(|| "kim".to_string());
    let base = session_base_dir(&stype, parsed.kim_dir, parsed.codex_dir);
    let date_dir =
        match resolve_session_date_dir(&base, &parsed.session_id, parsed.session_date.as_deref()) {
            Ok(v) => v,
            Err(e) => {
                respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
                return;
            }
        };
    // M-STORE-1: both branches read-modify-write under the process-wide
    // meta lock so a concurrent UI session-switch commit can't discard
    // the thread URL this bridge commit is saving (the known
    // "reused-thread vanishes" symptom).
    if browser_url_is_bad_for_commit(&current_url, &site) {
        // Preserve any useful previous URL; only update the last-site hint.
        let meta = update_browser_session_meta(&date_dir, &parsed.session_id, |meta| {
            meta.browser_last_site = Some(site.clone());
            meta.browser_threads_updated_at_ms = Some(now_ms());
            Ok(())
        })
        .unwrap_or_default();
        respond_json(
            request,
            200,
            serde_json::json!({"ok": true, "committed": false, "reason": "ignored_bad_url", "meta": meta}),
        );
        return;
    }

    match update_browser_session_meta(&date_dir, &parsed.session_id, |meta| {
        meta.browser_threads
            .insert(site.clone(), current_url.clone());
        meta.browser_last_site = Some(site.clone());
        meta.browser_threads_updated_at_ms = Some(now_ms());
        Ok(())
    }) {
        Ok(meta) => respond_json(
            request,
            200,
            serde_json::json!({"ok": true, "committed": true, "meta": meta}),
        ),
        Err(e) => respond_json(request, 500, serde_json::json!({"ok": false, "error": e})),
    }
}

/// `POST /v1/browser/restore` — reopen the saved thread for a session.
pub(super) fn restore(mut request: Request, app_handle: tauri::AppHandle) {
    let body = match read_body_capped(&mut request) {
        Ok(b) => b,
        Err(e) => {
            respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
            return;
        }
    };

    #[derive(Deserialize)]
    struct BrowserRestoreRequest {
        session_id: String,
        #[serde(default)]
        session_date: Option<String>,
        #[serde(default)]
        session_type: Option<String>,
        #[serde(default)]
        preferred_site: Option<String>,
        #[serde(default)]
        kim_dir: Option<String>,
        #[serde(default)]
        codex_dir: Option<String>,
    }

    let parsed: BrowserRestoreRequest = match serde_json::from_str(&body) {
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
    if let Err(e) = validate_session_id(&parsed.session_id) {
        respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
        return;
    }
    if is_bridge_task_running() {
        respond_json(
            request,
            409,
            serde_json::json!({
                "ok": false,
                "error": "Cannot restore provider browser while Kim is running a task.",
            }),
        );
        return;
    }

    let stype = parsed.session_type.unwrap_or_else(|| "kim".to_string());
    let base = session_base_dir(&stype, parsed.kim_dir, parsed.codex_dir);
    let date_dir =
        match resolve_session_date_dir(&base, &parsed.session_id, parsed.session_date.as_deref()) {
            Ok(v) => v,
            Err(e) => {
                respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
                return;
            }
        };
    let meta =
        read_browser_session_meta_from_dir(&date_dir, &parsed.session_id).unwrap_or_default();
    let site = parsed
        .preferred_site
        .as_deref()
        .map(normalize_site)
        .filter(|s| !s.is_empty())
        .or(meta.browser_last_site.clone())
        .unwrap_or_else(|| "claude".to_string());

    let mut restored = false;
    let reason;
    let target = if let Some(saved) = meta.browser_threads.get(&site) {
        if browser_url_allowed_for_restore(saved, &site) {
            restored = true;
            reason = "stored_thread".to_string();
            saved.clone()
        } else {
            reason = "stored_url_rejected".to_string();
            fresh_site_url(&site, None)
        }
    } else {
        reason = "no_stored_url".to_string();
        fresh_site_url(&site, None)
    };

    let provider_name = Some(format!("{} (session)", capitalize(&site)));
    // Match the Tauri-command path: chat-select restores happen invisibly.
    match open_browser_signin_window_with_visibility(&target, provider_name, false, &app_handle) {
        Ok(_) => {
            let message = if restored {
                "Restored the saved browser conversation for this session."
            } else if reason == "stored_url_rejected" {
                "Saved browser URL was not safe/valid; opened a fresh provider page."
            } else {
                "No saved browser conversation for this provider; opened the provider start page."
            };
            respond_json(
                request,
                200,
                serde_json::json!({
                    "ok": true,
                    "result": BrowserRestoreResult {
                        restored,
                        site,
                        url: target,
                        reason,
                        message: Some(message.to_string()),
                    },
                }),
            );
        }
        Err(e) => respond_json(
            request,
            500,
            serde_json::json!({"ok": false, "error": format!("{}", e)}),
        ),
    }
}

// http_util.rs — loopback control-plane request/response helpers.
// Extracted from lib.rs (file-split restructure) — behavior unchanged.

use crate::*;
use tiny_http::{Header, Request, Response, StatusCode};

pub(crate) fn query_param(raw_url: &str, wanted: &str) -> Option<String> {
    let url = tauri::Url::parse(&format!("http://localhost{}", raw_url)).ok()?;
    for (key, value) in url.query_pairs() {
        if key == wanted {
            let owned = value.into_owned();
            if !owned.trim().is_empty() {
                return Some(owned);
            }
        }
    }
    None
}

pub(crate) fn header_value(request: &Request, name: &str) -> Option<String> {
    request
        .headers()
        .iter()
        .find(|h| h.field.to_string().eq_ignore_ascii_case(name))
        .map(|h| h.value.as_str().to_string())
}

pub(crate) fn json_response(
    status: u16,
    body: serde_json::Value,
) -> Response<std::io::Cursor<Vec<u8>>> {
    let mut resp = Response::from_string(body.to_string()).with_status_code(StatusCode(status));
    if let Ok(h) = Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..]) {
        resp.add_header(h);
    }
    // Restrict CORS to the Tauri app origin only — wildcard would allow any
    // visited website to CORS-fetch this privileged loopback control plane (#7).
    if let Ok(h) = Header::from_bytes(
        &b"Access-Control-Allow-Origin"[..],
        &b"tauri://localhost"[..],
    ) {
        resp.add_header(h);
    }
    if let Ok(h) = Header::from_bytes(
        &b"Access-Control-Allow-Headers"[..],
        &b"Content-Type, X-Kim-Token"[..],
    ) {
        resp.add_header(h);
    }
    if let Ok(h) = Header::from_bytes(
        &b"Access-Control-Allow-Methods"[..],
        &b"GET, POST, OPTIONS"[..],
    ) {
        resp.add_header(h);
    }
    resp
}

pub(crate) fn respond_json(request: Request, status: u16, body: serde_json::Value) {
    let _ = request.respond(json_response(status, body));
}

pub(crate) fn agent_debug_log(hypothesis_id: &str, message: &str, data: serde_json::Value) {
    // Only write when KIM_BRIDGE_DEBUG=1 is set — the log is plaintext and can
    // contain prompt content/payloads (#6).  Off by default.
    if std::env::var("KIM_BRIDGE_DEBUG").as_deref() != Ok("1") {
        return;
    }
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    // Redact the hardcoded session ID — use a per-process identifier instead.
    let session_id = format!("proc_{}", std::process::id());
    let line = serde_json::json!({
        "sessionId": session_id,
        "hypothesisId": hypothesis_id,
        "location": "desktop/src-tauri/src/lib.rs",
        "message": message,
        "data": data,
        "timestamp": ts,
    });
    let log_path = default_sessions_dir().join("bridge_debug.log");
    let _ = std::fs::create_dir_all(default_sessions_dir());
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_path)
    {
        use std::io::Write;
        let _ = writeln!(f, "{}", line);
    }
}

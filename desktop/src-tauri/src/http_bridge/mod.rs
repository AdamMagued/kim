//! Local loopback HTTP bridge (`127.0.0.1:18991-19010`) used by the `kim`
//! CLI, the Python orchestrator, and the in-app provider webview (bridge.js).
//!
//! This module owns the server lifecycle, request authentication, and the
//! route dispatch; the handlers live in cohesive submodules:
//! * [`provider_send`] — `/v1/open`, `/v1/complete`, `/v1/send`
//! * [`results`] — `/v1/callback`, `/v1/result/{reqId}`
//! * [`browser_window`] — `/v1/hide`, `/v1/show`, `/v1/browser/*` window ops,
//!   `/v1/provider`
//! * [`session_meta`] — `/v1/browser/meta`, `/v1/browser/commit-url`,
//!   `/v1/browser/restore`
//! * [`tasks`] — `/v1/status`, `/v1/task`, `/v1/cancel`, `/v1/task/approve`

mod browser_window;
mod provider_send;
mod results;
mod session_meta;
mod tasks;

// F-H-9 (issue #56-H): route × auth regression matrix. Test-only; see
// auth_matrix_tests.rs for the full route table and exhaustiveness guard.
#[cfg(test)]
mod auth_matrix_tests;

use std::sync::atomic::Ordering;

use tauri::Manager;
use tiny_http::{Method, Request, Server};

use crate::http_util::{header_value, respond_json};
use crate::paths::default_project_root;
use crate::{WebviewBridgeConfig, WEBVIEW_BRIDGE_CFG};

/// M-BRIDGE-3: upper bound for any request body. Generous enough for several
/// base64 PNG screenshots in a /v1/send, small enough that a token-holding
/// local process can no longer OOM the app with a multi-GB POST.
const MAX_BODY_BYTES: usize = 32 * 1024 * 1024;

/// Read at most `cap` bytes of UTF-8 from `reader`; error if the stream holds
/// more. Split out from `read_body_capped` so it is unit-testable.
fn read_capped_string<R: std::io::Read>(reader: R, cap: usize) -> Result<String, String> {
    use std::io::Read as _;
    let mut body = String::new();
    reader
        .take(cap as u64 + 1)
        .read_to_string(&mut body)
        .map_err(|e| format!("Invalid body: {}", e))?;
    if body.len() > cap {
        return Err(format!("Request body too large (max {} bytes).", cap));
    }
    Ok(body)
}

/// Read a request body with a hard size cap (M-BRIDGE-3). Rejects on the
/// declared Content-Length when available, and always caps the actual read.
fn read_body_capped(request: &mut Request) -> Result<String, String> {
    if let Some(len) = request.body_length() {
        if len > MAX_BODY_BYTES {
            return Err(format!(
                "Request body too large ({} bytes; max {}).",
                len, MAX_BODY_BYTES
            ));
        }
    }
    read_capped_string(request.as_reader(), MAX_BODY_BYTES)
}

/// Constant-time byte-equality (no early return on the first mismatch) so the
/// token compare cannot leak length/position via timing (#19).
fn tokens_match(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len()
        && a.iter()
            .zip(b.iter())
            .fold(0u8, |acc, (x, y)| acc | (x ^ y))
            == 0
}

/// Decide whether a presented `X-Kim-Token` authorizes `(method, path)`.
///
/// F-D-4: the full bridge token authorizes every route; the webview-scoped
/// token — the ONLY token injected into third-party provider pages — authorizes
/// solely `POST /v1/callback` (the one route the injected auth-probe JS calls),
/// so a page compromise that steals it cannot reach `/v1/task` (spawn an agent
/// run) or `/v1/open` (SSRF). `GET /v1/health` is unauthenticated loopback
/// liveness by design (F-D-3).
fn bridge_request_authorized(
    method: &Method,
    path: &str,
    presented: &str,
    full_token: &str,
    webview_token: &str,
) -> bool {
    if *method == Method::Get && path == "/v1/health" {
        return true;
    }
    if !full_token.is_empty() && tokens_match(presented.as_bytes(), full_token.as_bytes()) {
        return true;
    }
    if *method == Method::Post
        && path == "/v1/callback"
        && !webview_token.is_empty()
        && tokens_match(presented.as_bytes(), webview_token.as_bytes())
    {
        return true;
    }
    false
}

fn handle_webview_bridge_request(request: Request, app_handle: tauri::AppHandle, token: String) {
    let method = request.method().clone();
    let path = request.url().split('?').next().unwrap_or("/").to_string();

    if method == Method::Options {
        respond_json(request, 204, serde_json::json!({"ok": true}));
        return;
    }

    // Auth gate. The full `token` authorizes every route; the F-D-4
    // webview-scoped token (injected into provider pages) authorizes only POST
    // /v1/callback; GET /v1/health is unauthenticated loopback liveness by
    // design (F-D-3). Constant-time compares prevent timing side-channels (#19).
    let auth = header_value(&request, "X-Kim-Token").unwrap_or_default();
    let webview_token = WEBVIEW_BRIDGE_CFG
        .get()
        .map(|c| c.webview_token.clone())
        .unwrap_or_default();
    if !bridge_request_authorized(&method, &path, &auth, &token, &webview_token) {
        respond_json(
            request,
            401,
            serde_json::json!({"ok": false, "error": "Unauthorized bridge token."}),
        );
        return;
    }

    // Handle /v1/result/{reqId} before the match, since it has a dynamic path
    // and we can't use it in a match arm without consuming `method`.
    if method == Method::Get && path.starts_with("/v1/result/") {
        results::handle_bridge_result_request(request, &path, &token, app_handle.clone());
        return;
    }

    match (method, path.as_str()) {
        (Method::Get, "/v1/health") => {
            respond_json(request, 200, serde_json::json!({"ok": true}));
        }
        (Method::Post, "/v1/hide") => browser_window::hide_main(request, app_handle),
        (Method::Post, "/v1/show") => browser_window::show_main(request, app_handle),
        (Method::Post, "/v1/open") => provider_send::open(request, app_handle),
        (Method::Post, "/v1/callback") => results::callback(request),
        // M-BRIDGE-5: the /v1/ping GET route was removed — it was doubly dead:
        // both Rust send sites pass a null callbackUrl (so bridge.js never
        // pings), and an <img> beacon cannot carry X-Kim-Token so any real
        // ping would have 401'd before reaching the handler. Result delivery
        // is the IPC/callback store plus the title-pull in collect_bridge_payload.
        (Method::Post, "/v1/complete") => provider_send::complete(request, app_handle, token),
        (Method::Post, "/v1/send") => provider_send::send(request, app_handle),
        // ---------------------------------------------------------------
        // kimctl routes
        // ---------------------------------------------------------------
        (Method::Get, "/v1/status") => tasks::status(request, app_handle),
        (Method::Get, "/v1/browser/current-url") => {
            browser_window::current_url(request, app_handle)
        }
        (Method::Get, "/v1/browser/meta") => session_meta::get_meta(request),
        (Method::Post, "/v1/browser/meta") => session_meta::write_meta(request),
        (Method::Post, "/v1/browser/commit-url") => session_meta::commit_url(request, app_handle),
        (Method::Post, "/v1/browser/restore") => session_meta::restore(request, app_handle),
        (Method::Post, "/v1/task") => tasks::start_task(request, app_handle),
        (Method::Post, "/v1/cancel") => tasks::cancel(request, app_handle),
        (Method::Post, "/v1/task/approve") => tasks::approve(request),
        (Method::Post, "/v1/browser/show") => browser_window::browser_show(request, app_handle),
        (Method::Post, "/v1/browser/hide") => browser_window::browser_hide(request, app_handle),
        (Method::Post, "/v1/browser/click") => browser_window::browser_click(request, app_handle),
        (Method::Post, "/v1/browser/new-chat") => {
            browser_window::browser_new_chat(request, app_handle)
        }
        (Method::Post, "/v1/provider") => browser_window::set_provider(request, app_handle),
        _ => {
            respond_json(
                request,
                404,
                serde_json::json!({"ok": false, "error": format!("Unknown bridge route: {}", path)}),
            );
        }
    }
}

pub(crate) fn capitalize(s: &str) -> String {
    let mut chars = s.chars();
    match chars.next() {
        None => String::new(),
        Some(first) => first.to_uppercase().collect::<String>() + chars.as_str(),
    }
}

/// D2: persist the active bridge token to `~/.kim/bridge_token` (0600 on unix)
/// so the `kim` CLI can pair without any manual configuration.
fn write_bridge_token_file(token: &str) {
    let Some(home) = dirs::home_dir() else {
        return;
    };
    let dir = home.join(".kim");
    if std::fs::create_dir_all(&dir).is_err() {
        return;
    }
    let path = dir.join("bridge_token");
    if std::fs::write(&path, token).is_ok() {
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
        }
    }
}

pub(crate) fn start_webview_bridge_server(app_handle: tauri::AppHandle) -> Result<(), String> {
    if WEBVIEW_BRIDGE_CFG.get().is_some() {
        return Ok(());
    }

    let mut selected: Option<(Server, u16)> = None;
    for port in 18991u16..19011u16 {
        if let Ok(server) = Server::http(("127.0.0.1", port)) {
            selected = Some((server, port));
            break;
        }
    }

    let (server, port) = selected
        .ok_or_else(|| "Could not bind local in-app bridge port (18991-19010).".to_string())?;

    let mut token = std::env::var("KIM_API_KEY").unwrap_or_default();
    if token.is_empty() {
        let env_path = default_project_root().join(".env");
        if let Ok(content) = std::fs::read_to_string(env_path) {
            for line in content.lines() {
                if line.starts_with("KIM_API_KEY=") || line.starts_with("RELAY_API_KEY=") {
                    token = line
                        .split_once('=')
                        .map(|x| x.1)
                        .unwrap_or("")
                        .trim()
                        .to_string();
                    if (token.starts_with('"') && token.ends_with('"') && token.len() >= 2)
                        || (token.starts_with('\'') && token.ends_with('\'') && token.len() >= 2)
                    {
                        token = token[1..token.len() - 1].to_string();
                    }
                    if !token.is_empty() {
                        break;
                    }
                }
            }
        }
    }

    if token.is_empty() {
        // Generate a cryptographically random fallback token (#18).
        // The previous format "kim-{pid}-{counter}" was predictable and low-entropy.
        use rand::Rng;
        let random_bytes: [u8; 32] = rand::thread_rng().gen();
        token = random_bytes.iter().map(|b| format!("{:02x}", b)).collect();
        eprintln!("[Kim] WARNING: KIM_API_KEY not found in env or .env. Falling back to random bridge token.");
    }

    let base_url = format!("http://127.0.0.1:{}", port);

    // F-D-4: mint a distinct, always-random webview token. This is the only
    // token injected into third-party provider pages (the auth-probe JS); it
    // authorizes ONLY POST /v1/callback, never /v1/task or /v1/open. Kept
    // separate from `token` (the full-capability CLI/orchestrator credential)
    // and never written to disk.
    let webview_token: String = {
        use rand::Rng;
        let random_bytes: [u8; 32] = rand::thread_rng().gen();
        random_bytes.iter().map(|b| format!("{:02x}", b)).collect()
    };

    let _ = WEBVIEW_BRIDGE_CFG.set(WebviewBridgeConfig {
        base_url: base_url.clone(),
        token: token.clone(),
        webview_token,
    });

    // Write only base_url to kim_sessions/.bridge_url (no cleartext tokens here)
    let sessions_dir = default_project_root().join("kim_sessions");
    let _ = std::fs::create_dir_all(&sessions_dir);
    let _ = std::fs::write(sessions_dir.join(".bridge_url"), &base_url);
    // Best-effort remove legacy cleartext token
    let _ = std::fs::remove_file(sessions_dir.join(".bridge_token"));

    // D2: pair with the CLI by default. Whatever token we settled on (env, .env,
    // or the random fallback) is written to ~/.kim/bridge_token with 0600 perms,
    // overwritten on every bridge start. This is a *local-loopback* credential —
    // it only authorizes requests to the 127.0.0.1 bridge on this machine.
    write_bridge_token_file(&token);

    let app_config = app_handle.state::<crate::config::AppConfig>();
    let timeout_secs = app_config.bridge_timeout_secs;

    std::thread::spawn(move || {
        eprintln!(
            "[Kim] In-app browser bridge listening at {} (mode=sentinel_v1, timeout={}s)",
            base_url, timeout_secs,
        );
        // L-BRIDGE-10: cap concurrent request threads. Each GET /v1/result can
        // block for up to bridge_timeout_secs in 500ms webview-eval loops, so
        // unbounded thread-per-request allowed hundreds of webview-hammering
        // threads. Excess requests get a fast 429 instead of a thread.
        use std::sync::atomic::AtomicUsize;
        use std::sync::Arc as StdArc;
        const MAX_IN_FLIGHT: usize = 32;
        let in_flight = StdArc::new(AtomicUsize::new(0));
        /// Decrement-on-drop so a panicking handler still releases its slot.
        struct SlotGuard(StdArc<AtomicUsize>);
        impl Drop for SlotGuard {
            fn drop(&mut self) {
                self.0.fetch_sub(1, Ordering::AcqRel);
            }
        }
        for request in server.incoming_requests() {
            if in_flight.fetch_add(1, Ordering::AcqRel) >= MAX_IN_FLIGHT {
                in_flight.fetch_sub(1, Ordering::AcqRel);
                respond_json(
                    request,
                    429,
                    serde_json::json!({
                        "ok": false,
                        "error": "Bridge is handling too many concurrent requests. Retry shortly.",
                    }),
                );
                continue;
            }
            let app = app_handle.clone();
            let tok = token.clone();
            let guard = SlotGuard(StdArc::clone(&in_flight));
            std::thread::spawn(move || {
                let _guard = guard;
                handle_webview_bridge_request(request, app, tok);
            });
        }
    });

    Ok(())
}

#[cfg(test)]
mod tests {
    // ── M-BRIDGE-3: request-body cap ────────────────────────────────────────

    #[test]
    fn read_capped_string_accepts_small_bodies() {
        let body = super::read_capped_string(std::io::Cursor::new(b"{\"ok\":true}"), 1024);
        assert_eq!(body.unwrap(), "{\"ok\":true}");
    }

    #[test]
    fn read_capped_string_rejects_oversized_bodies() {
        let big = vec![b'a'; 2049];
        let err = super::read_capped_string(std::io::Cursor::new(big), 2048);
        assert!(err.is_err(), "body over the cap must be rejected");
        assert!(err.unwrap_err().contains("too large"));
    }

    #[test]
    fn read_capped_string_accepts_exact_cap() {
        let exact = vec![b'a'; 2048];
        let body = super::read_capped_string(std::io::Cursor::new(exact), 2048).unwrap();
        assert_eq!(body.len(), 2048);
    }

    // ── F-D-4: capability-scoped webview token ──────────────────────────────
    use super::bridge_request_authorized;
    use tiny_http::Method;

    const FULL: &str = "full-secret-token";
    const WV: &str = "webview-scoped-token";

    #[test]
    fn full_token_authorizes_every_route() {
        for (m, p) in [
            (Method::Post, "/v1/task"),
            (Method::Post, "/v1/open"),
            (Method::Post, "/v1/callback"),
            (Method::Get, "/v1/status"),
        ] {
            assert!(
                bridge_request_authorized(&m, p, FULL, FULL, WV),
                "full token should authorize {p}"
            );
        }
    }

    #[test]
    fn webview_token_authorizes_only_callback() {
        // The scoped token reaches /v1/callback...
        assert!(bridge_request_authorized(
            &Method::Post,
            "/v1/callback",
            WV,
            FULL,
            WV
        ));
        // ...but NOTHING else — a stolen page token cannot spawn a run or SSRF.
        for (m, p) in [
            (Method::Post, "/v1/task"),
            (Method::Post, "/v1/open"),
            (Method::Get, "/v1/status"),
            (Method::Get, "/v1/callback"), // wrong method
        ] {
            assert!(
                !bridge_request_authorized(&m, p, WV, FULL, WV),
                "webview token must NOT authorize {m:?} {p}"
            );
        }
    }

    #[test]
    fn health_is_unauthenticated_and_bad_tokens_rejected() {
        assert!(bridge_request_authorized(
            &Method::Get,
            "/v1/health",
            "",
            FULL,
            WV
        ));
        // A wrong/empty token authorizes nothing but health.
        assert!(!bridge_request_authorized(
            &Method::Post,
            "/v1/task",
            "wrong",
            FULL,
            WV
        ));
        assert!(!bridge_request_authorized(
            &Method::Post,
            "/v1/callback",
            "",
            FULL,
            WV
        ));
    }
}

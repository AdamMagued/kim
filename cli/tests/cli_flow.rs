//! End-to-end CLI flow tests (T2): drive the real `kim` binary in one-shot
//! `kim chat` mode against a fake in-process HTTP server that impersonates an
//! OpenAI-compatible provider (ollama) and the Kim desktop bridge, then assert
//! both the request bodies the CLI sent and the output/exit code it produced.
//!
//! Isolation: each test runs the binary with HOME pointed at a fresh temp dir
//! (config, sessions, and bridge-token lookups all resolve under HOME), a temp
//! cwd (so no Kim repo root or KIM.md is discovered and the plain
//! `stream_kim_request` path is taken), and proxy/KIM env vars cleared.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::process::{Command, Output, Stdio};
use std::sync::{Arc, Mutex};

/// One HTTP request captured by the fake server.
#[derive(Debug, Clone)]
struct RecordedRequest {
    method: String,
    path: String,
    headers: HashMap<String, String>,
    body: String,
}

impl RecordedRequest {
    fn json_body(&self) -> serde_json::Value {
        serde_json::from_str(&self.body)
            .unwrap_or_else(|e| panic!("request body is not valid JSON ({e}): {:?}", self.body))
    }
}

/// Minimal std-only HTTP/1.1 server. Routes each request through `respond`
/// (returning `(status_line_suffix, content_type, body)`) and records it.
struct FakeServer {
    base_url: String,
    requests: Arc<Mutex<Vec<RecordedRequest>>>,
}

impl FakeServer {
    fn start<F>(respond: F) -> Self
    where
        F: Fn(&RecordedRequest) -> (u16, &'static str, String) + Send + 'static,
    {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind fake server");
        let port = listener.local_addr().unwrap().port();
        let requests: Arc<Mutex<Vec<RecordedRequest>>> = Arc::new(Mutex::new(Vec::new()));
        let recorded = Arc::clone(&requests);
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { continue };
                let Some(request) = read_http_request(&mut stream) else {
                    continue;
                };
                let (status, content_type, body) = respond(&request);
                recorded.lock().unwrap().push(request);
                let reason = match status {
                    200 => "OK",
                    404 => "Not Found",
                    500 => "Internal Server Error",
                    _ => "Status",
                };
                let response = format!(
                    "HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\n\
                     Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = stream.write_all(response.as_bytes());
                let _ = stream.flush();
            }
        });
        Self {
            base_url: format!("http://127.0.0.1:{port}"),
            requests,
        }
    }

    fn requests(&self) -> Vec<RecordedRequest> {
        self.requests.lock().unwrap().clone()
    }

    /// Like `start`, but the caller controls exactly how the SSE response
    /// body is chunked onto the wire: each returned piece is written with
    /// its own `flush()` and a short delay before the next. Used to prove an
    /// SSE event survives being split across a REAL network read boundary —
    /// `llm_stream.rs`'s `SseLineBuffer` already has synthetic in-memory
    /// `Vec<u8>` split coverage; this closes the gap at the actual streaming
    /// path the CLI binary runs (finding #1's "nice to have" test).
    fn start_streaming<F>(chunks: F) -> Self
    where
        F: Fn(&RecordedRequest) -> Vec<String> + Send + 'static,
    {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind fake server");
        let port = listener.local_addr().unwrap().port();
        let requests: Arc<Mutex<Vec<RecordedRequest>>> = Arc::new(Mutex::new(Vec::new()));
        let recorded = Arc::clone(&requests);
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { continue };
                let Some(request) = read_http_request(&mut stream) else {
                    continue;
                };
                let pieces = chunks(&request);
                recorded.lock().unwrap().push(request);
                let total_len: usize = pieces.iter().map(String::len).sum();
                let header = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\
                     Content-Length: {total_len}\r\nConnection: close\r\n\r\n"
                );
                let _ = stream.write_all(header.as_bytes());
                let _ = stream.flush();
                for piece in pieces {
                    let _ = stream.write_all(piece.as_bytes());
                    let _ = stream.flush();
                    // Force separate reads on the client side rather than
                    // letting the OS coalesce back-to-back writes into one.
                    std::thread::sleep(std::time::Duration::from_millis(40));
                }
            }
        });
        Self {
            base_url: format!("http://127.0.0.1:{port}"),
            requests,
        }
    }
}

/// Parse one HTTP request (request line, headers, Content-Length body).
fn read_http_request(stream: &mut std::net::TcpStream) -> Option<RecordedRequest> {
    stream
        .set_read_timeout(Some(std::time::Duration::from_secs(10)))
        .ok()?;
    let mut raw = Vec::new();
    let mut buf = [0u8; 4096];
    let header_end = loop {
        match raw.windows(4).position(|w| w == b"\r\n\r\n") {
            Some(pos) => break pos,
            None => {
                let n = stream.read(&mut buf).ok()?;
                if n == 0 {
                    return None;
                }
                raw.extend_from_slice(&buf[..n]);
            }
        }
    };
    let head = String::from_utf8_lossy(&raw[..header_end]).to_string();
    let mut lines = head.lines();
    let request_line = lines.next()?;
    let mut parts = request_line.split_whitespace();
    let method = parts.next()?.to_string();
    let path = parts.next()?.to_string();
    let mut headers = HashMap::new();
    for line in lines {
        if let Some((k, v)) = line.split_once(':') {
            headers.insert(k.trim().to_ascii_lowercase(), v.trim().to_string());
        }
    }
    let content_length: usize = headers
        .get("content-length")
        .and_then(|v| v.parse().ok())
        .unwrap_or(0);
    let mut body_bytes = raw[header_end + 4..].to_vec();
    while body_bytes.len() < content_length {
        let n = stream.read(&mut buf).ok()?;
        if n == 0 {
            break;
        }
        body_bytes.extend_from_slice(&buf[..n]);
    }
    Some(RecordedRequest {
        method,
        path,
        headers,
        body: String::from_utf8_lossy(&body_bytes).to_string(),
    })
}

/// Write `.kim/cli-config.json` under a fresh temp HOME and return its TempDir.
fn temp_home_with_config(config: &serde_json::Value) -> tempfile::TempDir {
    let home = tempfile::tempdir().expect("temp home");
    let kim_dir = home.path().join(".kim");
    std::fs::create_dir_all(&kim_dir).unwrap();
    std::fs::write(
        kim_dir.join("cli-config.json"),
        serde_json::to_string_pretty(config).unwrap(),
    )
    .unwrap();
    home
}

/// Run `kim chat <prompt>` with an isolated HOME/cwd and a clean env.
///
/// The binary is copied into the temp cwd first: `find_kim_repo_root` walks up
/// from the running executable as a last resort, and the freshly built binary
/// under `target/` lives inside the Kim repo — running it in place would
/// discover `orchestrator/agent.py` and take the agentic path instead of the
/// provider-streaming path these tests exercise.
fn run_kim_chat(home: &tempfile::TempDir, prompt: &str, extra_env: &[(&str, &str)]) -> Output {
    let cwd = tempfile::tempdir().expect("temp cwd");
    let kim_bin = cwd.path().join("kim");
    std::fs::copy(env!("CARGO_BIN_EXE_kim"), &kim_bin).expect("copy kim binary");
    let mut cmd = Command::new(&kim_bin);
    cmd.args(["chat", prompt])
        .current_dir(cwd.path())
        .env("HOME", home.path())
        // Never discover a real Kim repo root / desktop pairing / proxies.
        .env_remove("KIM_PROJECT_ROOT")
        .env_remove("KIM_API_KEY")
        .env_remove("HTTP_PROXY")
        .env_remove("HTTPS_PROXY")
        .env_remove("ALL_PROXY")
        .env_remove("http_proxy")
        .env_remove("https_proxy")
        .env_remove("all_proxy")
        .env("NO_COLOR", "1")
        .stdin(Stdio::null());
    for (k, v) in extra_env {
        cmd.env(k, v);
    }
    cmd.output().expect("kim binary should run")
}

/// Run `kim <args...>` with the same isolation as `run_kim_chat` (fresh HOME +
/// temp cwd + clean proxy env), for subcommands other than `chat`.
fn run_kim(home: &tempfile::TempDir, args: &[&str], extra_env: &[(&str, &str)]) -> Output {
    let cwd = tempfile::tempdir().expect("temp cwd");
    let kim_bin = cwd.path().join("kim");
    std::fs::copy(env!("CARGO_BIN_EXE_kim"), &kim_bin).expect("copy kim binary");
    let mut cmd = Command::new(&kim_bin);
    cmd.args(args)
        .current_dir(cwd.path())
        .env("HOME", home.path())
        .env_remove("KIM_PROJECT_ROOT")
        .env_remove("KIM_API_KEY")
        .env_remove("HTTP_PROXY")
        .env_remove("HTTPS_PROXY")
        .env_remove("ALL_PROXY")
        .env_remove("http_proxy")
        .env_remove("https_proxy")
        .env_remove("all_proxy")
        .env("NO_COLOR", "1")
        .stdin(Stdio::null());
    for (k, v) in extra_env {
        cmd.env(k, v);
    }
    cmd.output().expect("kim binary should run")
}

fn combined_output(output: &Output) -> String {
    format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    )
}

fn ollama_config(base_url: &str) -> serde_json::Value {
    serde_json::json!({
        "provider": "ollama",
        "model": "fake-test-model",
        "theme": "dark-neovim",
        "ollama_base_url": base_url,
        "desktop_bridge_url": "http://127.0.0.1:1", // unroutable; unused here
        "api_keys": {}
    })
}

// ── OpenAI-compatible (ollama) route ────────────────────────────────────────

#[test]
fn chat_oneshot_streams_reply_and_sends_correct_request() {
    let server = FakeServer::start(|req| {
        assert_eq!(req.path, "/v1/chat/completions", "unexpected path");
        let sse = concat!(
            "data: {\"choices\":[{\"delta\":{\"content\":\"<think>pondering hard</think>\"}}]}\n\n",
            "data: {\"choices\":[{\"delta\":{\"content\":\"Hello from the fake model.\"}}]}\n\n",
            "data: [DONE]\n\n"
        );
        (200, "text/event-stream", sse.to_string())
    });
    let home = temp_home_with_config(&ollama_config(&server.base_url));

    let output = run_kim_chat(&home, "integration test ping", &[]);

    assert!(
        output.status.success(),
        "kim chat should exit 0; output: {}",
        combined_output(&output)
    );
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        stdout.contains("Hello from the fake model."),
        "streamed answer should be printed; stdout: {stdout}"
    );

    // Request plumbing: exactly one chat-completions POST with the configured
    // model, stream:true, the Kim system prompt, and the user's prompt.
    let requests = server.requests();
    assert_eq!(requests.len(), 1, "expected one provider request");
    let req = &requests[0];
    assert_eq!(req.method, "POST");
    assert_eq!(
        req.headers.get("authorization").map(String::as_str),
        Some("Bearer ollama"),
        "keyless ollama must fall back to the literal 'ollama' bearer token"
    );
    let body = req.json_body();
    assert_eq!(body["model"], "fake-test-model");
    assert_eq!(body["stream"], true);
    let messages = body["messages"].as_array().expect("messages array");
    assert!(messages.len() >= 2, "system + user message expected");
    assert_eq!(messages[0]["role"], "system");
    assert!(
        messages[0]["content"]
            .as_str()
            .unwrap()
            .contains("You are Kim"),
        "system prompt must be prepended"
    );
    let last = messages.last().unwrap();
    assert_eq!(last["role"], "user");
    assert_eq!(last["content"], "integration test ping");
}

// #1 "nice to have": an SSE event split across two REAL network reads (not
// just a synthetic in-memory Vec<u8> split) must still assemble correctly —
// exercises the actual streaming path (`stream_openai_compatible` →
// `SseLineBuffer::push_chunk`) end to end through the real `kim` binary.
#[test]
fn chat_oneshot_survives_sse_event_split_across_two_network_reads() {
    let full = concat!(
        "data: {\"choices\":[{\"delta\":{\"content\":\"Hello from a split chunk.\"}}]}\n\n",
        "data: [DONE]\n\n"
    );
    // Split mid-value, so neither network write on its own contains a
    // complete SSE line — the assembler must buffer across the boundary.
    let split_at = full.find("Hel").expect("marker present") + 3;
    let (first, second) = full.split_at(split_at);
    let pieces = vec![first.to_string(), second.to_string()];

    let server = FakeServer::start_streaming(move |req| {
        assert_eq!(req.path, "/v1/chat/completions", "unexpected path");
        pieces.clone()
    });
    let home = temp_home_with_config(&ollama_config(&server.base_url));

    let output = run_kim_chat(&home, "split chunk test", &[]);

    assert!(
        output.status.success(),
        "kim chat should exit 0 even when the SSE event is split across network reads; output: {}",
        combined_output(&output)
    );
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        stdout.contains("Hello from a split chunk."),
        "the full streamed answer must survive a real network-level SSE line split; stdout: {stdout}"
    );
}

#[test]
fn chat_oneshot_surfaces_in_stream_provider_error_and_exits_nonzero() {
    let server = FakeServer::start(|_| {
        let sse = concat!(
            "data: {\"error\":{\"message\":\"model 'nope' not found, try pulling it first\"}}\n\n",
            "data: [DONE]\n\n"
        );
        (200, "text/event-stream", sse.to_string())
    });
    let home = temp_home_with_config(&ollama_config(&server.base_url));

    let output = run_kim_chat(&home, "trigger the error", &[]);

    assert!(
        !output.status.success(),
        "in-stream provider error must produce a non-zero exit"
    );
    let all = combined_output(&output);
    assert!(
        all.contains("not found"),
        "the provider error detail must be surfaced; output: {all}"
    );
}

#[test]
fn chat_oneshot_reports_http_error_status() {
    let server = FakeServer::start(|_| (500, "text/plain", "kaboom".to_string()));
    let home = temp_home_with_config(&ollama_config(&server.base_url));

    let output = run_kim_chat(&home, "hello", &[]);

    assert!(!output.status.success(), "HTTP 500 must exit non-zero");
    let all = combined_output(&output);
    assert!(
        all.contains("500") && all.contains("kaboom"),
        "status and body must be surfaced; output: {all}"
    );
}

// ── Desktop-bridge (browser provider) route ─────────────────────────────────

#[test]
fn browser_provider_routes_task_through_bridge_with_token() {
    let server = FakeServer::start(|req| match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/health") => (200, "application/json", "{\"ok\":true}".to_string()),
        ("POST", "/v1/task") => (
            200,
            "application/json",
            "{\"response\":\"bridge says hi\"}".to_string(),
        ),
        other => panic!("unexpected bridge request: {other:?}"),
    });
    let config = serde_json::json!({
        "provider": "browser:claude",
        "model": "browser-claude",
        "theme": "dark-neovim",
        "ollama_base_url": "http://127.0.0.1:1",
        "desktop_bridge_url": server.base_url,
        "api_keys": {}
    });
    let home = temp_home_with_config(&config);

    let output = run_kim_chat(
        &home,
        "summarize this repo",
        &[("KIM_API_KEY", "secret-token-123")],
    );

    assert!(
        output.status.success(),
        "bridge chat should exit 0; output: {}",
        combined_output(&output)
    );
    assert!(
        String::from_utf8_lossy(&output.stdout).contains("bridge says hi"),
        "bridge response must be printed; stdout: {}",
        String::from_utf8_lossy(&output.stdout)
    );

    let requests = server.requests();
    // Health probe first, then the task submission.
    assert!(
        requests
            .iter()
            .any(|r| r.method == "GET" && r.path == "/health"),
        "bridge availability must be probed via /health"
    );
    let task = requests
        .iter()
        .find(|r| r.path == "/v1/task")
        .expect("a /v1/task request must be sent");
    assert_eq!(
        task.headers.get("x-kim-token").map(String::as_str),
        Some("secret-token-123"),
        "KIM_API_KEY must be forwarded as X-Kim-Token"
    );
    let body = task.json_body();
    assert_eq!(body["task"], "summarize this repo");
    assert!(
        body["session_id"]
            .as_str()
            .is_some_and(|id| id.starts_with("session-")),
        "the CLI conversation id must be forwarded so /new resets the desktop browser thread; body: {body}"
    );
    assert_eq!(
        body["provider"], "browser:claude",
        "provider name must be forwarded so desktop routes to the right browser tab"
    );
}

#[test]
fn browser_provider_errors_cleanly_when_bridge_is_down() {
    // Bridge answers, but with 404 on both health endpoints → "not running".
    let server = FakeServer::start(|_| (404, "text/plain", "nope".to_string()));
    let config = serde_json::json!({
        "provider": "browser:gemini",
        "model": "browser-gemini",
        "theme": "dark-neovim",
        "ollama_base_url": "http://127.0.0.1:1",
        "desktop_bridge_url": server.base_url,
        "api_keys": {}
    });
    let home = temp_home_with_config(&config);

    let output = run_kim_chat(&home, "anyone home?", &[]);

    assert!(!output.status.success(), "downed bridge must exit non-zero");
    let all = combined_output(&output);
    // With no Kim repo root in this isolated cwd the local Playwright agent path
    // is unavailable, so a browser provider with a downed bridge now reports the
    // actionable env-setup message (install.bat / ./install.sh) instead of the
    // old "start the desktop" string.
    assert!(
        all.contains("Browser provider can't run") && all.contains("install.bat"),
        "browser providers must get the actionable env-setup message; output: {all}"
    );
    // Both health endpoints were probed; the task was never submitted.
    let requests = server.requests();
    assert!(requests.iter().all(|r| r.method == "GET"));
    assert!(
        requests.iter().any(|r| r.path == "/health")
            && requests.iter().any(|r| r.path == "/v1/health"),
        "both health endpoints should be tried; got {requests:?}"
    );
}

// ── F-E-1: `kim doctor` exit-code gating ────────────────────────────────────

// A closed Ollama endpoint plus a bogus model is a provider-specific (optional)
// failure. `kim doctor --strict` must exit non-zero (CI/install-script gate);
// plain `kim doctor` keeps exit 0 because the only failing checks are optional.
// Before the fix, `kim doctor` always exited 0 and `--strict` was ignored.
#[test]
fn doctor_strict_exits_nonzero_on_provider_failure_but_plain_stays_zero() {
    // 127.0.0.1:1 refuses instantly — no network, fully deterministic.
    let config = serde_json::json!({
        "provider": "ollama",
        "model": "definitely-not-a-real-model-xyz",
        "theme": "dark-neovim",
        "ollama_base_url": "http://127.0.0.1:1",
        "desktop_bridge_url": "http://127.0.0.1:1",
        "api_keys": {}
    });
    let home = temp_home_with_config(&config);

    let strict = run_kim(&home, &["doctor", "--strict"], &[]);
    assert!(
        !strict.status.success(),
        "kim doctor --strict must exit non-zero when a provider check fails; output:\n{}",
        combined_output(&strict)
    );

    let plain = run_kim(&home, &["doctor"], &[]);
    // Plain doctor only gates on required checks (a Python interpreter, present
    // on the host); provider-specific failures do not gate it.
    assert!(
        plain.status.success(),
        "plain kim doctor must stay exit 0 when only provider-specific checks fail; output:\n{}",
        combined_output(&plain)
    );
    // Both still print the human-readable report.
    assert!(combined_output(&plain).contains("KimCLI doctor"));
}

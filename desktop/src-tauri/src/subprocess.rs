use crate::*;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

/// RUN-IDENTITY: copy the `run_id` / `session_id` envelope off a raw agent
/// stdout line onto a curated `kim:*` payload. The typed `KimEvent` enum drops
/// these envelope-only fields on decode, so they must be re-attached here for
/// the frontend to route/file by run identity. Existing payload fields of the
/// same name win; a line that carries neither id is passed through untouched
/// (legacy/bridge streams stay byte-compatible). Pure, so it is unit-tested.
pub(crate) fn merge_run_envelope(
    raw_line: &str,
    mut payload: serde_json::Value,
) -> serde_json::Value {
    if let Ok(env) = serde_json::from_str::<serde_json::Value>(raw_line) {
        if let Some(obj) = payload.as_object_mut() {
            for key in ["run_id", "session_id"] {
                if let Some(v) = env.get(key) {
                    obj.entry(key).or_insert_with(|| v.clone());
                }
            }
        }
    }
    payload
}

/// Maximum bytes buffered for a single stdout/stderr line before Kim truncates
/// it. F-D-5: `BufReader::lines().next_line()` accumulates an entire line (a
/// child that emits a single multi-GB line, or floods with no newline, grows
/// Rust-side memory unboundedly) before yielding, and the request-body path
/// already has M-BRIDGE-3's 32 MB cap while the stdout path had none. 8 MiB is
/// far above any legitimate typed event or narration line.
pub(crate) const MAX_STDOUT_LINE_BYTES: usize = 8 * 1024 * 1024;

/// A bounded, chunk-fed line splitter. F-D-5: feed it whatever bytes a child's
/// stdout/stderr read returns; it yields completed newline-delimited lines but
/// never buffers more than `cap` bytes for any single line — an over-cap line
/// is truncated (with a marker) and the physical overflow is drained without
/// buffering. This is what bounds peak memory instead of `next_line()`'s
/// unbounded `String` growth. Pure (no I/O), so it is unit-tested directly.
pub(crate) struct CappedLineSplitter {
    cap: usize,
    line: Vec<u8>,
    overflowing: bool,
}

impl CappedLineSplitter {
    pub(crate) fn new(cap: usize) -> Self {
        Self {
            cap,
            line: Vec::new(),
            overflowing: false,
        }
    }

    /// Feed one read chunk, invoking `emit` for each completed line.
    pub(crate) fn push(&mut self, mut chunk: &[u8], mut emit: impl FnMut(String)) {
        while let Some(pos) = chunk.iter().position(|&b| b == b'\n') {
            if !self.overflowing {
                self.append_capped(&chunk[..pos]);
            }
            emit(self.take_line());
            chunk = &chunk[pos + 1..];
        }
        if !chunk.is_empty() && !self.overflowing {
            self.append_capped(chunk);
        }
    }

    /// Flush a trailing partial line (input that ended without a newline),
    /// matching `BufReader::lines()`, which yields the final unterminated line.
    pub(crate) fn finish(&mut self, mut emit: impl FnMut(String)) {
        if !self.line.is_empty() || self.overflowing {
            emit(self.take_line());
        }
    }

    fn append_capped(&mut self, seg: &[u8]) {
        if self.line.len() >= self.cap {
            self.overflowing = true;
            return;
        }
        let room = self.cap - self.line.len();
        let take = room.min(seg.len());
        self.line.extend_from_slice(&seg[..take]);
        if self.line.len() >= self.cap {
            self.overflowing = true;
        }
    }

    fn take_line(&mut self) -> String {
        // Match BufReader::lines() CRLF handling: drop a trailing '\r'.
        if self.line.last() == Some(&b'\r') {
            self.line.pop();
        }
        let mut out = String::from_utf8_lossy(&self.line).into_owned();
        if self.overflowing {
            out.push_str(" …[Kim truncated an oversized stdout line]");
        }
        self.line.clear();
        self.overflowing = false;
        out
    }
}

/// F-H-3: flood-protection budget for forwarding undecodable CHAT stdout lines.
/// A burst of this many is forwarded to the UI immediately; the token bucket
/// then refills one token every `PROTOCOL_ERROR_REFILL_MS`. Sized so genuine
/// anomalies (version skew, a stray `print()`, a partial JSON line from a killed
/// process) surface, but a runaway spew can't repaint the UI thousands of
/// times a second.
pub(crate) const PROTOCOL_ERROR_BURST: u32 = 20;
pub(crate) const PROTOCOL_ERROR_REFILL_MS: u64 = 250;

/// F-H-3: a per-run token-bucket rate limiter + decode-failure counter for the
/// typed-mode stdout path. Chat runs previously DROPPED SILENTLY any stdout line
/// that failed `KimEvent` decode; those lines are now forwarded raw, but through
/// this limiter so a flood cannot spam the UI. Pure (time is injected via
/// `record_at`), so the bucket/counter behavior is unit-tested directly; the
/// production path reads a monotonic clock through `uptime_ms`.
pub(crate) struct ProtocolErrorLimiter {
    capacity: u32,
    tokens: u32,
    refill_ms: u64,
    last_refill_ms: u64,
    total_failures: u64,
    suppressed: u64,
    start: Instant,
}

impl ProtocolErrorLimiter {
    pub(crate) fn new(capacity: u32, refill_ms: u64) -> Self {
        Self {
            capacity,
            tokens: capacity,
            refill_ms,
            last_refill_ms: 0,
            total_failures: 0,
            suppressed: 0,
            start: Instant::now(),
        }
    }

    /// The default per-run limiter used by the stdout pump.
    pub(crate) fn for_stdout() -> Self {
        Self::new(PROTOCOL_ERROR_BURST, PROTOCOL_ERROR_REFILL_MS)
    }

    /// Milliseconds since this limiter was constructed (monotonic). Used by the
    /// production path to feed `record_at` a real clock; tests inject `now_ms`
    /// directly instead.
    pub(crate) fn uptime_ms(&self) -> u64 {
        self.start.elapsed().as_millis() as u64
    }

    /// Record one decode failure at `now_ms` and decide whether its raw line may
    /// be forwarded to the UI. Every call increments `total_failures`; a call
    /// that finds no token increments `suppressed` and returns `false` (the line
    /// is counted but withheld — flood protection, NOT the old silent drop).
    pub(crate) fn record_at(&mut self, now_ms: u64) -> bool {
        self.total_failures = self.total_failures.saturating_add(1);
        // Refill: one token per `refill_ms` elapsed, capped at `capacity`.
        if self.refill_ms > 0 {
            let elapsed = now_ms.saturating_sub(self.last_refill_ms);
            let refilled = (elapsed / self.refill_ms) as u32;
            if refilled > 0 {
                self.tokens = self.tokens.saturating_add(refilled).min(self.capacity);
                // Advance by the consumed whole intervals so fractional time is
                // not lost (prevents drift under steady input).
                self.last_refill_ms = self
                    .last_refill_ms
                    .saturating_add(refilled as u64 * self.refill_ms);
            }
        }
        if self.tokens > 0 {
            self.tokens -= 1;
            true
        } else {
            self.suppressed = self.suppressed.saturating_add(1);
            false
        }
    }

    pub(crate) fn total_failures(&self) -> u64 {
        self.total_failures
    }

    pub(crate) fn suppressed(&self) -> u64 {
        self.suppressed
    }
}

/// F-H-3: how a typed-mode stdout line that FAILED `KimEvent` decode is handled.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum DecodeFailureAction {
    /// Forward the raw line on `kim-agent-output` — codex passthrough (always),
    /// or a chat line the rate limiter still has budget for.
    ForwardRaw,
    /// Chat flood protection engaged: the line is counted but withheld from the
    /// UI. (Distinct from the pre-F-H-3 SILENT drop, which never counted.)
    Suppress,
}

/// Decide the fate of an undecodable typed-mode stdout line. Codex forwards
/// unconditionally (preserving the pre-F-H-3 passthrough and never consuming the
/// chat budget); chat runs — which USED TO SILENTLY DROP the line — now forward
/// through the token-bucket limiter, degrading to `Suppress` only under a
/// genuine flood.
pub(crate) fn route_decode_failure(
    is_codex: bool,
    limiter: &mut ProtocolErrorLimiter,
    now_ms: u64,
) -> DecodeFailureAction {
    if is_codex {
        return DecodeFailureAction::ForwardRaw;
    }
    if limiter.record_at(now_ms) {
        DecodeFailureAction::ForwardRaw
    } else {
        DecodeFailureAction::Suppress
    }
}

pub(crate) fn forward_agent_stdout_line(
    app: &tauri::AppHandle,
    ipc_typed: bool,
    is_codex: bool,
    limiter: &mut ProtocolErrorLimiter,
    line: &str,
) {
    if ipc_typed {
        if let Ok(event) = serde_json::from_str::<KimEvent>(line) {
            // RUN-IDENTITY: the typed `KimEvent` enum intentionally ignores the
            // run/session envelope fields, so pull them straight off the raw
            // line and re-attach them to every curated `kim:*` payload. This is
            // what lets the frontend file a run's output under ITS session and
            // reject a foreign run's events, instead of routing by "current
            // view". Absent on legacy/bridge streams -> nothing is added.
            let emit = |name: &str, payload: serde_json::Value| {
                let _ = app.emit(name, merge_run_envelope(line, payload));
            };
            match &event {
                KimEvent::Status { message } => {
                    emit("kim:status", serde_json::json!({"message": message}));
                }
                KimEvent::Plan { steps } => {
                    emit("kim:plan", serde_json::json!({"steps": steps}));
                }
                KimEvent::Step { n, data } => {
                    emit("kim:step", serde_json::json!({"n": n, "data": data}));
                }
                KimEvent::Done { n } => {
                    emit("kim:done", serde_json::json!({"n": n}));
                }
                KimEvent::Context {
                    cumulative_input,
                    budget,
                    phase,
                    percent,
                    last_input,
                    last_output,
                    source,
                    estimate,
                } => {
                    emit(
                        "kim:context",
                        serde_json::json!({
                            "cumulative_input": cumulative_input,
                            "budget": budget,
                            "phase": phase,
                            "percent": percent,
                            "last_input": last_input,
                            "last_output": last_output,
                            "source": source,
                            "estimate": estimate,
                        }),
                    );
                }
                KimEvent::Stats {
                    input,
                    output,
                    total,
                } => {
                    emit(
                        "kim:stats",
                        serde_json::json!({"input": input, "output": output, "total": total}),
                    );
                }
                KimEvent::UiScreenshotFlash => {
                    emit("kim:ui", serde_json::json!({"action": "screenshot_flash"}));
                }
                KimEvent::UiShow => {
                    emit("kim:ui", serde_json::json!({"action": "show"}));
                }
                KimEvent::RunDone {
                    termination,
                    success,
                } => {
                    emit(
                        "kim:run-done",
                        serde_json::json!({"termination": termination, "success": success}),
                    );
                }
                KimEvent::RunFailed {
                    reason,
                    recoverable,
                    suggestion,
                } => {
                    emit(
                        "kim:run-failed",
                        serde_json::json!({
                            "reason": reason,
                            "recoverable": recoverable,
                            "suggestion": suggestion,
                        }),
                    );
                }
                KimEvent::AgentDone { success } => {
                    emit("kim:agent-done", serde_json::json!({"success": success}));
                }
                KimEvent::AgentCancelled { success } => {
                    emit("kim:agent-cancelled", serde_json::json!({"success": success}));
                }
                KimEvent::AgentError { error } => {
                    emit("kim:agent-error", serde_json::json!({"error": error}));
                }
                KimEvent::ProviderError { code, retryable } => {
                    emit(
                        "kim:provider-error",
                        serde_json::json!({"code": code, "retryable": retryable}),
                    );
                }
                KimEvent::RateLimited {
                    delay,
                    attempt,
                    max_retries,
                } => {
                    emit(
                        "kim:rate-limited",
                        serde_json::json!({
                            "delay": delay,
                            "attempt": attempt,
                            "max_retries": max_retries,
                        }),
                    );
                }
                KimEvent::HitlApprovalRequest {
                    tool,
                    risk,
                    reason,
                    preview,
                    id,
                } => {
                    // T1: forward the request's correlation id so the UI can echo
                    // it back on the decision; Python only applies a decision whose
                    // id matches the pending request (stale decisions are voided).
                    emit(
                        "kim:hitl-approval-request",
                        serde_json::json!({"tool": tool, "risk": risk, "reason": reason, "preview": preview, "id": id}),
                    );
                }
                KimEvent::HitlApprovalResult { tool, approved } => {
                    emit(
                        "kim:hitl-approval-result",
                        serde_json::json!({"tool": tool, "approved": approved}),
                    );
                }
                KimEvent::Tool { name, args } => {
                    emit("kim:tool", serde_json::json!({"name": name, "args": args}));
                }
                KimEvent::Answer { text } => {
                    emit("kim:answer", serde_json::json!({"text": text}));
                }
                KimEvent::Diff {
                    path,
                    added,
                    removed,
                } => {
                    emit(
                        "kim:diff",
                        serde_json::json!({"path": path, "added": added, "removed": removed}),
                    );
                }
                KimEvent::Activity { kind, text } => {
                    emit(
                        "kim:activity",
                        serde_json::json!({"kind": kind, "text": text}),
                    );
                }
                // Codex app-server transport (parity Part 3/5): native
                // approvals, live command output, plan/diff/token streams.
                KimEvent::CommandApprovalRequest {
                    id,
                    command,
                    cwd,
                    reason,
                    risk,
                    network,
                    amendment,
                } => {
                    emit(
                        "kim:command-approval-request",
                        serde_json::json!({
                            "id": id,
                            "command": command,
                            "cwd": cwd,
                            "reason": reason,
                            "risk": risk,
                            "network": network,
                            "amendment": amendment,
                        }),
                    );
                }
                KimEvent::FileChangeApprovalRequest { id, files, reason } => {
                    emit(
                        "kim:file-change-approval-request",
                        serde_json::json!({"id": id, "files": files, "reason": reason}),
                    );
                }
                KimEvent::UserInputRequest {
                    id,
                    kind,
                    item_id,
                    questions,
                    message,
                } => {
                    // C4: codex asked the USER a question (item/tool/requestUserInput
                    // or an MCP elicitation). Forward it so the frontend can render
                    // the card; the answer comes back via the respond_user_input
                    // command, which writes a user_input stdin line to the bridge.
                    emit(
                        "kim:user-input-request",
                        serde_json::json!({
                            "id": id,
                            "kind": kind,
                            "item_id": item_id,
                            "questions": questions,
                            "message": message,
                        }),
                    );
                }
                KimEvent::CommandOutput { item_id, chunk } => {
                    emit(
                        "kim:command-output",
                        serde_json::json!({"item_id": item_id, "chunk": chunk}),
                    );
                }
                KimEvent::AssistantDelta { chunk } => {
                    emit("kim:assistant-delta", serde_json::json!({"chunk": chunk}));
                }
                KimEvent::ReasoningDelta { chunk } => {
                    emit("kim:reasoning-delta", serde_json::json!({"chunk": chunk}));
                }
                KimEvent::PlanUpdate { steps } => {
                    emit("kim:plan-update", serde_json::json!({"steps": steps}));
                }
                KimEvent::DiffUpdate { unified_diff } => {
                    emit(
                        "kim:diff-update",
                        serde_json::json!({"unified_diff": unified_diff}),
                    );
                }
                KimEvent::TokenUsage {
                    input,
                    output,
                    total,
                } => {
                    emit(
                        "kim:token-usage",
                        serde_json::json!({"input": input, "output": output, "total": total}),
                    );
                }
                KimEvent::ItemLifecycle {
                    item_id,
                    kind,
                    phase,
                    title,
                } => {
                    emit(
                        "kim:item-lifecycle",
                        serde_json::json!({
                            "item_id": item_id,
                            "kind": kind,
                            "phase": phase,
                            "title": title,
                        }),
                    );
                }
                KimEvent::TurnLifecycle { phase, turn_id } => {
                    emit(
                        "kim:turn-lifecycle",
                        serde_json::json!({"phase": phase, "turn_id": turn_id}),
                    );
                }
            }
        } else {
            // F-H-3: the line failed `KimEvent` decode. Codex forwards it raw
            // (unchanged passthrough). A CHAT run used to SILENTLY DROP it — no
            // log, no counter — so a version-skewed / partial-JSON / stray-print
            // line just vanished and the run looked like it produced nothing.
            // Now forward it too, rate-limited via a per-run token bucket so a
            // flood cannot spam the UI; the withheld overflow is still counted.
            let now_ms = limiter.uptime_ms();
            match route_decode_failure(is_codex, limiter, now_ms) {
                DecodeFailureAction::ForwardRaw => {
                    let _ = app.emit("kim-agent-output", line);
                }
                DecodeFailureAction::Suppress => {
                    // One-time process-log note the first time flood protection
                    // trips this run — the lines are counted (total_failures) but
                    // withheld from the UI to avoid a repaint storm.
                    if limiter.suppressed() == 1 {
                        eprintln!(
                            "[Kim] flood protection: withholding undecodable chat stdout \
                             from the UI ({} decode failures so far this run). Check the \
                             orchestrator version / ipc_protocol if this persists.",
                            limiter.total_failures()
                        );
                    }
                }
            }
        }
    } else {
        let _ = app.emit("kim-agent-output", line);
    }
}

// HITL approval commands live in `crate::hitl` (K1). Kept out of this file so
// the K1 approval-vocabulary work does not regrow this already-oversized
// module ahead of its scheduled K2 decomposition.

/// K3: write a mid-run steering message to the running agent's stdin. Python's
/// stdin pump folds it into memory as a user message before the next LLM call.
#[tauri::command]
pub(crate) async fn steer_task(text: String) -> Result<(), String> {
    let payload = serde_json::json!({ "type": "user_steer", "text": text }).to_string();
    let msg = format!("{payload}\n");
    crate::task_runtime::write_stdin_line(&msg).await
}

include!("events.gen.rs");

/// Resolve a Python interpreter or bundled orchestrator sidecar.
///
/// Resolution order (first match wins):
///   1. Bundled `kim-orchestrator` sidecar adjacent to the Tauri executable
///      (set when running as a packaged .app; Tauri places sidecars in the
///      same MacOS/ directory as the main binary).
///   2. `~/.kim/venv` (or `.venv`) under the user's home directory (created by
///      the install script).
///   3. Project-local `venv/` or `.venv/` under `project_root`.
///   4. System-level `python3` / `python` on PATH.
///
/// F-L-9: the former `~/.kim_root/venv` candidates were dead — install.sh
/// writes `~/.kim_root` as a *file* (`echo "$PWD" > ~/.kim_root`, read as a
/// file by paths.rs), so a `~/.kim_root/venv/bin/python` directory-join could
/// never match anything the install script produced. Removed.
///
/// When the sidecar is found, the caller should invoke it directly (as a
/// standalone executable) rather than as `<interpreter> -m orchestrator.agent`.
/// The caller is responsible for detecting sidecar mode via
/// `is_bundled_orchestrator()` and adjusting the command accordingly.
pub(crate) fn find_python_interpreter(project_root: &Path) -> Result<String, String> {
    // ── 1. Bundled sidecar — highest priority when running as a packaged app ──
    if let Some(sidecar) = find_bundled_orchestrator() {
        return Ok(sidecar);
    }

    // ── 2. Install-script venv in ~/.kim ──────────────────────────────────────
    if let Some(home) = dirs_home() {
        for c in install_script_venv_candidates(&home) {
            if c.exists() {
                return Ok(c.to_string_lossy().to_string());
            }
        }
    }

    // ── 3. Project-local venv ─────────────────────────────────────────────────
    let candidates = [
        project_root.join("venv").join("bin").join("python"),
        project_root.join(".venv").join("bin").join("python"),
        project_root.join("venv").join("Scripts").join("python.exe"),
        project_root
            .join(".venv")
            .join("Scripts")
            .join("python.exe"),
    ];
    for candidate in candidates {
        if candidate.exists() {
            return Ok(candidate.to_string_lossy().to_string());
        }
    }

    // ── 4. System Python ──────────────────────────────────────────────────────
    #[cfg(target_os = "windows")]
    let cmd_candidates = ["py", "python", "python3"];
    #[cfg(not(target_os = "windows"))]
    let cmd_candidates = ["python3", "python"];

    for cmd in cmd_candidates {
        if command_exists(cmd) {
            return Ok(cmd.to_string());
        }
    }

    Err(
        "No Python interpreter found. Install Python 3, create a project venv (venv/.venv), \
         or rebuild the app with the bundled kim-orchestrator sidecar."
            .to_string(),
    )
}

/// Returns the path to the bundled `kim-orchestrator` sidecar if running
/// as a packaged Tauri app. Returns `None` in development mode.
///
/// On macOS, Tauri places sidecars in `<app>.app/Contents/MacOS/` adjacent
/// to the main binary. The sidecar binary name carries the platform target
/// triple at bundle time but is resolved without it at runtime via the
/// `externalBin` Tauri config mechanism. We look for both the bare name and
/// the current-platform suffixed name so the function works in both contexts.
fn find_bundled_orchestrator() -> Option<String> {
    // Resolve the directory containing the current executable.
    let exe = std::env::current_exe().ok()?;
    let exe_dir = exe.parent()?;

    // On macOS .app bundles, exe_dir ends with Contents/MacOS/.
    // On Linux AppImage / Windows installer, exe_dir is the install root.
    let sidecar_name = "kim-orchestrator";
    let target_triple = std::env::consts::ARCH.to_string()
        + "-"
        + if cfg!(target_os = "macos") {
            "apple-darwin"
        } else if cfg!(target_os = "linux") {
            "unknown-linux-gnu"
        } else if cfg!(target_os = "windows") {
            "pc-windows-msvc"
        } else {
            "unknown"
        };

    // Try bare name first (development builds / installed via direct copy).
    let bare = exe_dir.join(sidecar_name);
    if bare.exists() {
        return Some(bare.to_string_lossy().to_string());
    }

    // Try platform-suffixed name (Tauri sidecar bundle convention).
    let suffixed = exe_dir.join(format!("{}-{}", sidecar_name, target_triple));
    if suffixed.exists() {
        return Some(suffixed.to_string_lossy().to_string());
    }

    // Windows: also check for .exe extension.
    #[cfg(target_os = "windows")]
    {
        let win_bare = exe_dir.join(format!("{}.exe", sidecar_name));
        if win_bare.exists() {
            return Some(win_bare.to_string_lossy().to_string());
        }
    }

    None
}

/// Returns true when the resolved interpreter path points to the bundled
/// `kim-orchestrator` sidecar (a standalone executable, not a Python binary).
/// The caller must invoke it directly rather than via `python -m orchestrator.agent`.
pub(crate) fn is_bundled_orchestrator(interpreter: &str) -> bool {
    interpreter.contains("kim-orchestrator")
}

/// Best-effort home directory resolution (avoids pulling in the `dirs` crate).
fn dirs_home() -> Option<PathBuf> {
    // Try $HOME on Unix / $USERPROFILE on Windows.
    std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .ok()
        .map(PathBuf::from)
}

/// Interpreter candidates for an install-script-created venv under the user's
/// home directory. F-L-9: only `~/.kim/...` — the former `~/.kim_root/...`
/// candidates were dead (install.sh writes `~/.kim_root` as a file, so a
/// `~/.kim_root/venv/bin/python` directory path never existed).
fn install_script_venv_candidates(home: &Path) -> [PathBuf; 2] {
    [
        home.join(".kim").join("venv").join("bin").join("python"),
        home.join(".kim").join(".venv").join("bin").join("python"),
    ]
}

/// F-L-2: actionable message when Kim is about to spawn against a bare system
/// python that is missing its dependencies.
pub(crate) const PYTHON_DEPS_MISSING_MESSAGE: &str = "Kim's Python dependencies are not installed. Kim fell back to your system Python, which does not have Kim's packages. Run ./install.sh, or create the project venv:  python3 -m venv venv && ./venv/bin/pip install -r requirements.txt";

/// True when `interp` is a bare system `python`/`python3`/`py` command (no path
/// separator) rather than a resolved venv / bundled-sidecar interpreter.
/// F-L-2: the bare system python is the last-resort fallback that is the one
/// most likely to be missing Kim's dependencies.
fn is_bare_system_python(interp: &str) -> bool {
    !interp.contains('/') && !interp.contains('\\') && !is_bundled_orchestrator(interp)
}

/// F-L-2: when the interpreter search falls through to a bare system python
/// (every Mac has one), a missing/broken project venv means the orchestrator
/// spawns without its deps and dies instantly with a raw `ModuleNotFoundError:
/// No module named 'anthropic'` in the task stream — a long-known gotcha that
/// was neither detected nor translated. Run a cheap import preflight and, on
/// failure, return an actionable message. The caller surfaces it as the
/// spec-build error (shown in the UI, releases the runner slot). Venv / sidecar
/// interpreters skip the probe (their deps are assumed present).
async fn preflight_python_deps(interpreter: &str) -> Result<(), String> {
    if !is_bare_system_python(interpreter) {
        return Ok(());
    }
    let interp = interpreter.to_string();
    let ok = tokio::task::spawn_blocking(move || {
        std::process::Command::new(&interp)
            .args(["-c", "import mcp, anthropic"])
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false)
    })
    .await
    .unwrap_or(false);
    if ok {
        Ok(())
    } else {
        Err(PYTHON_DEPS_MISSING_MESSAGE.to_string())
    }
}

/// Search PATH for an executable, returning its resolved path. (#20: uses the
/// platform-appropriate lookup command.)
fn executable_on_path(name: &str) -> Option<PathBuf> {
    #[cfg(target_os = "windows")]
    let search_cmd = ("where", name);
    #[cfg(not(target_os = "windows"))]
    let search_cmd = ("which", name);

    let out = std::process::Command::new(search_cmd.0)
        .arg(search_cmd.1)
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    // `where` on Windows returns one match per line; take the first.
    let s = String::from_utf8_lossy(&out.stdout)
        .lines()
        .next()
        .unwrap_or("")
        .trim()
        .to_string();
    if s.is_empty() {
        None
    } else {
        Some(PathBuf::from(s))
    }
}

/// Locate the `codex` binary for the Code tab.
///
/// F-G-1: the bundled-Codex / bundled-Claw arms (`pythonExperimentTool/…`) and
/// the `CLAW_BIN` env / `claw`-on-PATH fallbacks were removed — the vendored
/// `pythonExperimentTool/` tree is deleted, so those arms were dead hooks that
/// could only ever produce a misleading "Build pythonExperimentTool/…" error.
/// Resolution is now simply: `CODEX_BIN` env → `codex` on PATH.
fn find_code_backend(_kim_root: &Path) -> Option<PathBuf> {
    if let Ok(p) = std::env::var("CODEX_BIN") {
        let path = PathBuf::from(p);
        if path.is_file() {
            return Some(path);
        }
    }
    executable_on_path("codex")
}

#[allow(clippy::too_many_arguments)]
#[tauri::command]
pub(crate) async fn send_task(
    task: String,
    provider: Option<String>,
    project_root: Option<String>,
    resume_session_id: Option<String>,
    google_authuser: Option<u32>,
    // Forwarded as KIM_CONTEXT_BUDGET_TOKENS for orchestrator context meter.
    context_budget_tokens: Option<u32>,
    ollama_base_url: Option<String>,
    ollama_mode: Option<String>,
    ollama_local_model: Option<String>,
    ollama_cloud_model: Option<String>,
    ollama_context_limit_override: Option<u32>,
    // HITL permission mode: "full_auto" | "ask_risky" | "ask_always"
    // Maps to KIM_HITL_RISK_THRESHOLD: full_auto=off, ask_risky=high, ask_always=medium
    permission_mode: Option<String>,
    app_handle: tauri::AppHandle,
) -> Result<String, String> {
    use crate::task_spec;

    // AUDIT FIX #6: reserve the runner slot HERE, as the very first thing
    // this function does, instead of merely checking occupancy here and
    // reserving only right before spawn() much further down. The old
    // "check-then-later-reserve" gap let two near-simultaneous send_task
    // calls both pass this initial check and both run the async work below
    // (build_gui_chat_spec / build_gui_codex_spec, which for Gemini
    // triggers a live Google OAuth token refresh) before the second call's
    // reserve finally rejected it -- wasting a real OAuth round-trip (and
    // risking whatever side effects a concurrent refresh has) on a request
    // that was always going to be refused. reserve_slot() performs the same
    // occupancy check under the same lock and atomically reserves in one
    // step, so there is no gap between "found free" and "marked occupied"
    // for a second caller to race into.
    //
    // Every fallible step between this point and the eventual spawn() call
    // must release the slot on its error path (spawn() already does this
    // internally per its own doc comment; the spec-builder failure path
    // below does it explicitly).
    crate::spawn_supervisor::reserve_slot()
        .await
        .map_err(|_| "A task is already running. Stop it before starting a new one.".to_string())?;
    let app_config = app_handle.state::<config::AppConfig>();

    // The Kim repo root (interpreter, PYTHONPATH, cwd) vs the target project
    // the user wants Kim/Codex to work on (MCP tools' PROJECT_ROOT).
    let kim_root = default_project_root();
    let target_root = project_root
        .map(PathBuf::from)
        .filter(|p| p.exists())
        .unwrap_or_else(|| kim_root.clone());
    // Codex mode is ONLY when the target project is a real external project,
    // i.e. distinct from the Kim repo itself. The Chat tab passes the Kim
    // repo as project_root (so MCP tools know the workspace), but those
    // sessions must still land in kim_sessions/, not .codex/sessions/.
    let is_codex = target_root.canonicalize().ok() != kim_root.canonicalize().ok();
    let session_dir = if is_codex {
        target_root.join(".codex").join("sessions")
    } else {
        kim_root.join("kim_sessions")
    };
    let provider_arg = task_spec::promote_provider(provider, "ollama", is_codex);
    let is_browser = task_spec::is_browser_provider(&provider_arg);
    let session_id = resume_session_id
        .clone()
        .unwrap_or_else(|| "gui".to_string());

    // Caller-resolved env extras shared by the orchestrator-backed specs.
    let bridge_cfg = WEBVIEW_BRIDGE_CFG.get().cloned();
    let mut extra = task_spec::EnvBuilder::new();
    if !is_codex || is_browser {
        if let Some(cfg) = &bridge_cfg {
            extra = extra.webview_bridge(Some((cfg.base_url.as_str(), cfg.token.as_str())));
        }
    }
    if is_browser {
        extra = extra.set(
            "KIM_BROWSER_RESTORE_STATUS",
            browser_restore_status_for_session(
                &session_dir,
                resume_session_id.as_deref(),
                &provider_arg,
            ),
        );
    }
    if let Some(idx) = google_authuser {
        extra = extra.set("KIM_GEMINI_AUTHUSER", idx.to_string());
    }

    // Build the pure TaskSpec via the named builder for this spawn shape.
    let inputs = GuiSpawnInputs {
        kim_root: &kim_root,
        target_root: &target_root,
        session_dir: &session_dir,
        task: &task,
        provider_arg: &provider_arg,
        session_id: &session_id,
        resume: resume_session_id.as_deref(),
        permission: permission_mode.as_deref(),
        ollama_base_url: ollama_base_url.as_deref(),
        ollama_mode: ollama_mode.as_deref(),
        ollama_local_model: ollama_local_model.as_deref(),
        ollama_cloud_model: ollama_cloud_model.as_deref(),
        ollama_context_limit_override,
        context_budget_tokens,
    };
    // AUDIT FIX #6: the slot is already reserved (see top of function), so a
    // failure building the spec here must release it explicitly -- unlike
    // spawn() below, these builders have no reservation to release
    // internally.
    let spec_result = if !is_codex {
        build_gui_chat_spec(&inputs, extra).await
    } else {
        build_gui_codex_spec(&inputs, extra, &app_handle, &app_config).await
    };
    let spec = match spec_result {
        Ok(v) => v,
        Err(e) => {
            crate::spawn_supervisor::release_slot().await;
            return Err(e);
        }
    };

    // K1 + RUN-IDENTITY: announce this run's stable identity to the frontend at
    // spawn. run_id doubles as the checkpoint id (K1); session_id lets the App
    // track which session owns the single active run, so a ChatView that
    // remounts on a tab/session/New-Chat switch can re-derive "this run is still
    // running" and re-attach to its event stream instead of orphaning it.
    // Emitted for every spawn shape (chat + codex) so the behavior is uniform;
    // codex specs without KIM_RUN_ID fall back to a spawn-time id.
    let run_id = spec
        .envs
        .iter()
        .find(|(k, _)| k == "KIM_RUN_ID")
        .map(|(_, v)| v.clone())
        .unwrap_or_else(|| task_spec::run_id_for_session(&session_id));
    let _ = app_handle.emit(
        "kim-run-id",
        serde_json::json!({ "run_id": run_id, "session_id": session_id }),
    );

    // Chrome CDP launch whenever a browser provider is in play (Chat tasks
    // and Codex browser-bridge runs) and no in-app bridge is available.
    if is_browser {
        maybe_launch_chrome_for_cdp(&kim_root, bridge_cfg.is_none()).await;
    }

    // AUDIT FIX #6: the runner slot was already reserved at the very top of
    // this function (before any async I/O), so no second reserve_slot()
    // call belongs here anymore -- calling it again would spuriously fail
    // ("already starting") against our own still-held reservation. Hand the
    // whole process lifecycle to the supervisor directly; spawn() consumes
    // the existing reservation and releases it internally on failure.
    let sup = crate::spawn_supervisor::spawn(spec).await?;
    // K7: reflect the running task in the tray status line.
    crate::speed_access::set_tray_status(&app_handle, Some(task.as_str()));
    let ipc_typed = app_config.ipc_protocol == "typed";
    let wait_result = crate::spawn_supervisor::supervise(sup, app_handle.clone(), ipc_typed).await;

    // K7: tray back to idle; restore UI state regardless of exit reason.
    crate::speed_access::set_tray_status(&app_handle, None);
    let _ = set_task_active_mode(app_handle.clone(), false).await;
    if let Some(flash_win) = app_handle.get_webview_window("screenshot-flash") {
        let _ = flash_win.close();
    }

    // M-PROC-6: a wait() IO error must not strand the UI in the running
    // state. Per the documented contract below, the frontend learns about
    // completion/failure via kim-agent-done — emit it even on wait errors.
    let status = match wait_result {
        Ok(status) => status,
        Err(e) => {
            let _ = app_handle.emit("kim-agent-error", format!("Task wait failed: {e}"));
            let _ = app_handle.emit("kim-agent-done", false);
            return Ok("Task ended".to_string());
        }
    };

    if is_codex {
        if let Some(session) = newest_codex_session(&target_root) {
            let _ = app_handle.emit("kim-agent-code-session", session);
        }
    }

    let _ = app_handle.emit("kim-agent-done", status.success());

    // Always return Ok — the frontend learns about failure via the kim-agent-done
    // event (payload = false). Returning Err here causes a second error path in the
    // JS catch block, leading to duplicate error messages and UI state conflicts.
    Ok(if status.success() {
        "Task completed".to_string()
    } else {
        "Task ended".to_string()
    })
}

// ---------------------------------------------------------------------------
// K2 — named spec builders for the GUI spawn path. These resolve the async /
// IO-dependent inputs (interpreter, provider route, OAuth env) and delegate
// the actual argv/env assembly to the pure builders in `task_spec`.
// ---------------------------------------------------------------------------

/// The GUI `send_task` inputs shared by both spec builders.
struct GuiSpawnInputs<'a> {
    kim_root: &'a Path,
    target_root: &'a Path,
    session_dir: &'a Path,
    task: &'a str,
    provider_arg: &'a str,
    session_id: &'a str,
    resume: Option<&'a str>,
    permission: Option<&'a str>,
    ollama_base_url: Option<&'a str>,
    ollama_mode: Option<&'a str>,
    ollama_local_model: Option<&'a str>,
    ollama_cloud_model: Option<&'a str>,
    ollama_context_limit_override: Option<u32>,
    context_budget_tokens: Option<u32>,
}

/// Chat-tab spec: `python -m orchestrator.agent` with the desktop-control
/// persona. Resolves the Google OAuth env (gemini) and Ollama overrides,
/// then delegates to `task_spec::chat_task_spec`.
async fn build_gui_chat_spec(
    p: &GuiSpawnInputs<'_>,
    mut extra: crate::task_spec::EnvBuilder,
) -> Result<crate::task_spec::TaskSpec, String> {
    use crate::task_spec;

    if p.provider_arg.trim().eq_ignore_ascii_case("gemini") {
        let google_env = google_oauth::google_oauth_env_for_agent().await.map_err(|err| {
            format!(
                "Google for Kim is not connected. Open Settings → Account → Google for Kim (API), then Continue with Google. {}",
                err
            )
        })?;
        extra = extra.extend(google_env.as_env_pairs());
    }
    if p.provider_arg.trim().eq_ignore_ascii_case("ollama") {
        extra = extra.ollama(
            p.ollama_base_url,
            p.ollama_mode,
            p.ollama_local_model,
            p.ollama_cloud_model,
            p.ollama_context_limit_override,
        );
    }
    if let Some(budget) = p.context_budget_tokens.filter(|b| *b > 0) {
        extra = extra.set("KIM_CONTEXT_BUDGET_TOKENS", budget.to_string());
    }
    let python = find_python_interpreter(p.kim_root)?;
    preflight_python_deps(&python).await?;
    Ok(task_spec::chat_task_spec(task_spec::ChatSpecParams {
        bundled_sidecar: is_bundled_orchestrator(&python),
        python: &python,
        kim_root: p.kim_root,
        project_root: p.target_root,
        session_dir: p.session_dir,
        task: p.task,
        provider: p.provider_arg,
        session_id: p.session_id.to_string(),
        resume: p.resume.map(str::to_string),
        permission_mode: p.permission,
        source: task_spec::SpawnSource::Gui,
        extra_envs: extra.build(),
    }))
}

/// Code-tab spec: either the Codex browser-bridge (`codex_bridge_service`
/// relaying through Kim's BrowserProvider) or the direct Codex CLI.
async fn build_gui_codex_spec(
    p: &GuiSpawnInputs<'_>,
    extra: crate::task_spec::EnvBuilder,
    app_handle: &tauri::AppHandle,
    app_config: &config::AppConfig,
) -> Result<crate::task_spec::TaskSpec, String> {
    use crate::task_spec;

    // F-G-1: the vendored bundled-Codex/bundled-Claw backends are gone, so the
    // only resolution is CODEX_BIN or `codex` on PATH.
    let codex_bin = find_code_backend(p.kim_root)
        .ok_or_else(|| "Code agent binary not found. Install codex or set CODEX_BIN to the binary path.".to_string())?;

    if task_spec::is_browser_provider(p.provider_arg) {
        // Browser-bridge mode: codex_bridge_service relays each LLM request
        // through Kim's BrowserProvider. No Anthropic key needed.
        let python = find_python_interpreter(p.kim_root)?;
        preflight_python_deps(&python).await?;
        let _ = app_handle.emit(
            "kim-agent-output",
            format!(
                "[STATUS] Routing to Codex via Kim's browser provider ({})",
                p.provider_arg
            ),
        );
        let _ = app_handle.emit(
            "kim-agent-output",
            format!("[STATUS] codex binary: {}", codex_bin.display()),
        );
        let spec = task_spec::codex_browser_spec(task_spec::CodexBridgeSpecParams {
            python: &python,
            kim_root: p.kim_root,
            target_root: p.target_root,
            task: p.task,
            provider: p.provider_arg,
            codex_bin: &codex_bin,
            session_id: p.session_id.to_string(),
            permission_mode: p.permission,
            extra_envs: extra.build(),
        });
        return Ok(spec);
    }

    // Direct API mode: run the Codex CLI itself.
    let route = if p.provider_arg.trim().eq_ignore_ascii_case("ollama") {
        // Codex bypasses its ChatGPT auth entirely via --oss.
        let model = selected_ollama_codex_model(
            p.ollama_mode,
            p.ollama_base_url,
            p.ollama_local_model,
            p.ollama_cloud_model,
            app_config,
        )
        .await?;
        task_spec::ProviderRoute {
            args: vec![
                "--oss".into(),
                "--local-provider".into(),
                "ollama".into(),
                "--model".into(),
                model.clone(),
            ],
            envs: vec![],
            label: format!("Ollama via local daemon ({model})"),
        }
    } else {
        configure_codex_direct_provider(
            p.provider_arg,
            p.kim_root,
            p.ollama_base_url,
            p.ollama_mode,
            p.ollama_local_model,
            p.ollama_cloud_model,
            app_config,
        )
        .await?
    };
    let _ = app_handle.emit(
        "kim-agent-output",
        format!(
            "[STATUS] ✓ Using Codex CLI via {}: {}",
            route.label,
            codex_bin.display(),
        ),
    );
    let spec = task_spec::codex_direct_spec(task_spec::CodexDirectSpecParams {
        code_bin: &codex_bin,
        target_root: p.target_root,
        task: p.task,
        // Gated behind an explicit opt-in env var (#1). Default: sandboxed +
        // approvals enabled.
        bypass_sandbox: std::env::var("KIM_CODEX_BYPASS_SANDBOX").as_deref() == Ok("1"),
        route,
        session_id: p.session_id.to_string(),
    });
    Ok(spec)
}

/// Launch Chrome for CDP if no in-app webview bridge is configured, waiting
/// briefly for the debug port to come up. Best-effort: failures are logged,
/// never fatal.
async fn maybe_launch_chrome_for_cdp(kim_root: &Path, no_bridge: bool) {
    if !no_bridge {
        eprintln!("[Kim] Browser provider using in-app bridge (no Chrome CDP launch)");
        return;
    }
    let root_for_chrome = kim_root.to_path_buf();
    match tokio::task::spawn_blocking(move || launch_chrome_for_cdp(&root_for_chrome)).await {
        Ok(Ok(true)) => {
            tokio::time::sleep(Duration::from_secs(2)).await;
        }
        Ok(Ok(false)) => {}
        Ok(Err(e)) => {
            eprintln!("[Kim] Chrome launch skipped: {}", e);
        }
        Err(e) => {
            eprintln!("[Kim] Chrome launch task panicked: {}", e);
        }
    }
}

// ---------------------------------------------------------------------------
// Cancel a running task — SIGTERM, then SIGKILL after 2s if still alive.
// ---------------------------------------------------------------------------

#[tauri::command]
pub(crate) async fn cancel_task(app_handle: tauri::AppHandle) -> Result<String, String> {
    let (pid, starting) = {
        let rt = crate::task_runtime::task_runtime().lock().await;
        (rt.pid, rt.starting)
    };

    let Some(pid) = pid else {
        if starting {
            return Err("Task is still starting. Try stopping again in a moment.".to_string());
        }
        return Err("No task is currently running.".to_string());
    };

    // Step 1 — graceful signal.
    send_signal(pid, false).map_err(|e| format!("Failed to send stop signal: {}", e))?;

    // Restore the UI immediately. If the agent is cancelled while a screenshot
    // tool is blocking, Python may never reach its finally block that normally
    // shows the main window again.
    let _ = set_task_active_mode(app_handle.clone(), false).await;
    if let Some(flash_win) = app_handle.get_webview_window("screenshot-flash") {
        let _ = flash_win.close();
    }

    // Step 2 — wait up to 2 seconds for the process to exit; if it's still
    // alive, force kill. We poll by re-sending signal 0 (existence check).
    let app = app_handle.clone();
    tokio::spawn(async move {
        for _ in 0..20 {
            tokio::time::sleep(Duration::from_millis(100)).await;
            if !process_exists(pid) {
                // #24: pid-guarded — a new task may already occupy the slot.
                let mut rt = crate::task_runtime::task_runtime().lock().await;
                rt.clear_if_pid(pid);
                let _ = app.emit("kim-agent-cancelled", true);
                return;
            }
        }
        // Still alive after 2s → SIGKILL / taskkill /F.
        // M-PROC-3: re-check that the runtime still tracks THIS pid before
        // escalating. If the child exited and the OS reused the pid (or a new
        // task already occupies the slot), a blind group-SIGKILL would hit an
        // unrelated process tree.
        let mut rt = crate::task_runtime::task_runtime().lock().await;
        if rt.pid == Some(pid) {
            let _ = send_signal(pid, true);
            rt.clear_if_pid(pid); // #24: pid-guarded
        }
        drop(rt);
        let _ = app.emit("kim-agent-cancelled", true);
    });

    Ok("Cancelling task…".to_string())
}

// ── Platform-specific signalling ─────────────────────────────────────────────

#[cfg(unix)]
pub(crate) fn send_signal(pid: u32, force: bool) -> std::io::Result<()> {
    // M-PROC-5: direct kill(2) syscalls instead of spawning a `kill` process —
    // cancel pollers call this every 100ms, some while holding the runtime lock.
    let sig = if force { libc::SIGKILL } else { libc::SIGTERM };
    // Kill the process group (-pid) so child processes are also terminated (#64).
    // If group kill fails (e.g. the process is not a group leader), fall back
    // to killing only the parent process.
    let group_ok = unsafe { libc::kill(-(pid as i32), sig) } == 0;
    if group_ok {
        return Ok(());
    }
    if unsafe { libc::kill(pid as i32, sig) } == 0 {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error())
    }
}

#[cfg(unix)]
pub(crate) fn process_exists(pid: u32) -> bool {
    // M-PROC-5: kill(pid, 0) syscall instead of spawning a `kill` process.
    // Callers only ever pass pids of OUR children (which we can always
    // signal), so EPERM can only mean the pid was reused by a foreign
    // process after our child died — report that as "dead" so slot recovery
    // still works. SIGKILL escalation paths additionally re-check identity
    // against the runtime before signalling (M-PROC-3), so a reused pid is
    // never group-killed.
    unsafe { libc::kill(pid as i32, 0) == 0 }
}

#[cfg(windows)]
pub(crate) fn send_signal(pid: u32, force: bool) -> std::io::Result<()> {
    use std::process::Command;
    // Windows has no SIGTERM; use taskkill. /T kills the process tree so
    // the Python interpreter and any child processes it spawned go too.
    let mut cmd = Command::new("taskkill");
    cmd.args(["/PID", &pid.to_string(), "/T"]);
    if force {
        cmd.arg("/F");
    }
    let status = cmd.status()?;
    if !status.success() {
        return Err(std::io::Error::other(format!(
            "taskkill failed with {}",
            status
        )));
    }
    Ok(())
}

#[cfg(windows)]
pub(crate) fn process_exists(pid: u32) -> bool {
    use std::process::Command;
    // `tasklist /FI "PID eq N"` prints a header only when there's no match.
    match Command::new("tasklist")
        .args(["/FI", &format!("PID eq {}", pid), "/NH"])
        .output()
    {
        Ok(out) => {
            let s = String::from_utf8_lossy(&out.stdout);
            s.contains(&pid.to_string())
        }
        Err(_) => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    // -- F-D-5: bounded stdout line splitter --

    fn split_all(cap: usize, chunks: &[&[u8]]) -> Vec<String> {
        let mut s = CappedLineSplitter::new(cap);
        let mut out = Vec::new();
        for c in chunks {
            s.push(c, |line| out.push(line));
        }
        s.finish(|line| out.push(line));
        out
    }

    #[test]
    fn splitter_yields_lines_like_bufreader() {
        // Multiple lines in one chunk, split across chunk boundaries, CRLF, and
        // a trailing unterminated line all behave like BufReader::lines().
        let out = split_all(1024, &[b"alpha\nbeta\r\n", b"gam", b"ma\ndelta"]);
        assert_eq!(out, vec!["alpha", "beta", "gamma", "delta"]);
    }

    #[test]
    fn splitter_empty_lines_preserved() {
        let out = split_all(1024, &[b"\n\nx\n"]);
        assert_eq!(out, vec!["", "", "x"]);
    }

    #[test]
    fn splitter_truncates_oversized_line() {
        // A single line far larger than the cap is truncated to ~cap bytes with
        // a marker — never buffered whole.
        let cap = 16;
        let big = vec![b'A'; 10_000];
        let mut chunks: Vec<&[u8]> = vec![&big];
        let nl = b"\ntail\n";
        chunks.push(nl);
        let out = split_all(cap, &chunks);
        assert_eq!(out.len(), 2);
        assert!(out[0].starts_with("AAAA"), "got {:?}", out[0]);
        assert!(out[0].contains("truncated"), "expected marker: {:?}", out[0]);
        // The capped prefix stays within cap + the marker (no 10k buffering).
        assert!(out[0].len() < cap + 64);
        // The line AFTER the oversized one is unaffected.
        assert_eq!(out[1], "tail");
    }

    #[test]
    fn splitter_no_newline_flood_is_bounded_at_eof() {
        // A child that emits a huge run with no newline at all still yields a
        // single bounded, truncated line at EOF (finish()).
        let cap = 8;
        let big = vec![b'x'; 5000];
        let out = split_all(cap, &[&big]);
        assert_eq!(out.len(), 1);
        assert!(out[0].contains("truncated"));
        assert!(out[0].len() < cap + 64);
    }

    // -- F-H-3: undecodable chat stdout is forwarded (not silently dropped) --

    #[test]
    fn chat_decode_failure_is_forwarded_not_dropped() {
        // Regression for the finding: pre-fix, a CHAT run (is_codex == false)
        // whose stdout line failed KimEvent decode was SILENTLY DROPPED. It must
        // now route to ForwardRaw so version-skew / partial-JSON / stray-print
        // lines reach the UI instead of vanishing — and be counted.
        let mut lim = ProtocolErrorLimiter::for_stdout();
        assert_eq!(
            route_decode_failure(false, &mut lim, 0),
            DecodeFailureAction::ForwardRaw
        );
        assert_eq!(lim.total_failures(), 1);
        assert_eq!(lim.suppressed(), 0);
    }

    #[test]
    fn codex_decode_failure_forwards_unbounded_and_off_chat_budget() {
        // Preserve the existing codex raw-emit behavior: codex always forwards,
        // is never rate-limited, and never consumes the chat token budget.
        let mut lim = ProtocolErrorLimiter::new(1, 1_000_000);
        for _ in 0..1000 {
            assert_eq!(
                route_decode_failure(true, &mut lim, 0),
                DecodeFailureAction::ForwardRaw
            );
        }
        assert_eq!(
            lim.total_failures(),
            0,
            "codex passthrough must not touch the chat decode-failure counter"
        );
        assert_eq!(lim.suppressed(), 0);
    }

    #[test]
    fn chat_flood_is_rate_limited_but_every_failure_counted() {
        // A burst up to capacity is forwarded; the flood beyond it is Suppressed
        // (withheld from the UI) yet still counted — nothing is silently lost.
        let mut lim = ProtocolErrorLimiter::new(3, 1000);
        for _ in 0..3 {
            assert_eq!(
                route_decode_failure(false, &mut lim, 0),
                DecodeFailureAction::ForwardRaw
            );
        }
        // 4th and 5th within the same instant have no token → suppressed.
        assert_eq!(
            route_decode_failure(false, &mut lim, 0),
            DecodeFailureAction::Suppress
        );
        assert_eq!(
            route_decode_failure(false, &mut lim, 0),
            DecodeFailureAction::Suppress
        );
        assert_eq!(lim.total_failures(), 5, "every decode failure is counted");
        assert_eq!(lim.suppressed(), 2, "the two over-budget lines are counted");
    }

    #[test]
    fn chat_budget_refills_over_time_and_is_capped() {
        let mut lim = ProtocolErrorLimiter::new(2, 500);
        // Drain the burst at t=0.
        assert!(lim.record_at(0));
        assert!(lim.record_at(0));
        assert!(!lim.record_at(0));
        // One token back after one refill interval.
        assert!(lim.record_at(500));
        assert!(!lim.record_at(500));
        // A long idle gap refills to capacity but NOT beyond (no unbounded
        // accrual → the next burst is still bounded by capacity).
        assert!(lim.record_at(100_000));
        assert!(lim.record_at(100_000));
        assert!(!lim.record_at(100_000));
    }

    #[test]
    fn limiter_never_refills_when_interval_zero() {
        // Defensive: a zero refill interval must not divide-by-zero; the bucket
        // simply never refills.
        let mut lim = ProtocolErrorLimiter::new(1, 0);
        assert!(lim.record_at(0));
        assert!(!lim.record_at(1_000_000));
        assert_eq!(lim.suppressed(), 1);
    }

    #[test]
    fn bare_system_python_is_classified() {
        // F-L-2: only bare command names (no path separator) are the
        // dependency-risk system fallback.
        assert!(is_bare_system_python("python3"));
        assert!(is_bare_system_python("python"));
        assert!(is_bare_system_python("py"));
        // Venv / absolute interpreters and the bundled sidecar are NOT bare.
        assert!(!is_bare_system_python("/proj/venv/bin/python"));
        assert!(!is_bare_system_python("/Users/x/.kim/venv/bin/python"));
        assert!(!is_bare_system_python(r"C:\proj\venv\Scripts\python.exe"));
        assert!(!is_bare_system_python(
            "/Applications/Kim.app/Contents/MacOS/kim-orchestrator"
        ));
    }

    #[tokio::test]
    async fn preflight_skips_venv_interpreters() {
        // A resolved venv/absolute interpreter never triggers the probe, so this
        // returns Ok even though the path does not exist.
        assert!(preflight_python_deps("/nonexistent/venv/bin/python")
            .await
            .is_ok());
    }

    #[tokio::test]
    async fn preflight_bare_python_missing_deps_is_friendly() {
        // A bare command that cannot import Kim's deps yields the actionable
        // message rather than a raw ModuleNotFoundError. Use a command name that
        // does not exist so the probe fails deterministically.
        let err = preflight_python_deps("kim-nonexistent-python-xyz")
            .await
            .unwrap_err();
        assert_eq!(err, PYTHON_DEPS_MISSING_MESSAGE);
        assert!(err.contains("install.sh"));
    }

    #[test]
    fn install_venv_candidates_drop_dead_kim_root_arm() {
        // F-L-9: the interpreter search must not probe ~/.kim_root as a
        // directory — install.sh writes it as a FILE, so those candidates
        // could never match. Only ~/.kim venvs remain.
        let home = Path::new("/home/tester");
        let cands = install_script_venv_candidates(home);
        for c in &cands {
            let s = c.to_string_lossy();
            assert!(
                !s.contains(".kim_root"),
                "dead ~/.kim_root venv candidate must be gone: {s}"
            );
            assert!(s.contains("/.kim/"), "expected a ~/.kim venv candidate: {s}");
        }
        assert_eq!(cands.len(), 2);
    }

    #[test]
    fn merge_run_envelope_reattaches_run_and_session() {
        // The typed enum drops the envelope on decode; the forwarder must copy
        // it back onto the curated payload so the frontend can route by run.
        let raw = r#"{"type":"status","message":"hi","run_id":"sessA-9","session_id":"sessA"}"#;
        let curated = serde_json::json!({"message": "hi"});
        let out = merge_run_envelope(raw, curated);
        assert_eq!(out["run_id"], "sessA-9");
        assert_eq!(out["session_id"], "sessA");
        assert_eq!(out["message"], "hi");
    }

    #[test]
    fn merge_run_envelope_is_noop_without_ids() {
        // Legacy / bridge streams carry no envelope: payload must be untouched.
        let raw = r#"{"type":"answer","text":"done"}"#;
        let curated = serde_json::json!({"text": "done"});
        let out = merge_run_envelope(raw, curated);
        assert!(out.get("run_id").is_none());
        assert!(out.get("session_id").is_none());
        assert_eq!(out["text"], "done");
    }

    #[test]
    fn merge_run_envelope_stamps_only_present_id() {
        let raw = r#"{"type":"status","message":"hi","session_id":"sessB"}"#;
        let out = merge_run_envelope(raw, serde_json::json!({"message": "hi"}));
        assert_eq!(out["session_id"], "sessB");
        assert!(out.get("run_id").is_none());
    }

    #[test]
    fn merge_run_envelope_does_not_override_existing_field() {
        // Defensive: a real payload field named run_id must win over the envelope.
        let raw = r#"{"type":"x","run_id":"envelope"}"#;
        let out = merge_run_envelope(raw, serde_json::json!({"run_id": "payload"}));
        assert_eq!(out["run_id"], "payload");
    }

    #[test]
    fn test_parse_run_done_event() {
        let json = r#"{"type":"run_done","termination":"task_complete","success":true}"#;
        let event: KimEvent = serde_json::from_str(json).expect("run_done should deserialize");
        match event {
            KimEvent::RunDone {
                termination,
                success,
            } => {
                assert_eq!(termination, "task_complete");
                assert!(success);
            }
            _ => panic!("Expected RunDone, got {:?}", event),
        }
    }

    #[test]
    fn test_parse_run_done_failed_variant() {
        let json = r#"{"type":"run_done","termination":"max_iterations","success":false}"#;
        let event: KimEvent =
            serde_json::from_str(json).expect("run_done failed should deserialize");
        match event {
            KimEvent::RunDone {
                termination,
                success,
            } => {
                assert_eq!(termination, "max_iterations");
                assert!(!success);
            }
            _ => panic!("Expected RunDone"),
        }
    }

    #[test]
    fn test_parse_provider_error_event() {
        let json = r#"{"type":"provider_error","code":"rate_limit","retryable":false}"#;
        let event: KimEvent =
            serde_json::from_str(json).expect("provider_error should deserialize");
        match event {
            KimEvent::ProviderError { code, retryable } => {
                assert_eq!(code, "rate_limit");
                assert!(!retryable);
            }
            _ => panic!("Expected ProviderError, got {:?}", event),
        }
    }

    #[test]
    fn test_parse_provider_error_retryable_variant() {
        let json = r#"{"type":"provider_error","code":"server_error","retryable":true}"#;
        let event: KimEvent =
            serde_json::from_str(json).expect("provider_error retryable should deserialize");
        match event {
            KimEvent::ProviderError { code, retryable } => {
                assert_eq!(code, "server_error");
                assert!(retryable);
            }
            _ => panic!("Expected ProviderError"),
        }
    }

    #[test]
    fn test_parse_run_done_all_terminations() {
        let terminations = [
            "task_complete",
            "cancelled",
            "max_iterations",
            "stuck",
            "provider_failed",
            "need_help",
            "conversational_loop",
        ];
        for term in &terminations {
            let json = format!(
                r#"{{"type":"run_done","termination":"{}","success":false}}"#,
                term
            );
            let event: KimEvent = serde_json::from_str(&json)
                .unwrap_or_else(|e| panic!("run_done with termination={} failed: {}", term, e));
            match event {
                KimEvent::RunDone { termination, .. } => assert_eq!(&termination, term),
                _ => panic!("Expected RunDone for termination={}", term),
            }
        }
    }

    #[test]
    fn test_parse_hitl_approval_request_event() {
        let json = r#"{"type":"hitl_approval_request","tool":"run_command","risk":"high","reason":"arbitrary_code_execution"}"#;
        let event: KimEvent =
            serde_json::from_str(json).expect("hitl_approval_request should deserialize");
        match event {
            KimEvent::HitlApprovalRequest {
                tool,
                risk,
                reason,
                preview,
                id,
            } => {
                assert_eq!(tool, "run_command");
                assert_eq!(risk, "high");
                assert_eq!(reason, "arbitrary_code_execution");
                assert_eq!(preview, ""); // absent in legacy line → default
                assert_eq!(id, ""); // absent in legacy line → default
            }
            _ => panic!("Expected HitlApprovalRequest, got {:?}", event),
        }
    }

    #[test]
    fn test_parse_hitl_approval_result_approved_event() {
        let json = r#"{"type":"hitl_approval_result","tool":"run_command","approved":true}"#;
        let event: KimEvent =
            serde_json::from_str(json).expect("hitl_approval_result should deserialize");
        match event {
            KimEvent::HitlApprovalResult { tool, approved } => {
                assert_eq!(tool, "run_command");
                assert!(approved);
            }
            _ => panic!("Expected HitlApprovalResult, got {:?}", event),
        }
    }

    #[test]
    fn test_parse_hitl_approval_result_denied_event() {
        let json = r#"{"type":"hitl_approval_result","tool":"delete_file","approved":false}"#;
        let event: KimEvent =
            serde_json::from_str(json).expect("hitl_approval_result denied should deserialize");
        match event {
            KimEvent::HitlApprovalResult { tool, approved } => {
                assert_eq!(tool, "delete_file");
                assert!(!approved);
            }
            _ => panic!("Expected HitlApprovalResult, got {:?}", event),
        }
    }

    #[test]
    fn test_parse_generated_activity_events() {
        let cases = [
            r#"{"type":"tool","name":"read_file","args":{}}"#,
            r#"{"type":"answer","text":"done"}"#,
            r#"{"type":"diff","path":"main.rs","added":2,"removed":1}"#,
            r#"{"type":"activity","kind":"error","text":"failed"}"#,
        ];
        for json in cases {
            serde_json::from_str::<KimEvent>(json)
                .unwrap_or_else(|error| panic!("generated event failed to decode: {error}"));
        }
    }

    #[test]
    fn test_find_python_finds_venv() {
        let tmp = tempfile::tempdir().expect("failed to create temp dir");
        let root = tmp.path();

        // Create a fake venv/bin/python
        let venv_bin = root.join("venv").join("bin");
        fs::create_dir_all(&venv_bin).unwrap();
        let python_path = venv_bin.join("python");
        fs::write(&python_path, "#!/bin/sh\n").unwrap();

        // Make it "exist" (the function only checks .exists(), not executability)
        let result = find_python_interpreter(root);
        assert!(result.is_ok(), "Expected Ok, got {:?}", result);
        let found = result.unwrap();
        assert!(found.contains("venv"), "Expected venv path, got: {}", found);
    }

    #[test]
    fn test_find_python_finds_dot_venv() {
        let tmp = tempfile::tempdir().expect("failed to create temp dir");
        let root = tmp.path();

        // Create a fake .venv/bin/python (no venv/)
        let venv_bin = root.join(".venv").join("bin");
        fs::create_dir_all(&venv_bin).unwrap();
        let python_path = venv_bin.join("python");
        fs::write(&python_path, "#!/bin/sh\n").unwrap();

        let result = find_python_interpreter(root);
        assert!(result.is_ok());
        let found = result.unwrap();
        assert!(
            found.contains(".venv"),
            "Expected .venv path, got: {}",
            found
        );
    }

    #[test]
    fn test_find_python_prefers_venv_over_dot_venv() {
        let tmp = tempfile::tempdir().expect("failed to create temp dir");
        let root = tmp.path();

        // Create both venv/ and .venv/
        for dir_name in &["venv", ".venv"] {
            let venv_bin = root.join(dir_name).join("bin");
            fs::create_dir_all(&venv_bin).unwrap();
            fs::write(venv_bin.join("python"), "#!/bin/sh\n").unwrap();
        }

        let result = find_python_interpreter(root);
        assert!(result.is_ok());
        let found = result.unwrap();
        // venv/ comes first in the candidate list
        let found_path = PathBuf::from(&found);
        assert!(
            found_path.starts_with(root.join("venv"))
                && !found_path.starts_with(root.join(".venv")),
            "Expected venv/ to be preferred over .venv/, got: {}",
            found
        );
    }

    #[test]
    fn test_find_python_fallback_to_system() {
        let tmp = tempfile::tempdir().expect("failed to create temp dir");
        let root = tmp.path();
        // No venv directories — should fall back to system python3/python

        let result = find_python_interpreter(root);
        // On a system with Python installed this should succeed;
        // on CI without Python it may fail — both are valid.
        // We just verify it doesn't panic.
        match result {
            Ok(cmd) => assert!(!cmd.is_empty()),
            Err(msg) => assert!(msg.contains("No Python interpreter")),
        }
    }

    #[test]
    fn test_is_bundled_orchestrator_positive() {
        assert!(is_bundled_orchestrator(
            "/path/to/kim-orchestrator-aarch64-apple-darwin"
        ));
        assert!(is_bundled_orchestrator(
            "/Applications/Kim.app/Contents/MacOS/kim-orchestrator"
        ));
        assert!(is_bundled_orchestrator("kim-orchestrator"));
    }

    #[test]
    fn test_is_bundled_orchestrator_negative() {
        assert!(!is_bundled_orchestrator("/usr/bin/python3"));
        assert!(!is_bundled_orchestrator("python"));
        assert!(!is_bundled_orchestrator("/path/venv/bin/python"));
    }

    // ── Regression: bundled_detection_by_basename ──────────────────────────────
    // is_bundled_orchestrator must return true for every expected basename variant
    // of the sidecar and false for the python interpreter names that can appear
    // on PATH (python3 is the default system interpreter on macOS/Linux).
    #[test]
    fn bundled_detection_by_basename() {
        // Bare sidecar name (development copy placed next to executable).
        assert!(is_bundled_orchestrator("kim-orchestrator"));
        // The Tauri bundle convention appends the platform triple.
        assert!(is_bundled_orchestrator(
            "kim-orchestrator-aarch64-apple-darwin"
        ));
        assert!(is_bundled_orchestrator(
            "kim-orchestrator-x86_64-unknown-linux-gnu"
        ));
        assert!(is_bundled_orchestrator(
            "kim-orchestrator-x86_64-pc-windows-msvc"
        ));
        // Any string that embeds the sidecar name is treated as bundled —
        // this is intentional per the `contains` implementation.
        assert!(is_bundled_orchestrator("prefix-kim-orchestrator-suffix"));

        // Python interpreter names must never be treated as bundled.
        assert!(!is_bundled_orchestrator("python3"));
        assert!(!is_bundled_orchestrator("python"));
        assert!(!is_bundled_orchestrator("py"));
        // Unrelated orchestration tools must not match.
        assert!(!is_bundled_orchestrator("orchestrator"));
        assert!(!is_bundled_orchestrator("kim-agent"));
    }

    // ── Regression: bundled_detection_with_path_and_suffix ─────────────────────
    // Detection must work when the sidecar is referenced via an absolute path and
    // when the binary carries a .exe extension (Windows sidecar convention).
    #[test]
    fn bundled_detection_with_path_and_suffix() {
        // Absolute path inside a macOS .app bundle.
        assert!(is_bundled_orchestrator(
            "/Applications/Kim.app/Contents/MacOS/kim-orchestrator"
        ));
        // Absolute path with Tauri platform-triple suffix (macOS arm64).
        assert!(is_bundled_orchestrator(
            "/Applications/Kim.app/Contents/MacOS/kim-orchestrator-aarch64-apple-darwin"
        ));
        // Absolute path on Linux.
        assert!(is_bundled_orchestrator(
            "/usr/local/lib/kim/kim-orchestrator-x86_64-unknown-linux-gnu"
        ));
        // Windows path — bare name with .exe extension.
        assert!(is_bundled_orchestrator(
            r"C:\Program Files\Kim\kim-orchestrator.exe"
        ));
        // Windows path — platform-triple + .exe extension.
        assert!(is_bundled_orchestrator(
            r"C:\Program Files\Kim\kim-orchestrator-x86_64-pc-windows-msvc.exe"
        ));
        // The .exe suffix alone (without "kim-orchestrator") must NOT match.
        assert!(!is_bundled_orchestrator("python.exe"));
        assert!(!is_bundled_orchestrator(r"C:\Python312\python.exe"));
    }

    // ── Regression: python_paths_not_bundled ───────────────────────────────────
    // Any Python interpreter path returned by find_python_interpreter() in the
    // non-sidecar code paths must NOT be classified as the bundled orchestrator.
    #[test]
    fn python_paths_not_bundled() {
        // Project-local venv paths (both Unix and Windows layouts).
        assert!(!is_bundled_orchestrator(
            "/workspace/project/venv/bin/python"
        ));
        assert!(!is_bundled_orchestrator(
            "/workspace/project/.venv/bin/python"
        ));
        assert!(!is_bundled_orchestrator(
            r"C:\workspace\project\venv\Scripts\python.exe"
        ));
        assert!(!is_bundled_orchestrator(
            r"C:\workspace\project\.venv\Scripts\python.exe"
        ));
        // Kim install-script home venv paths.
        assert!(!is_bundled_orchestrator(
            "/home/user/.kim_root/venv/bin/python"
        ));
        assert!(!is_bundled_orchestrator("/home/user/.kim/venv/bin/python"));
        // Bare names as returned by system PATH fallback.
        assert!(!is_bundled_orchestrator("python"));
        assert!(!is_bundled_orchestrator("python3"));
        // System-installed interpreters.
        assert!(!is_bundled_orchestrator("/usr/bin/python3"));
        assert!(!is_bundled_orchestrator("/usr/local/bin/python3"));
    }

    #[test]
    fn test_find_bundled_orchestrator_absent_in_test() {
        // In the test binary context, there is no kim-orchestrator sidecar
        // adjacent to the test executable. find_bundled_orchestrator() must
        // return None rather than panicking.
        let result = find_bundled_orchestrator();
        // Either None (no sidecar found) or Some (if by coincidence a binary
        // named kim-orchestrator exists in the test dir) — both are valid.
        // This test just asserts the function doesn't panic.
        let _ = result;
    }

    #[test]
    fn test_dirs_home_returns_something() {
        // HOME or USERPROFILE should be set in any normal environment.
        let home = dirs_home();
        assert!(home.is_some(), "dirs_home() returned None — HOME not set?");
    }
}

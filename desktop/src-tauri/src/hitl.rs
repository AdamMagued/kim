//! Human-in-the-loop approval plumbing (K1).
//!
//! The approval GATE itself now lives server-side in `mcp_server` (policy +
//! approval broker); this module only carries the decision from the UI back to
//! the running agent's stdin, where Python's `StdinApprovalBridge` reads it.
//!
//! Extracted from `subprocess.rs` so the K1 approval-vocabulary work does not
//! regrow that oversized module ahead of its scheduled K2 decomposition.

/// Build the `hitl_approve` JSON stdin line from a UI decision.
///
/// K1 vocabulary: `decision` is `accept` | `acceptForSession` | `decline`;
/// `id` optionally correlates the reply with a specific approval request.
/// Both are optional so frontends that only send `approved` keep working, and
/// `approved` is always emitted for Python-side back-compat.
pub(crate) fn build_hitl_approve_line(
    approved: bool,
    decision: Option<String>,
    id: Option<String>,
) -> String {
    let decision =
        decision.unwrap_or_else(|| if approved { "accept" } else { "decline" }.to_string());
    // L-PROC-8: whitelist the decision vocabulary like build_approval_decision_line
    // does — an unknown string can never be forwarded (and never approve).
    let decision = match decision.as_str() {
        "accept" | "acceptForSession" | "decline" => decision,
        _ => "decline".to_string(),
    };
    let approved = matches!(decision.as_str(), "accept" | "acceptForSession");
    let mut payload = serde_json::json!({
        "type": "hitl_approve",
        "approved": approved,
        "decision": decision,
    });
    if let Some(id) = id {
        payload["id"] = serde_json::Value::String(id);
    }
    payload.to_string()
}

/// Parse a `/v1/task/approve` JSON body into the stdin line to forward.
/// Returns a user-facing error string on malformed JSON (the caller maps it
/// to a 400 response).
pub(crate) fn approve_line_from_body(body: &str) -> Result<String, String> {
    #[derive(serde::Deserialize)]
    struct ApproveRequest {
        approved: bool,
        // K1 vocabulary: accept | acceptForSession | decline.
        #[serde(default)]
        decision: Option<String>,
        #[serde(default)]
        id: Option<String>,
    }
    let parsed: ApproveRequest =
        serde_json::from_str(body).map_err(|e| format!("Invalid JSON: {e}"))?;
    Ok(format!(
        "{}\n",
        build_hitl_approve_line(parsed.approved, parsed.decision, parsed.id)
    ))
}

/// Write the user's HITL approval decision to the running agent's stdin.
/// Python's `StdinApprovalBridge` reads this line to unblock the (now
/// server-side) approval gate.
#[tauri::command]
pub(crate) async fn hitl_respond_approval(
    approved: bool,
    decision: Option<String>,
    id: Option<String>,
) -> Result<(), String> {
    let msg = format!("{}\n", build_hitl_approve_line(approved, decision, id));
    crate::task_runtime::write_stdin_line(&msg).await
}

/// Build the `approval_decision` stdin line for a NATIVE codex approval
/// (app-server transport, parity Part 3/5). `decision` is
/// `accept` | `acceptForSession` | `decline`; anything unrecognized is
/// downgraded to `decline` so a UI bug can never accidentally approve.
pub(crate) fn build_approval_decision_line(id: &str, decision: &str) -> String {
    let decision = match decision {
        "accept" | "acceptForSession" | "decline" => decision,
        _ => "decline",
    };
    serde_json::json!({
        "type": "approval_decision",
        "id": id,
        "decision": decision,
    })
    .to_string()
}

/// Answer a native codex command / file-change approval request
/// (`kim:command-approval-request` / `kim:file-change-approval-request`).
/// The codex bridge service blocks its turn on this stdin line.
#[tauri::command]
pub(crate) async fn respond_approval_decision(id: String, decision: String) -> Result<(), String> {
    let msg = format!("{}\n", build_approval_decision_line(&id, &decision));
    crate::task_runtime::write_stdin_line(&msg).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_hitl_line_derives_decision_from_approved() {
        let line = build_hitl_approve_line(true, None, None);
        let v: serde_json::Value = serde_json::from_str(&line).unwrap();
        assert_eq!(v["type"], "hitl_approve");
        assert_eq!(v["approved"], true);
        assert_eq!(v["decision"], "accept");

        let line = build_hitl_approve_line(false, None, None);
        let v: serde_json::Value = serde_json::from_str(&line).unwrap();
        assert_eq!(v["approved"], false);
        assert_eq!(v["decision"], "decline");
    }

    #[test]
    fn test_hitl_line_accept_for_session_sets_approved_true() {
        let line = build_hitl_approve_line(false, Some("acceptForSession".into()), None);
        let v: serde_json::Value = serde_json::from_str(&line).unwrap();
        // decision wins over the raw bool so legacy Python readers stay correct
        assert_eq!(v["approved"], true);
        assert_eq!(v["decision"], "acceptForSession");
    }

    #[test]
    fn test_hitl_line_decline_decision_overrides_approved_bool() {
        let line = build_hitl_approve_line(true, Some("decline".into()), None);
        let v: serde_json::Value = serde_json::from_str(&line).unwrap();
        assert_eq!(v["approved"], false);
        assert_eq!(v["decision"], "decline");
    }

    #[test]
    fn test_hitl_line_includes_request_id_when_given() {
        let line = build_hitl_approve_line(true, Some("accept".into()), Some("req-42".into()));
        let v: serde_json::Value = serde_json::from_str(&line).unwrap();
        assert_eq!(v["id"], "req-42");
    }

    #[test]
    fn test_approval_decision_line_vocabulary() {
        for decision in ["accept", "acceptForSession", "decline"] {
            let line = build_approval_decision_line("7", decision);
            let v: serde_json::Value = serde_json::from_str(&line).unwrap();
            assert_eq!(v["type"], "approval_decision");
            assert_eq!(v["id"], "7");
            assert_eq!(v["decision"], decision);
        }
    }

    // L-PROC-8: an arbitrary decision string must never be forwarded raw to
    // the Python stdin bridge — unknown vocabulary is downgraded to decline.
    #[test]
    fn test_hitl_line_unknown_decision_sanitized_to_decline() {
        let line = build_hitl_approve_line(true, Some("yesplease\"}\n{".into()), None);
        let v: serde_json::Value = serde_json::from_str(&line).unwrap();
        assert_eq!(v["decision"], "decline");
        assert_eq!(v["approved"], false);
    }

    #[test]
    fn test_approval_decision_line_unknown_decision_declines() {
        let line = build_approval_decision_line("7", "yolo");
        let v: serde_json::Value = serde_json::from_str(&line).unwrap();
        assert_eq!(v["decision"], "decline");
    }
}

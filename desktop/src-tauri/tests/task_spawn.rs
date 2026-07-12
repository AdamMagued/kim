//! T2 — integration tests for the K2 `TaskSpec` decomposition.
//!
//! These run against the public `desktop_lib::task_spec` seam and prove two
//! things the unit tests can't:
//!
//! 1. The GUI and `/v1/task` bridge paths, both built through
//!    `chat_task_spec`, produce argv/env that CANNOT diverge on the shared
//!    contract (module flag, HITL threshold vocabulary, run-id, PYTHONPATH).
//! 2. A `TaskSpec` is directly executable: spawning `program`+`args`+`envs`
//!    with a fake recorder binary reproduces exactly the argv and env the
//!    spec declared (the "fake binary records reality" harness pattern).

use desktop_lib::task_spec::{
    chat_task_spec, codex_browser_spec, codex_direct_spec, promote_provider, ChatSpecParams,
    CodexBridgeSpecParams, CodexDirectSpecParams, ProviderRoute, SpawnSource, StdinMode, TaskSpec,
};
use std::path::Path;

fn env_of<'a>(spec: &'a TaskSpec, key: &str) -> Option<&'a str> {
    spec.envs
        .iter()
        .find(|(k, _)| k == key)
        .map(|(_, v)| v.as_str())
}

fn gui_spec(permission: Option<&str>) -> TaskSpec {
    chat_task_spec(ChatSpecParams {
        python: "/venv/bin/python",
        bundled_sidecar: false,
        kim_root: Path::new("/kim"),
        project_root: Path::new("/proj"),
        session_dir: Path::new("/kim/kim_sessions"),
        task: "hello",
        provider: "browser",
        session_id: "sess".into(),
        resume: Some("sess".into()),
        permission_mode: permission,
        source: SpawnSource::Gui,
        extra_envs: vec![],
    })
}

fn bridge_spec(permission: Option<&str>) -> TaskSpec {
    chat_task_spec(ChatSpecParams {
        python: "/venv/bin/python",
        bundled_sidecar: false,
        kim_root: Path::new("/kim"),
        project_root: Path::new("/kim"),
        session_dir: Path::new("/kim/kim_sessions"),
        task: "hello",
        provider: "browser",
        session_id: "sess".into(),
        resume: Some("sess".into()),
        permission_mode: permission,
        source: SpawnSource::Bridge,
        extra_envs: vec![],
    })
}

// ---------------------------------------------------------------------------
// A1/A3 — the two spawn paths share one contract by construction.
// ---------------------------------------------------------------------------

#[test]
fn gui_and_bridge_chat_specs_share_argv_and_hitl_contract() {
    for mode in [
        None,
        Some("ask_risky"),
        Some("ask_always"),
        Some("full_auto"),
    ] {
        let gui = gui_spec(mode);
        let bridge = bridge_spec(mode);
        // Identical argv (PROJECT_ROOT is the only intended env difference).
        assert_eq!(gui.args, bridge.args, "argv diverged for mode={mode:?}");
        assert_eq!(
            env_of(&gui, "KIM_HITL_RISK_THRESHOLD"),
            env_of(&bridge, "KIM_HITL_RISK_THRESHOLD"),
            "HITL threshold diverged for mode={mode:?}"
        );
        assert_eq!(gui.stdin, StdinMode::Piped);
        assert_eq!(bridge.stdin, StdinMode::Piped);
        for spec in [&gui, &bridge] {
            assert_eq!(env_of(spec, "PYTHONPATH"), Some("/kim"));
            assert_eq!(env_of(spec, "KIM_TAURI_MODE"), Some("1"));
            assert!(env_of(spec, "KIM_RUN_ID").unwrap().starts_with("sess-"));
        }
    }
    // The intended difference: MCP tools operate on the target project (GUI)
    // vs the kim root (bridge default).
    assert_eq!(env_of(&gui_spec(None), "PROJECT_ROOT"), Some("/proj"));
    assert_eq!(env_of(&bridge_spec(None), "PROJECT_ROOT"), Some("/kim"));
}

#[test]
fn legacy_permission_aliases_match_canonical_on_both_paths() {
    assert_eq!(
        env_of(
            &gui_spec(Some("confirm-sensitive")),
            "KIM_HITL_RISK_THRESHOLD"
        ),
        Some("high")
    );
    assert_eq!(
        env_of(&bridge_spec(Some("confirm-all")), "KIM_HITL_RISK_THRESHOLD"),
        Some("medium")
    );
}

#[test]
fn provider_promotion_is_code_tab_only() {
    assert_eq!(
        promote_provider(Some("chatgpt".into()), "ollama", true),
        "browser:chatgpt"
    );
    assert_eq!(
        promote_provider(Some("chatgpt".into()), "ollama", false),
        "chatgpt"
    );
    assert_eq!(promote_provider(None, "browser", false), "browser");
}

#[test]
fn codex_direct_spec_has_no_stdin_and_no_orchestrator_env() {
    let spec = codex_direct_spec(CodexDirectSpecParams {
        code_bin: Path::new("/bin/codex"),
        is_claw: false,
        target_root: Path::new("/proj"),
        task: "t",
        bypass_sandbox: false,
        route: ProviderRoute::default(),
        session_id: "s".into(),
    });
    assert_eq!(spec.stdin, StdinMode::Null);
    assert!(env_of(&spec, "KIM_TAURI_MODE").is_none());
    assert!(env_of(&spec, "PYTHONPATH").is_none());
    assert_eq!(spec.args[..2], ["exec".to_string(), "--json".to_string()]);
}

#[test]
fn codex_browser_spec_keeps_hitl_stdin_contract() {
    let spec = codex_browser_spec(CodexBridgeSpecParams {
        python: "/venv/bin/python",
        kim_root: Path::new("/kim"),
        target_root: Path::new("/proj"),
        task: "t",
        provider: "browser:gemini",
        codex_bin: Path::new("/bin/codex"),
        session_id: "s".into(),
        permission_mode: Some("ask_risky"),
        extra_envs: vec![],
    });
    // The pre-spawn HITL round-trip requires a live stdin pipe.
    assert_eq!(spec.stdin, StdinMode::Piped);
    assert_eq!(env_of(&spec, "CODEX_BIN"), Some("/bin/codex"));
    assert_eq!(env_of(&spec, "KIM_HITL_RISK_THRESHOLD"), Some("high"));
}

// ---------------------------------------------------------------------------
// F-H-8/F-H-2 — every orchestrator-backed spawn shape MUST export the
// run-identity envelope (KIM_RUN_ID + KIM_SESSION_ID) so the Python emitter
// (events_gen) self-stamps typed events with the session they belong to.
// Before the fix, codex_browser_spec exported neither, so Code-tab browser
// events routed to whatever view was mounted (F-F-2/F-F-8).
// ---------------------------------------------------------------------------

#[test]
fn codex_browser_spec_exports_run_identity_envelope() {
    let spec = codex_browser_spec(CodexBridgeSpecParams {
        python: "/venv/bin/python",
        kim_root: Path::new("/kim"),
        target_root: Path::new("/proj"),
        task: "t",
        provider: "browser:gemini",
        codex_bin: Path::new("/bin/codex"),
        session_id: "sess".into(),
        permission_mode: None,
        extra_envs: vec![],
    });
    // KIM_SESSION_ID is the session verbatim; KIM_RUN_ID is derived from it,
    // exactly as chat_task_spec does (sanitized session + "-" + timestamp).
    assert_eq!(env_of(&spec, "KIM_SESSION_ID"), Some("sess"));
    assert!(
        env_of(&spec, "KIM_RUN_ID")
            .expect("KIM_RUN_ID must be exported on the codex browser-bridge spawn")
            .starts_with("sess-"),
        "KIM_RUN_ID should be run_id_for_session(session_id)"
    );
}

#[test]
fn every_orchestrator_spawn_shape_exports_run_identity() {
    // Both orchestrator-backed shapes (chat + codex browser-bridge) must carry
    // the envelope. codex_direct_spec runs a non-Kim binary that emits no typed
    // events, so it is intentionally exempt.
    let chat = gui_spec(None);
    let codex = codex_browser_spec(CodexBridgeSpecParams {
        python: "/venv/bin/python",
        kim_root: Path::new("/kim"),
        target_root: Path::new("/proj"),
        task: "t",
        provider: "browser:claude",
        codex_bin: Path::new("/bin/codex"),
        session_id: "sess".into(),
        permission_mode: None,
        extra_envs: vec![],
    });
    for spec in [&chat, &codex] {
        assert!(
            env_of(spec, "KIM_RUN_ID").is_some(),
            "orchestrator spawn shape missing KIM_RUN_ID"
        );
        assert_eq!(
            env_of(spec, "KIM_SESSION_ID"),
            Some("sess"),
            "orchestrator spawn shape missing/incorrect KIM_SESSION_ID"
        );
    }
}

// ---------------------------------------------------------------------------
// Behavioral: a TaskSpec is executable — a fake recorder binary observes
// exactly the argv and env the spec declared.
// ---------------------------------------------------------------------------

#[cfg(unix)]
#[test]
fn spawned_spec_reproduces_declared_argv_and_env() {
    use std::os::unix::fs::PermissionsExt;

    let tmp = tempfile::tempdir().expect("tempdir");
    let recorder = tmp.path().join("recorder.sh");
    let out_path = tmp.path().join("recorded.txt");
    std::fs::write(
        &recorder,
        format!(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > {out}\nenv | grep -E '^(PYTHONPATH|PROJECT_ROOT|KIM_)' | sort >> {out}\n",
            out = out_path.display()
        ),
    )
    .unwrap();
    std::fs::set_permissions(&recorder, std::fs::Permissions::from_mode(0o755)).unwrap();

    let spec = chat_task_spec(ChatSpecParams {
        python: recorder.to_str().unwrap(),
        bundled_sidecar: true, // recorder is not a python interpreter
        kim_root: tmp.path(),
        project_root: tmp.path(),
        session_dir: Path::new("/tmp/sessions"),
        task: "record me",
        provider: "ollama",
        session_id: "rec".into(),
        resume: None,
        permission_mode: Some("ask_always"),
        source: SpawnSource::Gui,
        extra_envs: vec![("KIM_EXTRA_MARKER".into(), "yes".into())],
    });

    // Execute the spec exactly as the supervisor would (minus tokio/pipes).
    let mut cmd = std::process::Command::new(&spec.program);
    cmd.args(&spec.args);
    if let Some(cwd) = &spec.cwd {
        cmd.current_dir(cwd);
    }
    for (k, v) in &spec.envs {
        cmd.env(k, v);
    }
    let status = cmd.status().expect("spawn recorder");
    assert!(status.success());

    let recorded = std::fs::read_to_string(&out_path).expect("recorder output");
    // Argv, one per line, in declared order.
    let argv: Vec<&str> = recorded.lines().take(spec.args.len()).collect();
    assert_eq!(
        argv,
        spec.args.iter().map(String::as_str).collect::<Vec<_>>()
    );
    // Declared env observed by the child.
    assert!(recorded.contains("KIM_HITL_RISK_THRESHOLD=medium"));
    assert!(recorded.contains("KIM_TAURI_MODE=1"));
    assert!(recorded.contains("KIM_EXTRA_MARKER=yes"));
    assert!(recorded.contains(&format!("PROJECT_ROOT={}", tmp.path().display())));
    assert!(recorded.contains("KIM_RUN_ID=rec-"));
}

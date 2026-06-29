//! Session listing, deletion, summarization, and message loading.
//!
//! Extracted from lib.rs (Phase 8 restructure).
//! Public Tauri commands: `list_sessions`, `delete_sessions`,
//! `delete_all_sessions`, `prune_sessions`,
//! `summarize_session`, `load_session_messages`, `get_app_version`.

use std::fs;
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use serde_json;

#[tauri::command]
pub async fn list_sessions(
    kim_dir: Option<String>,
    codex_dir: Option<String>,
) -> Result<Vec<crate::SessionInfo>, String> {
    let kim_base = kim_dir
        .map(PathBuf::from)
        .unwrap_or_else(crate::default_sessions_dir);

    let mut sessions = crate::read_sessions_from_dir(&kim_base, "kim")?;

    if let Some(codex_path) = codex_dir {
        let codex_base = PathBuf::from(codex_path);
        let codex_sessions = crate::read_sessions_from_dir(&codex_base, "codex")?;
        sessions.extend(codex_sessions);
    }

    Ok(sessions)
}

#[tauri::command]
pub async fn delete_sessions(
    session_ids: Vec<String>,
    kim_dir: Option<String>,
    codex_dir: Option<String>,
) -> Result<(), String> {
    let mut failures: Vec<String> = Vec::new();

    for session_id in session_ids {
        crate::validate_session_id(&session_id)?;

        let mut deleted = false;
        let mut had_hard_failure = false;

        let dirs_to_search: Vec<PathBuf> = {
            let mut v = vec![kim_dir
                .as_deref()
                .map(PathBuf::from)
                .unwrap_or_else(crate::default_sessions_dir)];
            if let Some(codex_path) = &codex_dir {
                v.push(PathBuf::from(codex_path));
            }
            v
        };

        for base in &dirs_to_search {
            if !base.exists() {
                continue;
            }
            if let Ok(entries) = std::fs::read_dir(base) {
                for entry in entries.filter_map(|e| e.ok()) {
                    let date_dir = entry.path();
                    if !date_dir.is_dir() {
                        continue;
                    }
                    let jsonl_path = date_dir.join(format!("{}.jsonl", session_id));
                    if jsonl_path.exists() {
                        match std::fs::remove_file(&jsonl_path) {
                            Err(e) => {
                                had_hard_failure = true;
                                failures.push(format!(
                                    "Failed to delete session file {}: {}",
                                    session_id, e
                                ));
                            }
                            Ok(()) => {
                                deleted = true;
                                let summary_path = date_dir.join(format!("{}.summary.txt", session_id));
                                if summary_path.exists() {
                                    let _ = std::fs::remove_file(&summary_path);
                                }
                                let browser_meta_path = date_dir.join(crate::browser_session_meta_filename(&session_id));
                                if browser_meta_path.exists() {
                                    let _ = std::fs::remove_file(&browser_meta_path);
                                }
                            }
                        }
                    }
                }
            }
        }

        if !deleted && !had_hard_failure {
            failures.push(format!("Session {} not found for deletion.", session_id));
        }
    }

    if failures.is_empty() {
        Ok(())
    } else {
        Err(failures.join("; "))
    }
}

#[tauri::command]
pub async fn summarize_session(
    session_id: String,
    session_type: String,
    project_root: Option<String>,
) -> Result<(), String> {
    crate::validate_session_id(&session_id)?;

    let mut dirs_to_search: Vec<PathBuf> = vec![crate::default_sessions_dir()];
    if session_type == "codex" {
        if let Some(ref pr) = project_root {
            let codex_dir = PathBuf::from(pr).join(".codex").join("sessions");
            if codex_dir.exists() {
                dirs_to_search.push(codex_dir);
            }
        }
    }

    let mut jsonl_path: Option<PathBuf> = None;
    for base in &dirs_to_search {
        if !base.exists() { continue; }
        if let Ok(entries) = fs::read_dir(base) {
            let mut date_dirs: Vec<_> = entries
                .filter_map(|e| e.ok())
                .filter(|e| e.path().is_dir())
                .collect();
            date_dirs.sort_by_key(|b| std::cmp::Reverse(b.file_name()));
            for entry in date_dirs {
                let candidate = entry.path().join(format!("{}.jsonl", session_id));
                if candidate.exists() {
                    jsonl_path = Some(candidate);
                    break;
                }
            }
        }
        if jsonl_path.is_some() { break; }
    }

    let path = jsonl_path.ok_or_else(|| format!("Session file not found: {}", session_id))?;

    let file = fs::File::open(&path).map_err(|e| e.to_string())?;
    let reader = BufReader::new(file);

    let mut first_user_text: Option<String> = None;
    let mut last_assistant_text: Option<String> = None;
    let mut touched_files: Vec<String> = Vec::new();

    for line in reader.lines().map_while(Result::ok) {
        let trimmed = line.trim();
        if trimmed.is_empty() { continue; }
        let value: serde_json::Value = match serde_json::from_str(trimmed) {
            Ok(v) => v,
            Err(_) => continue,
        };

        let (role, content) = if let Some(r) = value.get("role").and_then(|v| v.as_str()) {
            (r.to_string(), value.get("content").cloned())
        } else if value.get("type").and_then(|v| v.as_str()) == Some("message") {
            let msg = value.get("message").unwrap_or(&serde_json::Value::Null);
            let r = msg.get("role").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let c = msg.get("blocks").or_else(|| msg.get("content")).cloned();
            (r, c)
        } else {
            continue;
        };

        let text = match &content {
            Some(serde_json::Value::String(s)) => Some(s.clone()),
            Some(serde_json::Value::Array(items)) => {
                let texts: Vec<String> = items.iter()
                    .filter(|b| b.get("type").and_then(|v| v.as_str()) == Some("text"))
                    .filter_map(|b| b.get("text").and_then(|v| v.as_str()).map(|s| s.to_string()))
                    .collect();
                if texts.is_empty() { None } else { Some(texts.join("\n")) }
            }
            _ => None,
        };

        if role == "user" {
            if let Some(ref t) = text {
                let clean = t.trim();
                if !clean.starts_with("[Tool result:") && !clean.is_empty() && first_user_text.is_none() {
                    first_user_text = Some(clean.strip_prefix("Task: ").unwrap_or(clean).to_string());
                }
            }
        } else if role == "assistant" {
            if let Some(ref t) = text {
                let clean = t.trim()
                    .trim_start_matches("TASK_COMPLETE:")
                    .trim();
                if !clean.is_empty() {
                    last_assistant_text = Some(clean.to_string());
                }
            }
            if let Some(serde_json::Value::Array(items)) = &content {
                for block in items {
                    if block.get("type").and_then(|v| v.as_str()) == Some("tool_use") {
                        let name = block.get("name").and_then(|v| v.as_str()).unwrap_or("");
                        if matches!(name, "write_file" | "edit_file" | "create_file") {
                            let mut path_str = None;
                            if let Some(input_val) = block.get("input") {
                                if let Some(s) = input_val.as_str() {
                                    if let Ok(parsed) = serde_json::from_str::<serde_json::Value>(s) {
                                        path_str = parsed.get("path").or_else(|| parsed.get("file_path"))
                                            .and_then(|v| v.as_str())
                                            .map(|s| s.to_string());
                                    }
                                } else if let Some(obj) = input_val.as_object() {
                                    path_str = obj.get("path").or_else(|| obj.get("file_path"))
                                        .and_then(|v| v.as_str())
                                        .map(|s| s.to_string());
                                }
                            }
                            if let Some(p) = path_str {
                                let fname = p.rsplit('/').next().unwrap_or(&p);
                                if !touched_files.contains(&fname.to_string()) {
                                    touched_files.push(fname.to_string());
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    let mut summary_parts: Vec<String> = Vec::new();
    if let Some(ref task) = first_user_text {
        let truncated = if task.chars().count() > 100 {
            format!("{}…", task.chars().take(100).collect::<String>())
        } else {
            task.clone()
        };
        summary_parts.push(format!("Task: {}", truncated));
    }
    if let Some(ref result) = last_assistant_text {
        let truncated = if result.chars().count() > 200 {
            format!("{}…", result.chars().take(200).collect::<String>())
        } else {
            result.clone()
        };
        summary_parts.push(format!("Result: {}", truncated));
    }
    if !touched_files.is_empty() {
        summary_parts.push(format!("Files: {}", touched_files.join(", ")));
    }

    let summary = if summary_parts.is_empty() {
        "No summary available.".to_string()
    } else {
        summary_parts.join("\n")
    };

    let summary_path = path.with_file_name(format!("{}.summary.txt", session_id));
    fs::write(&summary_path, &summary).map_err(|e| e.to_string())?;

    Ok(())
}

#[tauri::command]
pub async fn load_session_messages(
    session_id: String,
    session_date: Option<String>,
    kim_dir: Option<String>,
    codex_dir: Option<String>,
) -> Result<Vec<crate::KimMessage>, String> {
    crate::validate_session_id(&session_id)?;
    let dirs_to_search: Vec<PathBuf> = {
        let mut v = vec![kim_dir
            .map(PathBuf::from)
            .unwrap_or_else(crate::default_sessions_dir)];
        if let Some(codex_path) = codex_dir {
            v.push(PathBuf::from(codex_path));
        }
        v
    };

    for base in &dirs_to_search {
        if !base.exists() {
            continue;
        }
        if let Some(date) = session_date.as_deref() {
            crate::validate_session_id(date)?;
            let date_dir = base.join(date);
            let candidate = date_dir.join(format!("{}.jsonl", session_id));
            if candidate.exists() {
                let (canon_candidate, canon_dir) = match (
                    candidate.canonicalize(),
                    date_dir.canonicalize(),
                ) {
                    (Ok(c), Ok(d)) => (c, d),
                    _ => continue,
                };
                if !canon_candidate.starts_with(&canon_dir) {
                    return Err("Resolved session path escapes its date directory".to_string());
                }
                return crate::parse_jsonl(&canon_candidate);
            }
            continue;
        }
        let mut date_dirs: Vec<_> = fs::read_dir(base)
            .map_err(|e| e.to_string())?
            .filter_map(|e| e.ok())
            .filter(|e| e.path().is_dir())
            .collect();
        date_dirs.sort_by_key(|b| std::cmp::Reverse(b.file_name()));

        for date_entry in date_dirs {
            let date_dir = date_entry.path();
            let candidate = date_dir.join(format!("{}.jsonl", session_id));
            if !candidate.exists() {
                continue;
            }
            let (canon_candidate, canon_dir) = match (
                candidate.canonicalize(),
                date_dir.canonicalize(),
            ) {
                (Ok(c), Ok(d)) => (c, d),
                _ => continue,
            };
            if !canon_candidate.starts_with(&canon_dir) {
                return Err("Resolved session path escapes its date directory".to_string());
            }
            return crate::parse_jsonl(&canon_candidate);
        }
    }

    Err(format!("Session not found: {}", session_id))
}

/// Apply the retention policy: strip screenshots from sessions older than
/// `screenshot_strip_age_days` and delete sessions older than `max_age_days`.
/// Delegates to the Python SessionStore.prune_old_sessions via a subprocess call,
/// so config and Python helper logic stay in one place.
///
/// Returns a JSON string: {"stripped": N, "deleted": N}.
#[tauri::command]
pub async fn prune_sessions(
    max_age_days: Option<u32>,
    screenshot_strip_age_days: Option<u32>,
) -> Result<String, String> {
    use tokio::process::Command;
    let kim_root = crate::default_project_root();
    let python = crate::find_python_interpreter(&kim_root)?;

    let max_days = max_age_days.unwrap_or(30);
    let strip_days = screenshot_strip_age_days.unwrap_or(2);

    // D5: pass root/args via argv, never interpolated into the source — a path
    // containing a quote or backslash would otherwise break (or inject) the script.
    let script = r#"
import json, sys
sys.path.insert(0, sys.argv[1])
from orchestrator.session_store import SessionStore
result = SessionStore.prune_old_sessions(
    max_age_days=int(sys.argv[2]),
    screenshot_strip_age_days=int(sys.argv[3]),
)
print(json.dumps(result))
"#;

    let output = Command::new(&python)
        .arg("-c")
        .arg(script)
        .arg(kim_root.as_os_str())
        .arg(max_days.to_string())
        .arg(strip_days.to_string())
        .env("PYTHONPATH", kim_root.to_str().unwrap_or(""))
        .output()
        .await
        .map_err(|e| format!("Failed to spawn Python for prune: {e}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!("prune_sessions failed: {stderr}"));
    }

    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
    // Validate it's JSON before returning
    serde_json::from_str::<serde_json::Value>(&stdout)
        .map_err(|e| format!("Unexpected prune output: {e}"))?;
    Ok(stdout)
}

fn validate_run_id(run_id: &str) -> Result<(), String> {
    if run_id.is_empty() {
        return Err("run_id is empty".to_string());
    }
    if run_id.len() > 200 {
        return Err("run_id is too long".to_string());
    }
    if !run_id
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
    {
        return Err("run_id contains illegal characters".to_string());
    }
    Ok(())
}

/// K1: restore a run's pre-images. Shells the Python checkpoints helper, passing
/// the run id via argv (never interpolated into source — see D5).
#[tauri::command]
pub async fn revert_run(run_id: String) -> Result<String, String> {
    use tokio::process::Command;
    validate_run_id(&run_id)?;
    let kim_root = crate::default_project_root();
    let python = crate::find_python_interpreter(&kim_root)?;
    let script = r#"
import json, sys
sys.path.insert(0, sys.argv[1])
from mcp_server.checkpoints import revert_run
print(json.dumps(revert_run(sys.argv[2])))
"#;
    let output = Command::new(&python)
        .arg("-c")
        .arg(script)
        .arg(kim_root.as_os_str())
        .arg(&run_id)
        .env("PYTHONPATH", kim_root.to_str().unwrap_or(""))
        .output()
        .await
        .map_err(|e| format!("Failed to spawn Python for revert: {e}"))?;
    if !output.status.success() {
        return Err(format!(
            "revert_run failed: {}",
            String::from_utf8_lossy(&output.stderr)
        ));
    }
    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
    serde_json::from_str::<serde_json::Value>(&stdout)
        .map_err(|e| format!("Unexpected revert output: {e}"))?;
    Ok(stdout)
}

/// K1: does a checkpoint exist for this run id?
#[tauri::command]
pub fn has_checkpoint(run_id: String) -> bool {
    if validate_run_id(&run_id).is_err() {
        return false;
    }
    dirs::home_dir()
        .map(|h| {
            h.join(".kim")
                .join("checkpoints")
                .join(&run_id)
                .join("manifest.jsonl")
                .exists()
        })
        .unwrap_or(false)
}

/// K9: toggle the privacy-pause sentinel (`~/.kim/privacy_pause`). While present,
/// the MCP server's screen-capture tools refuse to run.
#[tauri::command]
pub fn set_privacy_pause(on: bool) -> Result<(), String> {
    let home = dirs::home_dir().ok_or_else(|| "No home directory".to_string())?;
    let dir = home.join(".kim");
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let sentinel = dir.join("privacy_pause");
    if on {
        std::fs::write(&sentinel, "1").map_err(|e| e.to_string())?;
    } else {
        let _ = std::fs::remove_file(&sentinel);
    }
    Ok(())
}

/// K9: report whether privacy pause is currently on.
#[tauri::command]
pub fn get_privacy_pause() -> bool {
    dirs::home_dir()
        .map(|h| h.join(".kim").join("privacy_pause").exists())
        .unwrap_or(false)
}

/// Open the logs directory in the system file manager.
/// Creates the directory if it doesn't exist yet.
#[tauri::command]
pub fn reveal_logs() -> Result<(), String> {
    // D3: logs live under the repo `logs/` when writable, else `~/.kim/logs`
    // (packaged/read-only installs). Reveal whichever actually exists, preferring
    // an existing repo dir, then an existing fallback, then creating one.
    let repo_logs = crate::default_project_root().join("logs");
    let home_logs = dirs::home_dir().map(|h| h.join(".kim").join("logs"));
    let log_dir = if repo_logs.is_dir() {
        repo_logs
    } else if let Some(h) = home_logs.as_ref().filter(|p| p.is_dir()) {
        h.clone()
    } else if std::fs::create_dir_all(&repo_logs).is_ok() {
        repo_logs
    } else if let Some(h) = home_logs {
        std::fs::create_dir_all(&h).map_err(|e| format!("Failed to create logs dir: {e}"))?;
        h
    } else {
        return Err("No writable logs directory found.".to_string());
    };

    #[cfg(target_os = "macos")]
    std::process::Command::new("open")
        .arg(&log_dir)
        .spawn()
        .map_err(|e| format!("open failed: {e}"))?;

    #[cfg(target_os = "windows")]
    std::process::Command::new("explorer")
        .arg(&log_dir)
        .spawn()
        .map_err(|e| format!("explorer failed: {e}"))?;

    #[cfg(target_os = "linux")]
    std::process::Command::new("xdg-open")
        .arg(&log_dir)
        .spawn()
        .map_err(|e| format!("xdg-open failed: {e}"))?;

    Ok(())
}

#[tauri::command]
pub fn get_app_version() -> String {
    env!("CARGO_PKG_VERSION").to_string()
}

// ── K4: session management (rename / pin / delete / search) ──────────────────

use std::path::Path;

/// Read the `<id>.meta.json` sidecar → (title override, pinned).
pub(crate) fn read_session_meta(date_dir: &Path, session_id: &str) -> (Option<String>, bool) {
    let p = date_dir.join(format!("{session_id}.meta.json"));
    let Ok(text) = fs::read_to_string(p) else {
        return (None, false);
    };
    let Ok(v) = serde_json::from_str::<serde_json::Value>(&text) else {
        return (None, false);
    };
    let title = v.get("title").and_then(|x| x.as_str()).map(|s| s.to_string());
    let pinned = v.get("pinned").and_then(|x| x.as_bool()).unwrap_or(false);
    (title, pinned)
}

fn write_session_meta(
    date_dir: &Path,
    session_id: &str,
    set_title: Option<String>,
    set_pinned: Option<bool>,
) -> Result<(), String> {
    let (cur_title, cur_pinned) = read_session_meta(date_dir, session_id);
    let title = set_title.or(cur_title);
    let pinned = set_pinned.unwrap_or(cur_pinned);
    let mut obj = serde_json::Map::new();
    if let Some(t) = title {
        obj.insert("title".into(), serde_json::Value::String(t));
    }
    obj.insert("pinned".into(), serde_json::Value::Bool(pinned));
    fs::create_dir_all(date_dir).map_err(|e| e.to_string())?;
    let dest = date_dir.join(format!("{session_id}.meta.json"));
    // Atomic write: write to a sibling tmp file then rename so concurrent
    // readers never see a partial file.
    let tmp = date_dir.join(format!("{session_id}.meta.json.tmp"));
    fs::write(&tmp, serde_json::Value::Object(obj).to_string())
        .map_err(|e| e.to_string())?;
    fs::rename(&tmp, &dest).map_err(|e| {
        let _ = fs::remove_file(&tmp);
        e.to_string()
    })
}

fn base_dir(kim_dir: &Option<String>) -> PathBuf {
    kim_dir
        .as_deref()
        .map(PathBuf::from)
        .unwrap_or_else(crate::default_sessions_dir)
}

#[tauri::command]
pub fn rename_session(
    session_id: String,
    date: String,
    title: String,
    kim_dir: Option<String>,
) -> Result<(), String> {
    crate::validate_session_id(&session_id)?;
    crate::validate_session_id(&date)?;
    let date_dir = base_dir(&kim_dir).join(&date);
    write_session_meta(&date_dir, &session_id, Some(title), None)
}

#[tauri::command]
pub fn set_session_pinned(
    session_id: String,
    date: String,
    pinned: bool,
    kim_dir: Option<String>,
) -> Result<(), String> {
    crate::validate_session_id(&session_id)?;
    crate::validate_session_id(&date)?;
    let date_dir = base_dir(&kim_dir).join(&date);
    write_session_meta(&date_dir, &session_id, None, Some(pinned))
}

/// Remove a session's JSONL + summary + meta + browser-meta + all sidecar files.
/// Returns count of files removed.
pub(crate) fn delete_session_files(date_dir: &Path, session_id: &str) -> usize {
    // Fixed-name sidecars.
    let fixed = [
        format!("{session_id}.jsonl"),
        format!("{session_id}.summary.txt"),
        format!("{session_id}.meta.json"),
        format!("{session_id}.browser.json"),
        format!("{session_id}.context.json"),
        format!("{session_id}.runs.json"),
    ];
    let mut n = 0;
    for name in &fixed {
        if fs::remove_file(date_dir.join(name)).is_ok() {
            n += 1;
        }
    }
    // Wildcard sidecars: <id>.compact.*.json and <id>.roll.*.jsonl.
    // Scan the directory so we don't need an external glob crate.
    let compact_prefix = format!("{session_id}.compact.");
    let roll_prefix = format!("{session_id}.roll.");
    if let Ok(entries) = fs::read_dir(date_dir) {
        for entry in entries.filter_map(|e| e.ok()) {
            let fname = entry.file_name();
            let fname_str = fname.to_string_lossy();
            let is_compact = fname_str.starts_with(&compact_prefix)
                && fname_str.ends_with(".json");
            let is_roll = fname_str.starts_with(&roll_prefix)
                && fname_str.ends_with(".jsonl");
            if (is_compact || is_roll) && fs::remove_file(entry.path()).is_ok() {
                n += 1;
            }
        }
    }
    n
}

#[tauri::command]
pub fn delete_session(
    session_id: String,
    date: String,
    kim_dir: Option<String>,
) -> Result<usize, String> {
    crate::validate_session_id(&session_id)?;
    crate::validate_session_id(&date)?;
    let date_dir = base_dir(&kim_dir).join(&date);
    let n = delete_session_files(&date_dir, &session_id);
    if n == 0 {
        return Err(format!("No files removed for session {session_id}"));
    }
    Ok(n)
}

#[derive(serde::Serialize)]
pub struct SearchHit {
    pub session_id: String,
    pub date: String,
    pub title: String,
    pub snippet: String,
}

/// Grep title + message content across a sessions base dir. Caps results and
/// wall-clock so the sidebar stays responsive on large histories.
pub(crate) fn search_in_dir(base: &Path, query: &str, cap: usize, budget_ms: u128) -> Vec<SearchHit> {
    let q = query.trim().to_lowercase();
    let mut hits = Vec::new();
    if q.is_empty() || !base.exists() {
        return hits;
    }
    let start = std::time::Instant::now();
    let Ok(date_dirs) = fs::read_dir(base) else {
        return hits;
    };
    let mut dirs: Vec<PathBuf> = date_dirs
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.is_dir())
        .collect();
    dirs.sort_by(|a, b| b.file_name().cmp(&a.file_name())); // newest first
    for date_dir in dirs {
        let date = date_dir.file_name().unwrap_or_default().to_string_lossy().to_string();
        let Ok(files) = fs::read_dir(&date_dir) else { continue };
        for entry in files.filter_map(|e| e.ok()) {
            if hits.len() >= cap || start.elapsed().as_millis() > budget_ms {
                return hits;
            }
            let path = entry.path();
            let name = path.file_name().unwrap_or_default().to_string_lossy().to_string();
            if !name.ends_with(".jsonl") || name.contains(".summary") {
                continue;
            }
            let session_id = path.file_stem().unwrap_or_default().to_string_lossy().to_string();
            let (meta_title, _) = read_session_meta(&date_dir, &session_id);
            let title = meta_title.unwrap_or_else(|| session_id.clone());
            let content = fs::read_to_string(&path).unwrap_or_default();
            let hay = format!("{}\n{}", title, content).to_lowercase();
            if let Some(idx) = hay.find(&q) {
                let snippet_src = if title.to_lowercase().contains(&q) { &title } else { &content };
                let snippet = make_snippet(snippet_src, &q).unwrap_or_else(|| {
                    let s = hay.get(idx..(idx + 60).min(hay.len())).unwrap_or("");
                    s.to_string()
                });
                hits.push(SearchHit { session_id, date: date.clone(), title, snippet });
            }
        }
    }
    hits
}

fn make_snippet(text: &str, q: &str) -> Option<String> {
    let lower = text.to_lowercase();
    let idx = lower.find(q)?;
    let start = idx.saturating_sub(30);
    let end = (idx + q.len() + 30).min(text.len());
    // snap to char boundaries
    let start = (0..=start).rev().find(|i| text.is_char_boundary(*i)).unwrap_or(0);
    let end = (end..=text.len()).find(|i| text.is_char_boundary(*i)).unwrap_or(text.len());
    Some(text[start..end].replace('\n', " ").trim().to_string())
}

#[tauri::command]
pub fn search_sessions(query: String, kim_dir: Option<String>) -> Result<Vec<SearchHit>, String> {
    Ok(search_in_dir(&base_dir(&kim_dir), &query, 50, 200))
}

#[cfg(test)]
mod k4_tests {
    use super::*;

    fn tmp() -> PathBuf {
        let p = std::env::temp_dir().join(format!(
            "kim-k4-{}",
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()
        ));
        fs::create_dir_all(&p).unwrap();
        p
    }

    #[test]
    fn delete_removes_all_session_files() {
        let base = tmp();
        let date_dir = base.join("2026-06-18");
        fs::create_dir_all(&date_dir).unwrap();
        let id = "sess1";
        fs::write(date_dir.join(format!("{id}.jsonl")), "{}").unwrap();
        fs::write(date_dir.join(format!("{id}.summary.txt")), "s").unwrap();
        write_session_meta(&date_dir, id, Some("Title".into()), Some(true)).unwrap();
        let n = delete_session_files(&date_dir, id);
        assert_eq!(n, 3);
        assert!(!date_dir.join(format!("{id}.jsonl")).exists());
        assert!(!date_dir.join(format!("{id}.meta.json")).exists());
        let _ = fs::remove_dir_all(&base);
    }

    #[test]
    fn search_matches_title_and_body_and_caps() {
        let base = tmp();
        let date_dir = base.join("2026-06-18");
        fs::create_dir_all(&date_dir).unwrap();
        // body match
        fs::write(date_dir.join("a.jsonl"), r#"{"role":"user","content":"fix the login bug"}"#).unwrap();
        // title match (via meta)
        fs::write(date_dir.join("b.jsonl"), "{}").unwrap();
        write_session_meta(&date_dir, "b", Some("Login redesign".into()), None).unwrap();
        // no match
        fs::write(date_dir.join("c.jsonl"), r#"{"role":"user","content":"weather report"}"#).unwrap();

        let hits = search_in_dir(&base, "login", 50, 1000);
        let ids: Vec<_> = hits.iter().map(|h| h.session_id.as_str()).collect();
        assert!(ids.contains(&"a"));
        assert!(ids.contains(&"b"));
        assert!(!ids.contains(&"c"));

        // cap respected
        let capped = search_in_dir(&base, "login", 1, 1000);
        assert_eq!(capped.len(), 1);
        let _ = fs::remove_dir_all(&base);
    }

    #[test]
    fn empty_query_returns_nothing() {
        let base = tmp();
        assert!(search_in_dir(&base, "  ", 50, 1000).is_empty());
        let _ = fs::remove_dir_all(&base);
    }

    #[test]
    fn run_id_validation_rejects_path_segments() {
        assert!(validate_run_id("session-123_456").is_ok());
        assert!(validate_run_id("../session-123").is_err());
        assert!(validate_run_id("nested/session").is_err());
        assert!(validate_run_id("").is_err());
    }

    // ── Regression guards for delete_session_files (context.json / runs.json /
    //    wildcard sidecars / isolation between session ids) ────────────────────

    /// All six fixed-name sidecars are removed and the returned count matches.
    #[test]
    fn delete_session_files_removes_fixed_sidecars() {
        let base = tmp();
        let date_dir = base.join("2026-06-28");
        fs::create_dir_all(&date_dir).unwrap();
        let id = "abc-123";
        let fixed = [
            format!("{id}.jsonl"),
            format!("{id}.summary.txt"),
            format!("{id}.meta.json"),
            format!("{id}.browser.json"),
            format!("{id}.context.json"),
            format!("{id}.runs.json"),
        ];
        for name in &fixed {
            fs::write(date_dir.join(name), "x").unwrap();
        }
        let n = delete_session_files(&date_dir, id);
        assert_eq!(n, 6, "expected 6 fixed sidecars removed, got {n}");
        for name in &fixed {
            assert!(
                !date_dir.join(name).exists(),
                "{name} should have been deleted"
            );
        }
        let _ = fs::remove_dir_all(&base);
    }

    /// Wildcard sidecars (<id>.compact.*.json and <id>.roll.*.jsonl) are also
    /// removed by the directory-scan path.
    #[test]
    fn delete_session_files_removes_wildcards() {
        let base = tmp();
        let date_dir = base.join("2026-06-28");
        fs::create_dir_all(&date_dir).unwrap();
        let id = "sess-wild";
        // At least one fixed file so deletion does not return 0.
        fs::write(date_dir.join(format!("{id}.jsonl")), "{}").unwrap();
        // Wildcard sidecars.
        let compact = format!("{id}.compact.v1.json");
        let roll1 = format!("{id}.roll.2026-06-28.jsonl");
        let roll2 = format!("{id}.roll.2026-06-27.jsonl");
        fs::write(date_dir.join(&compact), "{}").unwrap();
        fs::write(date_dir.join(&roll1), "{}").unwrap();
        fs::write(date_dir.join(&roll2), "{}").unwrap();

        let n = delete_session_files(&date_dir, id);
        // 1 fixed (.jsonl) + 1 compact + 2 roll = 4 total.
        assert_eq!(n, 4, "expected 4 files removed, got {n}");
        assert!(!date_dir.join(&compact).exists(), "compact sidecar should be gone");
        assert!(!date_dir.join(&roll1).exists(), "roll sidecar 1 should be gone");
        assert!(!date_dir.join(&roll2).exists(), "roll sidecar 2 should be gone");
        let _ = fs::remove_dir_all(&base);
    }

    /// Files belonging to a different session id, and files with non-matching
    /// names, are not touched when deleting a specific session.
    #[test]
    fn unrelated_files_untouched() {
        let base = tmp();
        let date_dir = base.join("2026-06-28");
        fs::create_dir_all(&date_dir).unwrap();

        let target = "target-session";
        let other = "other-session";

        // Target session files.
        fs::write(date_dir.join(format!("{target}.jsonl")), "{}").unwrap();
        fs::write(date_dir.join(format!("{target}.context.json")), "{}").unwrap();

        // Other session files — must survive.
        let other_jsonl = date_dir.join(format!("{other}.jsonl"));
        let other_meta = date_dir.join(format!("{other}.meta.json"));
        let other_roll = date_dir.join(format!("{other}.roll.2026-06-28.jsonl"));
        fs::write(&other_jsonl, "{}").unwrap();
        fs::write(&other_meta, "{}").unwrap();
        fs::write(&other_roll, "{}").unwrap();

        // A completely unrelated file — must survive.
        let unrelated = date_dir.join("README.txt");
        fs::write(&unrelated, "hello").unwrap();

        let n = delete_session_files(&date_dir, target);
        assert_eq!(n, 2, "expected 2 target files removed, got {n}");

        assert!(other_jsonl.exists(), "other session jsonl must be untouched");
        assert!(other_meta.exists(), "other session meta must be untouched");
        assert!(other_roll.exists(), "other session roll must be untouched");
        assert!(unrelated.exists(), "unrelated file must be untouched");
        let _ = fs::remove_dir_all(&base);
    }
}

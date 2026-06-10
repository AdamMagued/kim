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
    for session_id in session_ids {
        crate::validate_session_id(&session_id)?;

        let mut deleted = false;

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
                        if let Err(e) = std::fs::remove_file(&jsonl_path) {
                            eprintln!("Failed to delete session file {}: {}", session_id, e);
                        } else {
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

        if !deleted {
            eprintln!("Session {} not found for deletion.", session_id);
        }
    }

    Ok(())
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

    let script = format!(
        r#"
import json, sys
sys.path.insert(0, r"{root}")
from orchestrator.session_store import SessionStore
result = SessionStore.prune_old_sessions(max_age_days={max}, screenshot_strip_age_days={strip})
print(json.dumps(result))
"#,
        root = kim_root.display(),
        max = max_days,
        strip = strip_days,
    );

    let output = Command::new(&python)
        .arg("-c")
        .arg(&script)
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

#[tauri::command]
pub fn get_app_version() -> String {
    env!("CARGO_PKG_VERSION").to_string()
}

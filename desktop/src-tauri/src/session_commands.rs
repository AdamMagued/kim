//! Session listing, summarization, deletion, and message loading.
//!
//! Extracted from lib.rs (Phase 8 restructure).
//! Public Tauri commands include `list_sessions`,
//! `summarize_session`, `load_session_messages`, and `get_app_version`.

use serde_json;
use std::fs;
use std::io::{BufRead, BufReader};
use std::path::PathBuf;

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
        if !base.exists() {
            continue;
        }
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
        if jsonl_path.is_some() {
            break;
        }
    }

    let path = jsonl_path.ok_or_else(|| format!("Session file not found: {}", session_id))?;

    let file = fs::File::open(&path).map_err(|e| e.to_string())?;
    let reader = BufReader::new(file);

    let mut first_user_text: Option<String> = None;
    let mut last_assistant_text: Option<String> = None;
    let mut touched_files: Vec<String> = Vec::new();

    for line in reader.lines().map_while(Result::ok) {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let value: serde_json::Value = match serde_json::from_str(trimmed) {
            Ok(v) => v,
            Err(_) => continue,
        };

        let (role, content) = if let Some(r) = value.get("role").and_then(|v| v.as_str()) {
            (r.to_string(), value.get("content").cloned())
        } else if value.get("type").and_then(|v| v.as_str()) == Some("message") {
            let msg = value.get("message").unwrap_or(&serde_json::Value::Null);
            let r = msg
                .get("role")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let c = msg.get("blocks").or_else(|| msg.get("content")).cloned();
            (r, c)
        } else {
            continue;
        };

        let text = match &content {
            Some(serde_json::Value::String(s)) => Some(s.clone()),
            Some(serde_json::Value::Array(items)) => {
                let texts: Vec<String> = items
                    .iter()
                    .filter(|b| b.get("type").and_then(|v| v.as_str()) == Some("text"))
                    .filter_map(|b| {
                        b.get("text")
                            .and_then(|v| v.as_str())
                            .map(|s| s.to_string())
                    })
                    .collect();
                if texts.is_empty() {
                    None
                } else {
                    Some(texts.join("\n"))
                }
            }
            _ => None,
        };

        if role == "user" {
            if let Some(ref t) = text {
                let clean = t.trim();
                if !clean.starts_with("[Tool result:")
                    && !clean.is_empty()
                    && first_user_text.is_none()
                {
                    first_user_text =
                        Some(clean.strip_prefix("Task: ").unwrap_or(clean).to_string());
                }
            }
        } else if role == "assistant" {
            if let Some(ref t) = text {
                let clean = t.trim().trim_start_matches("TASK_COMPLETE:").trim();
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
                                    if let Ok(parsed) = serde_json::from_str::<serde_json::Value>(s)
                                    {
                                        path_str = parsed
                                            .get("path")
                                            .or_else(|| parsed.get("file_path"))
                                            .and_then(|v| v.as_str())
                                            .map(|s| s.to_string());
                                    }
                                } else if let Some(obj) = input_val.as_object() {
                                    path_str = obj
                                        .get("path")
                                        .or_else(|| obj.get("file_path"))
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
                let (canon_candidate, canon_dir) =
                    match (candidate.canonicalize(), date_dir.canonicalize()) {
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
            let (canon_candidate, canon_dir) =
                match (candidate.canonicalize(), date_dir.canonicalize()) {
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
    let title = v
        .get("title")
        .and_then(|x| x.as_str())
        .map(|s| s.to_string());
    let pinned = v.get("pinned").and_then(|x| x.as_bool()).unwrap_or(false);
    (title, pinned)
}

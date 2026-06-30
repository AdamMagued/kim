//! Codex (Code) project management — lists sessions grouped by project dir + git branch.
//!
//! Extracted from lib.rs (Phase 8 restructure).
//! Public Tauri commands: `list_codex_projects`, `open_in_finder`.

use crate::data_io::unix_secs_to_utc_iso;
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct CodexSession {
    pub session_id: String,
    /// YYYY-MM-DD bucket under <project>/.codex/sessions.
    pub date: String,
    /// ISO timestamp (UTC) derived from file modified time.
    pub updated_at: String,
    pub message_count: usize,
    pub summary: Option<String>,
    pub title: String,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct CodexBranch {
    pub name: String,
    pub sessions: Vec<CodexSession>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct CodexProject {
    pub path: String,
    pub name: String,
    pub current_branch: String,
    pub branches: Vec<CodexBranch>,
}

fn read_git_branch(project_path: &Path) -> String {
    let head = project_path.join(".git").join("HEAD");
    if let Ok(content) = fs::read_to_string(&head) {
        let s = content.trim();
        if let Some(branch) = s.strip_prefix("ref: refs/heads/") {
            return branch.to_string();
        }
        if s.len() >= 8 {
            return s[..8].to_string();
        }
    }
    "main".to_string()
}

/// List Codex sessions for explicitly-added project paths.
/// Scans <project>/.codex/sessions/ — NEVER ~/.claude/projects/.
#[tauri::command]
pub async fn list_codex_projects(project_paths: Vec<String>) -> Result<Vec<CodexProject>, String> {
    let mut result: Vec<CodexProject> = Vec::new();

    for raw_path in project_paths {
        let project_path = PathBuf::from(&raw_path);
        if !project_path.exists() {
            continue;
        }

        let name = project_path
            .file_name()
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_else(|| raw_path.clone());

        let current_branch = read_git_branch(&project_path);

        let codex_sessions_dir = project_path.join(".codex").join("sessions");
        let sessions = if codex_sessions_dir.exists() {
            read_project_sessions(&codex_sessions_dir)
        } else {
            vec![]
        };

        let branches = if sessions.is_empty() {
            vec![]
        } else {
            vec![CodexBranch {
                name: current_branch.clone(),
                sessions,
            }]
        };

        result.push(CodexProject {
            path: raw_path,
            name,
            current_branch,
            branches,
        });
    }

    Ok(result)
}

/// Open a filesystem path in the OS file manager.
#[tauri::command]
pub async fn open_in_finder(path: String) -> Result<(), String> {
    let p = PathBuf::from(&path);
    if !p.exists() {
        return Err(format!("Path does not exist: {path}"));
    }

    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("open")
            .arg(&path)
            .spawn()
            .map_err(|e| e.to_string())?;
        Ok(())
    }
    #[cfg(target_os = "windows")]
    {
        std::process::Command::new("explorer")
            .arg(&path)
            .spawn()
            .map_err(|e| e.to_string())?;
        Ok(())
    }
    #[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
    {
        std::process::Command::new("xdg-open")
            .arg(&path)
            .spawn()
            .map_err(|e| e.to_string())?;
        Ok(())
    }
}

fn read_project_sessions(dir: &Path) -> Vec<CodexSession> {
    let mut sessions = Vec::new();
    let Ok(date_entries) = fs::read_dir(dir) else {
        return sessions;
    };

    let mut date_dirs: Vec<_> = date_entries
        .filter_map(|e| e.ok())
        .filter(|e| e.file_type().map(|t| t.is_dir()).unwrap_or(false))
        .collect();
    date_dirs.sort_by_key(|b| std::cmp::Reverse(b.file_name()));

    for date_entry in date_dirs {
        let date = date_entry.file_name().to_string_lossy().to_string();
        let date_path = date_entry.path();
        let Ok(file_entries) = fs::read_dir(&date_path) else {
            continue;
        };

        let mut files: Vec<_> = file_entries
            .filter_map(|e| e.ok())
            .filter(|e| {
                let n = e.file_name();
                let s = n.to_string_lossy();
                s.ends_with(".jsonl") && !s.contains(".summary")
            })
            .collect();
        files.sort_by_key(|b| std::cmp::Reverse(b.file_name()));

        for fe in files {
            if sessions.len() >= 50 {
                return sessions;
            }
            let session_id = fe
                .path()
                .file_stem()
                .unwrap_or_default()
                .to_string_lossy()
                .to_string();
            let modified_secs = fe
                .metadata()
                .ok()
                .and_then(|m| m.modified().ok())
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_secs())
                .unwrap_or(0);
            let modified_at = unix_secs_to_utc_iso(modified_secs);
            let message_count = crate::count_lines(&fe.path()).unwrap_or(0);
            let summary_path = date_path.join(format!("{}.summary.txt", session_id));
            let summary = if summary_path.exists() {
                fs::read_to_string(&summary_path)
                    .ok()
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty())
            } else {
                None
            };
            let title = crate::infer_session_title(&fe.path(), summary.as_ref(), &session_id);
            sessions.push(CodexSession {
                session_id,
                date: date.clone(),
                updated_at: modified_at,
                message_count,
                summary,
                title,
            });
        }
    }
    sessions
}

fn newest_claw_session_file(project_path: &Path) -> Option<(PathBuf, SystemTime)> {
    let sessions_root = project_path.join(".claw").join("sessions");
    let entries = fs::read_dir(&sessions_root).ok()?;
    let mut newest: Option<(PathBuf, SystemTime)> = None;

    for entry in entries.filter_map(|e| e.ok()) {
        let path = entry.path();
        if path.is_dir() {
            let Ok(files) = fs::read_dir(&path) else {
                continue;
            };
            for file_entry in files.filter_map(|e| e.ok()) {
                let file_path = file_entry.path();
                let Some(name) = file_path.file_name().and_then(|n| n.to_str()) else {
                    continue;
                };
                if !name.ends_with(".jsonl") || name.contains(".summary") {
                    continue;
                }
                let modified = file_entry
                    .metadata()
                    .and_then(|m| m.modified())
                    .unwrap_or(SystemTime::UNIX_EPOCH);
                let is_newest = newest
                    .as_ref()
                    .map(|(_, previous)| modified > *previous)
                    .unwrap_or(true);
                if is_newest {
                    newest = Some((file_path, modified));
                }
            }
        } else {
            let Some(name) = path.file_name().and_then(|n| n.to_str()) else {
                continue;
            };
            if !name.ends_with(".jsonl") || name.contains(".summary") {
                continue;
            }
            let modified = entry
                .metadata()
                .and_then(|m| m.modified())
                .unwrap_or(SystemTime::UNIX_EPOCH);
            let is_newest = newest
                .as_ref()
                .map(|(_, previous)| modified > *previous)
                .unwrap_or(true);
            if is_newest {
                newest = Some((path, modified));
            }
        }
    }

    newest
}

fn date_for_system_time(time: SystemTime) -> String {
    let secs = time
        .duration_since(SystemTime::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    unix_secs_to_utc_iso(secs)
        .get(0..10)
        .map(str::to_string)
        .unwrap_or_else(crate::chrono_like_today)
}

pub(crate) fn mirror_latest_claw_session_to_codex(project_path: &Path) -> Option<PathBuf> {
    let (src, modified) = newest_claw_session_file(project_path)?;
    let filename = src.file_name()?.to_os_string();
    let date = date_for_system_time(modified);
    let dest_dir = project_path.join(".codex").join("sessions").join(date);
    fs::create_dir_all(&dest_dir).ok()?;
    let dest = dest_dir.join(filename);

    let should_copy = match fs::metadata(&dest).and_then(|m| m.modified()) {
        Ok(dest_modified) => modified > dest_modified,
        Err(_) => true,
    };
    if should_copy {
        fs::copy(&src, &dest).ok()?;
    }

    Some(dest)
}

pub(crate) fn newest_codex_session(project_path: &Path) -> Option<crate::CompletedCodexSession> {
    let sessions_dir = project_path.join(".codex").join("sessions");
    let date_entries = fs::read_dir(&sessions_dir).ok()?;
    let mut newest: Option<(SystemTime, PathBuf, String, String)> = None;

    for date_entry in date_entries.filter_map(|e| e.ok()) {
        let date_path = date_entry.path();
        if !date_path.is_dir() {
            continue;
        }
        let date = date_entry.file_name().to_string_lossy().to_string();
        let Ok(file_entries) = fs::read_dir(&date_path) else {
            continue;
        };
        for file_entry in file_entries.filter_map(|e| e.ok()) {
            let path = file_entry.path();
            let name = file_entry.file_name().to_string_lossy().to_string();
            if !name.ends_with(".jsonl") || name.contains(".summary") {
                continue;
            }
            let modified = file_entry
                .metadata()
                .and_then(|m| m.modified())
                .unwrap_or(SystemTime::UNIX_EPOCH);
            let is_newest = newest
                .as_ref()
                .map(|(prev, _, _, _)| modified > *prev)
                .unwrap_or(true);
            if is_newest {
                let session_id = path
                    .file_stem()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .to_string();
                newest = Some((modified, path, date.clone(), session_id));
            }
        }
    }

    let (_, path, date, session_id) = newest?;
    let summary_path = sessions_dir
        .join(&date)
        .join(format!("{}.summary.txt", session_id));
    let summary = if summary_path.exists() {
        fs::read_to_string(summary_path)
            .ok()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
    } else {
        None
    };
    let message_count = crate::count_lines(&path).unwrap_or(0);
    let title = crate::infer_session_title(&path, summary.as_ref(), &session_id);
    let project_path_str = project_path.to_string_lossy().to_string();

    Some(crate::CompletedCodexSession {
        session_key: format!("codex:{}:{}", date, session_id),
        session_id,
        title,
        date,
        message_count,
        has_summary: summary.is_some(),
        summary,
        session_type: "codex".to_string(),
        project_path: project_path_str,
    })
}

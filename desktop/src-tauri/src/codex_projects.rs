//! Codex (Code) project management — lists sessions grouped by project dir + git branch.
//!
//! Extracted from lib.rs (Phase 8 restructure).
//! Public Tauri commands: `list_codex_projects`, `add_code_project`,
//! `remove_code_project`, `open_in_finder`.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::SystemTime;
use serde::{Deserialize, Serialize};
use crate::account::{KimAccount, account_path};
use crate::data_io::unix_secs_to_utc_iso;

/// Serialises all read-modify-write access to account.json within this process.
static ACCOUNT_FILE_LOCK: Mutex<()> = Mutex::new(());

/// Write `account` atomically: serialise to a sibling `.tmp` file, then rename.
/// The rename is atomic on POSIX (same filesystem); on Windows it is a best-effort
/// replace via `fs::rename` (atomic enough for our use-case given the mutex guard).
fn write_account_atomic(acct_path: &Path, account: &KimAccount) -> Result<(), String> {
    let json = serde_json::to_string_pretty(account).map_err(|e| e.to_string())?;
    let tmp_path = acct_path.with_extension("json.tmp");
    fs::write(&tmp_path, &json).map_err(|e| e.to_string())?;
    fs::rename(&tmp_path, acct_path).map_err(|e| {
        // Best-effort cleanup; ignore secondary error.
        let _ = fs::remove_file(&tmp_path);
        e.to_string()
    })?;
    Ok(())
}

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

/// Add a project root path to the account's code_projects list.
#[tauri::command]
pub async fn add_code_project(path: String) -> Result<Vec<String>, String> {
    let acct_path = account_path();
    let _guard = ACCOUNT_FILE_LOCK.lock().map_err(|e| e.to_string())?;

    let mut account: KimAccount = if acct_path.exists() {
        let raw = fs::read_to_string(&acct_path).map_err(|e| e.to_string())?;
        serde_json::from_str(&raw).map_err(|e| e.to_string())?
    } else {
        return Err("No account found".to_string());
    };

    let canonical = PathBuf::from(&path)
        .canonicalize()
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or(path);

    if !account.code_projects.contains(&canonical) {
        account.code_projects.push(canonical);
    }

    write_account_atomic(&acct_path, &account)?;
    Ok(account.code_projects)
}

/// Remove a project root path from the account's code_projects list.
#[tauri::command]
pub async fn remove_code_project(path: String) -> Result<Vec<String>, String> {
    let acct_path = account_path();
    let _guard = ACCOUNT_FILE_LOCK.lock().map_err(|e| e.to_string())?;

    let mut account: KimAccount = if acct_path.exists() {
        let raw = fs::read_to_string(&acct_path).map_err(|e| e.to_string())?;
        serde_json::from_str(&raw).map_err(|e| e.to_string())?
    } else {
        return Err("No account found".to_string());
    };

    let canonical = PathBuf::from(&path)
        .canonicalize()
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|_| path.clone());
    account.code_projects.retain(|p| p != &canonical && p != &path);

    write_account_atomic(&acct_path, &account)?;
    Ok(account.code_projects)
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
        return Ok(());
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
    let Ok(date_entries) = fs::read_dir(dir) else { return sessions };

    let mut date_dirs: Vec<_> = date_entries
        .filter_map(|e| e.ok())
        .filter(|e| e.file_type().map(|t| t.is_dir()).unwrap_or(false))
        .collect();
    date_dirs.sort_by_key(|b| std::cmp::Reverse(b.file_name()));

    for date_entry in date_dirs {
        let date = date_entry.file_name().to_string_lossy().to_string();
        let date_path = date_entry.path();
        let Ok(file_entries) = fs::read_dir(&date_path) else { continue };

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
            let session_id = fe.path()
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
            let Ok(files) = fs::read_dir(&path) else { continue };
            for file_entry in files.filter_map(|e| e.ok()) {
                let file_path = file_entry.path();
                let Some(name) = file_path.file_name().and_then(|n| n.to_str()) else { continue };
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
            let Some(name) = path.file_name().and_then(|n| n.to_str()) else { continue };
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

#[cfg(test)]
mod codex_projects_tests {
    use super::*;
    use crate::account::KimAccount;

    fn minimal_account() -> KimAccount {
        KimAccount {
            display_name: "Test User".to_string(),
            github_username: None,
            github_token: None,
            github_avatar_url: None,
            gist_id: None,
            created_at: "2024-01-01T00:00:00Z".to_string(),
            code_projects: vec![],
            google_accounts: vec![],
            google_active_account: None,
            google_api_account: None,
        }
    }

    /// write_account_atomic writes valid JSON that round-trips back to an equal KimAccount.
    #[test]
    fn write_account_atomic_produces_valid_json() {
        let dir = tempfile::tempdir().expect("failed to create temp dir");
        let acct_path = dir.path().join("account.json");
        let account = minimal_account();

        write_account_atomic(&acct_path, &account).expect("write_account_atomic failed");

        assert!(acct_path.exists(), "account.json should exist after write");
        let raw = std::fs::read_to_string(&acct_path).expect("failed to read account.json");
        let parsed: KimAccount =
            serde_json::from_str(&raw).expect("written file is not valid JSON / not a KimAccount");

        assert_eq!(parsed.display_name, account.display_name);
        assert_eq!(parsed.created_at, account.created_at);
        assert_eq!(parsed.code_projects, account.code_projects);
    }

    /// After a successful write_account_atomic the sibling .json.tmp file must not exist.
    #[test]
    fn no_tmp_file_left_on_success() {
        let dir = tempfile::tempdir().expect("failed to create temp dir");
        let acct_path = dir.path().join("account.json");
        let account = minimal_account();

        write_account_atomic(&acct_path, &account).expect("write_account_atomic failed");

        let tmp_path = acct_path.with_extension("json.tmp");
        assert!(
            !tmp_path.exists(),
            ".json.tmp sibling should not exist after successful write"
        );
    }

    /// Calling write_account_atomic twice replaces previous contents (last write wins).
    #[test]
    fn overwrites_existing_file() {
        let dir = tempfile::tempdir().expect("failed to create temp dir");
        let acct_path = dir.path().join("account.json");

        let mut account_v1 = minimal_account();
        account_v1.display_name = "Version One".to_string();
        write_account_atomic(&acct_path, &account_v1).expect("first write failed");

        let mut account_v2 = minimal_account();
        account_v2.display_name = "Version Two".to_string();
        account_v2.code_projects = vec!["/some/project".to_string()];
        write_account_atomic(&acct_path, &account_v2).expect("second write failed");

        let raw = std::fs::read_to_string(&acct_path).expect("failed to read account.json");
        let parsed: KimAccount = serde_json::from_str(&raw).expect("not valid JSON");

        assert_eq!(
            parsed.display_name, "Version Two",
            "second write should overwrite the first"
        );
        assert_eq!(
            parsed.code_projects,
            vec!["/some/project".to_string()],
            "code_projects from second write should be present"
        );
    }
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
        let Ok(file_entries) = fs::read_dir(&date_path) else { continue };
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
    let summary_path = sessions_dir.join(&date).join(format!("{}.summary.txt", session_id));
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

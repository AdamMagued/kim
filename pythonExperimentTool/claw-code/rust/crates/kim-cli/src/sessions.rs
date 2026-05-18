use std::fs;
use std::path::{Path, PathBuf};

use serde_json::Value;

use crate::{MessageRole, UiMessage};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionEntry {
    pub id: String,
    pub label: String,
    pub preview: String,
    pub path: PathBuf,
}

pub fn discover_sessions() -> Vec<SessionEntry> {
    let mut roots = Vec::new();
    if let Some(repo_root) = find_kim_repo_root() {
        roots.push(repo_root.join("kim_sessions"));
        roots.push(repo_root.join("sessions"));
        roots.push(repo_root.join(".claw/sessions"));
        roots.push(repo_root.join("desktop/.claw/sessions"));
    }
    if let Ok(cwd) = std::env::current_dir() {
        roots.push(cwd.join(".claw/sessions"));
        add_ancestor_session_roots(&cwd, &mut roots);
    }
    if let Some(home) = dirs::home_dir() {
        roots.push(home.join(".kim/sessions"));
        roots.push(home.join(".claw/sessions"));
        roots.push(home.join("Desktop/kimFork/kim-pro/kim_sessions"));
        roots.push(home.join("Desktop/kimFork/kim-pro/sessions"));
        roots.push(home.join("Desktop/kimFork/kim-pro/.claw/sessions"));
        roots.push(home.join("Desktop/kimFork/kim-pro/desktop/.claw/sessions"));
    }

    let mut sessions = Vec::new();
    for root in roots {
        collect_jsonl_sessions(&root, &mut sessions);
    }
    sessions.sort_by(|left, right| {
        modified_time(&right.path)
            .cmp(&modified_time(&left.path))
            .then_with(|| left.label.cmp(&right.label))
    });
    sessions.dedup_by(|left, right| left.path == right.path);
    sessions.truncate(60);
    sessions
}

pub fn discover_project_sessions() -> Vec<SessionEntry> {
    let mut sessions = Vec::new();
    if let Ok(cwd) = std::env::current_dir() {
        for root in [
            cwd.join("kim_sessions"),
            cwd.join("sessions"),
            cwd.join(".kim/sessions"),
            cwd.join(".claw/sessions"),
        ] {
            collect_jsonl_sessions(&root, &mut sessions);
        }
    }
    sessions.sort_by(|left, right| {
        modified_time(&right.path)
            .cmp(&modified_time(&left.path))
            .then_with(|| left.label.cmp(&right.label))
    });
    sessions.dedup_by(|left, right| left.path == right.path);
    sessions.truncate(60);
    sessions
}

pub fn find_session_by_id(id: &str) -> Option<SessionEntry> {
    discover_sessions()
        .into_iter()
        .find(|session| session.id == id || session.label.ends_with(id))
}

pub fn load_session_messages(path: &Path) -> Result<Vec<UiMessage>, String> {
    let raw = fs::read_to_string(path)
        .map_err(|error| format!("Could not read {}: {error}", path.display()))?;
    let mut messages = Vec::new();
    for line in raw.lines() {
        let Ok(value) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        let Some(role) = value.get("role").and_then(Value::as_str) else {
            continue;
        };
        let Some(content) =
            content_text(value.get("content")).and_then(|text| display_message_text(&text))
        else {
            continue;
        };
        let message_role = match role {
            "user" => MessageRole::User,
            "assistant" => MessageRole::Assistant,
            "system" | "compact_summary" => MessageRole::System,
            _ => continue,
        };
        messages.push(UiMessage {
            role: message_role,
            content,
        });
    }
    if messages.is_empty() {
        Err(format!("No displayable messages in {}.", path.display()))
    } else {
        Ok(messages)
    }
}

fn collect_jsonl_sessions(root: &Path, sessions: &mut Vec<SessionEntry>) {
    if !root.exists() {
        return;
    }
    if root.extension().and_then(|ext| ext.to_str()) == Some("jsonl") {
        if let Some(entry) = session_entry(root) {
            sessions.push(entry);
        }
        return;
    }
    let Ok(entries) = fs::read_dir(root) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            collect_jsonl_sessions(&path, sessions);
        } else if path.extension().and_then(|ext| ext.to_str()) == Some("jsonl") {
            if let Some(entry) = session_entry(&path) {
                sessions.push(entry);
            }
        }
    }
}

fn session_entry(path: &Path) -> Option<SessionEntry> {
    let id = path.file_stem()?.to_string_lossy().to_string();
    let date = session_date(path);
    let preview = preview_for_session(path);
    Some(SessionEntry {
        label: title_for_session(&preview, &date),
        id,
        preview: date,
        path: path.to_path_buf(),
    })
}

fn preview_for_session(path: &Path) -> String {
    let Ok(raw) = fs::read_to_string(path) else {
        return "(unreadable)".to_string();
    };
    for line in raw.lines().take(80) {
        let Ok(value) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        if value.get("role").and_then(Value::as_str) != Some("user") {
            continue;
        }
        if let Some(content) = content_text(value.get("content")) {
            if let Some(cleaned) = clean_message_text(&content) {
                return truncate(&cleaned, 46);
            }
        }
    }
    "Untitled conversation".to_string()
}

fn content_text(value: Option<&Value>) -> Option<String> {
    match value? {
        Value::String(text) => Some(text.clone()),
        Value::Array(items) => {
            let mut out = Vec::new();
            for item in items {
                if let Some(text) = item.get("text").and_then(Value::as_str) {
                    out.push(text.to_string());
                } else if let Some(text) = item.as_str() {
                    out.push(text.to_string());
                }
            }
            if out.is_empty() {
                None
            } else {
                Some(out.join("\n"))
            }
        }
        _ => None,
    }
}

fn clean_message_text(text: &str) -> Option<String> {
    let trimmed = text.trim();
    if trimmed.is_empty()
        || trimmed.starts_with("[Tool result:")
        || trimmed.starts_with('{')
        || trimmed.starts_with("TASK_COMPLETE:")
    {
        return None;
    }
    let without_task = trimmed
        .strip_prefix("Task:")
        .or_else(|| trimmed.strip_prefix("task:"))
        .unwrap_or(trimmed)
        .trim();
    let compact = without_task
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    (!compact.is_empty()).then_some(compact)
}

pub fn display_message_text(text: &str) -> Option<String> {
    clean_message_text(text)
}

fn session_date(path: &Path) -> String {
    path.parent().and_then(Path::file_name).map_or_else(
        || "session".to_string(),
        |date| date.to_string_lossy().to_string(),
    )
}

fn title_for_session(preview: &str, date: &str) -> String {
    if preview == "Untitled conversation" {
        format!("{date} · Untitled")
    } else {
        preview.to_string()
    }
}

fn truncate(text: &str, max_chars: usize) -> String {
    let mut chars = text.chars();
    let truncated = chars.by_ref().take(max_chars).collect::<String>();
    if chars.next().is_some() {
        format!("{truncated}…")
    } else {
        truncated
    }
}

fn find_kim_repo_root() -> Option<PathBuf> {
    let mut dir = std::env::current_dir().ok()?;
    loop {
        if dir.join("kim_sessions").exists() && dir.join("orchestrator").exists() {
            return Some(dir);
        }
        if !dir.pop() {
            return None;
        }
    }
}

fn add_ancestor_session_roots(start: &Path, roots: &mut Vec<PathBuf>) {
    let mut dir = start.to_path_buf();
    loop {
        roots.push(dir.join("kim_sessions"));
        roots.push(dir.join("sessions"));
        roots.push(dir.join(".claw/sessions"));
        roots.push(dir.join("desktop/.claw/sessions"));
        if !dir.pop() {
            break;
        }
    }
}

fn modified_time(path: &Path) -> std::time::SystemTime {
    fs::metadata(path)
        .and_then(|metadata| metadata.modified())
        .unwrap_or(std::time::SystemTime::UNIX_EPOCH)
}

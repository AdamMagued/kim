use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::json;
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
    let sessions = discover_sessions();
    if matches!(id, "latest" | "last" | "recent") {
        return sessions.into_iter().next();
    }
    sessions
        .into_iter()
        .find(|session| session.id == id || session.label.ends_with(id))
}

pub fn save_session_messages(session_id: &str, messages: &[UiMessage]) -> Result<PathBuf, String> {
    let root = dirs::home_dir()
        .ok_or_else(|| "Could not find a home directory for Kim sessions.".to_string())?
        .join(".kim")
        .join("sessions");
    fs::create_dir_all(&root)
        .map_err(|error| format!("Could not create {}: {error}", root.display()))?;

    let safe_id = sanitize_session_id(session_id);
    let path = root.join(format!("{safe_id}.jsonl"));
    let now_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_millis());
    let mut raw = String::new();
    for message in messages {
        let Some(role) = persisted_role(message.role) else {
            continue;
        };
        let value = json!({
            "type": "message",
            "role": role,
            "content": message.content,
            "timestamp_ms": now_ms,
        });
        let line = serde_json::to_string(&value)
            .map_err(|error| format!("Could not encode session message: {error}"))?;
        raw.push_str(&line);
        raw.push('\n');
    }
    fs::write(&path, raw)
        .map_err(|error| format!("Could not write {}: {error}", path.display()))?;
    Ok(path)
}

fn persisted_role(role: MessageRole) -> Option<&'static str> {
    match role {
        MessageRole::User => Some("user"),
        MessageRole::Assistant => Some("assistant"),
        MessageRole::System => Some("system"),
        MessageRole::Error | MessageRole::Reasoning => None,
    }
}

fn sanitize_session_id(session_id: &str) -> String {
    let cleaned = session_id
        .chars()
        .filter(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '-' | '_'))
        .collect::<String>();
    if cleaned.is_empty() {
        "session".to_string()
    } else {
        cleaned
    }
}

pub fn load_session_messages(path: &Path) -> Result<Vec<UiMessage>, String> {
    let raw = fs::read_to_string(path)
        .map_err(|error| format!("Could not read {}: {error}", path.display()))?;
    let mut messages = Vec::new();
    for line in raw.lines() {
        let Ok(value) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        let role = value.get("role").and_then(Value::as_str);
        let record_type = value.get("type").and_then(Value::as_str);
        let compact_summary = match (role, record_type) {
            (Some("compact_summary"), _) | (_, Some("compaction")) => {
                value.get("summary").and_then(Value::as_str)
            }
            _ => None,
        };
        let Some(content) = compact_summary
            .map(ToOwned::to_owned)
            .or_else(|| content_text(value.get("content")))
            .and_then(|text| display_message_text(&text))
        else {
            continue;
        };
        let message_role = match role {
            Some("user") => MessageRole::User,
            Some("assistant") => MessageRole::Assistant,
            Some("system" | "compact_summary") | None if record_type == Some("compaction") => {
                MessageRole::System
            }
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
    let preview = preview_for_session(path);
    let context = session_context(path, &id);
    let label = if preview == "Untitled conversation" {
        // Use a readable timestamp rather than the raw hash/id as the title.
        let ts = readable_modified_time(path);
        format!("{context} · {ts}")
    } else {
        preview.clone()
    };
    Some(SessionEntry {
        label,
        id,
        preview: context,
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
    let trimmed = text.trim();
    if trimmed.is_empty()
        || trimmed.starts_with("[Tool result:")
        || trimmed.starts_with('{')
    {
        return None;
    }

    let cleaned = if let Some(stripped) = trimmed.strip_prefix("TASK_COMPLETE:") {
        stripped.trim().to_string()
    } else if let Some(stripped) = trimmed.strip_prefix("task_complete:") {
        stripped.trim().to_string()
    } else {
        trimmed.to_string()
    };

    if cleaned.is_empty() {
        None
    } else {
        Some(cleaned)
    }
}

fn session_context(path: &Path, _id: &str) -> String {
    let parent = path.parent().and_then(Path::file_name).map_or_else(
        || "saved chat".to_string(),
        |name| name.to_string_lossy().to_string(),
    );
    if matches!(
        parent.as_str(),
        "sessions" | "kim_sessions" | ".kim" | ".claw"
    ) {
        "saved chat".to_string()
    } else {
        parent
    }
}

fn readable_modified_time(path: &Path) -> String {
    use std::time::UNIX_EPOCH;
    let Ok(meta) = std::fs::metadata(path) else {
        return "saved chat".to_string();
    };
    let Ok(modified) = meta.modified() else {
        return "saved chat".to_string();
    };
    let secs = modified
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    // Format as "May 18 14:23" — no external crate needed.
    let minutes = secs / 60;
    let hours = (minutes / 60) % 24;
    let day_of_year = (secs / 86400) % 365;
    let month_starts: [u64; 12] = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334];
    let month_names = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    let month_idx = month_starts
        .iter()
        .rposition(|&start| day_of_year >= start)
        .unwrap_or(0);
    let day = day_of_year - month_starts[month_idx] + 1;
    let hour = hours % 24;
    let min = minutes % 60;
    format!("{} {} {:02}:{:02}", month_names[month_idx], day, hour, min)
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

pub(crate) fn find_kim_repo_root() -> Option<PathBuf> {
    // 1. Environment override wins (explicit user intent).
    if let Ok(env_root) = std::env::var("KIM_PROJECT_ROOT") {
        let p = PathBuf::from(env_root);
        if p.exists() && p.join("orchestrator").join("agent.py").exists() {
            return Some(p);
        }
    }

    // 2. Compile-time baked path walk-up from CARGO_MANIFEST_DIR.
    // Since we are compiling/running on the same machine/source-tree, this is extremely reliable.
    let manifest_dir = env!("CARGO_MANIFEST_DIR");
    let mut manifest_path = PathBuf::from(manifest_dir);
    loop {
        if manifest_path.join("orchestrator").join("agent.py").exists() {
            return Some(manifest_path);
        }
        if !manifest_path.pop() {
            break;
        }
    }

    // 3. ~/.kim_root — written by install.sh
    if let Some(home) = dirs::home_dir() {
        let root_file = home.join(".kim_root");
        if let Ok(contents) = std::fs::read_to_string(&root_file) {
            let p = PathBuf::from(contents.trim());
            if p.exists() && p.join("orchestrator").join("agent.py").exists() {
                return Some(p);
            }
        }
    }

    // 4. Walk up from current executable
    if let Ok(exe) = std::env::current_exe() {
        for ancestor in exe.ancestors() {
            if ancestor.join("orchestrator").join("agent.py").exists() {
                return Some(ancestor.to_path_buf());
            }
        }
    }

    // 5. Walk up from the current working directory
    if let Ok(mut dir) = std::env::current_dir() {
        loop {
            if dir.join("orchestrator").join("agent.py").exists() {
                return Some(dir);
            }
            if !dir.pop() {
                break;
            }
        }
    }

    // 6. ~/.kim
    if let Some(home) = dirs::home_dir() {
        let p = home.join(".kim");
        if p.exists() && p.join("orchestrator").join("agent.py").exists() {
            return Some(p);
        }
    }

    None
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

#[cfg(test)]
mod tests {
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

    use super::load_session_messages;
    use crate::MessageRole;

    #[test]
    fn loads_runtime_compaction_summary_records() {
        let path = std::env::temp_dir().join(format!(
            "kim-cli-compaction-{}.jsonl",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("clock should be after epoch")
                .as_nanos()
        ));
        fs::write(
            &path,
            r#"{"type":"compaction","summary":"Earlier work was compacted."}"#,
        )
        .expect("session fixture should write");

        let messages = load_session_messages(&path).expect("summary should load");
        let _ = fs::remove_file(&path);

        assert_eq!(messages.len(), 1);
        assert_eq!(messages[0].role, MessageRole::System);
        assert_eq!(messages[0].content, "Earlier work was compacted.");
    }
}

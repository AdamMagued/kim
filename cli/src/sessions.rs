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

    // Primary: ~/.kim/sessions
    if let Some(home) = dirs::home_dir() {
        roots.push(home.join(".kim").join("sessions"));
    }

    // If running from source, also check the repo's session directories.
    if let Some(repo_root) = find_kim_repo_root() {
        roots.push(repo_root.join("kim_sessions"));
        roots.push(repo_root.join("sessions"));
    }

    // Current working directory (project-local sessions).
    if let Ok(cwd) = std::env::current_dir() {
        roots.push(cwd.join(".kim").join("sessions"));
        roots.push(cwd.join("kim_sessions"));
    }

    let mut sessions = Vec::new();
    for root in roots {
        collect_jsonl_sessions(&root, &mut sessions);
    }
    sessions.sort_by(|l, r| {
        modified_time(&r.path)
            .cmp(&modified_time(&l.path))
            .then_with(|| l.label.cmp(&r.label))
    });
    sessions.dedup_by(|l, r| l.path == r.path);
    sessions.truncate(60);
    sessions
}

pub fn discover_project_sessions() -> Vec<SessionEntry> {
    let mut sessions = Vec::new();
    if let Ok(cwd) = std::env::current_dir() {
        for root in [
            cwd.join("kim_sessions"),
            cwd.join("sessions"),
            cwd.join(".kim").join("sessions"),
        ] {
            collect_jsonl_sessions(&root, &mut sessions);
        }
    }
    sessions.sort_by(|l, r| {
        modified_time(&r.path)
            .cmp(&modified_time(&l.path))
            .then_with(|| l.label.cmp(&r.label))
    });
    sessions.dedup_by(|l, r| l.path == r.path);
    sessions.truncate(60);
    sessions
}

pub fn find_session_by_id(id: &str) -> Option<SessionEntry> {
    let sessions = discover_sessions();
    if matches!(id, "latest" | "last" | "recent") {
        return sessions.into_iter().next();
    }
    // F14: match on the session id only — exact first, else a UNIQUE id prefix.
    // The old `label.ends_with(id)` fuzzy match silently resumed whichever
    // newest session's preview text happened to end with the query.
    if let Some(pos) = sessions.iter().position(|s| s.id == id) {
        return sessions.into_iter().nth(pos);
    }
    let mut by_prefix = sessions.into_iter().filter(|s| s.id.starts_with(id));
    let first = by_prefix.next()?;
    if by_prefix.next().is_some() {
        None // ambiguous — refuse rather than resume the wrong session
    } else {
        Some(first)
    }
}

pub fn save_session_messages(session_id: &str, messages: &[UiMessage]) -> Result<PathBuf, String> {
    let root = dirs::home_dir()
        .ok_or_else(|| "Could not find a home directory for Kim sessions.".to_string())?
        .join(".kim")
        .join("sessions");
    save_session_messages_in(&root, session_id, messages)
}

/// Inner implementation — accepts an explicit root so tests can target a temp dir
/// without touching the real `~/.kim/sessions` store.
pub(crate) fn save_session_messages_in(
    root: &Path,
    session_id: &str,
    messages: &[UiMessage],
) -> Result<PathBuf, String> {
    fs::create_dir_all(root).map_err(|e| format!("Could not create {}: {e}", root.display()))?;

    let safe_id = sanitize_session_id(session_id);
    let path = root.join(format!("{safe_id}.jsonl"));

    // #7: two processes resuming the same session id (e.g. `kim --resume
    // <id>` run twice) previously raced this whole tempfile+rename cycle
    // with no coordination — "last renamer wins" non-deterministically, and
    // a slower process holding stale in-memory messages could clobber a
    // faster process's newer save. Hold an advisory cross-process lock (an
    // OS `flock` on a `.lock` sentinel file, scoped to this session id) for
    // the entire write cycle below so concurrent saves serialize instead.
    let mut session_lock = lock_session_file(root, &safe_id)?;
    let _lock_guard = session_lock
        .write()
        .map_err(|e| format!("Could not lock session {safe_id} for saving: {e}"))?;

    let (mut tmp_file, tmp_path, nanos) = create_temp_session_file(root, &safe_id)?;

    let now_ms = nanos / 1_000_000;
    let mut raw = String::new();
    for msg in messages {
        let Some(role) = persisted_role(msg.role) else {
            continue;
        };
        let value = json!({
            "type": "message",
            "role": role,
            "content": msg.content,
            "timestamp_ms": now_ms,
        });
        let line = serde_json::to_string(&value)
            .map_err(|e| format!("Could not encode session message: {e}"))?;
        raw.push_str(&line);
        raw.push('\n');
    }

    // Write to temp, sync to disk, then rename over the final path.
    //
    // POSIX: rename(2) is atomic when src and dst share a filesystem; writing
    // into the same directory guarantees this.
    // Windows: MoveFileExW replaces the destination file; readers see either the
    // complete old or the complete new content, never a partial write.
    //
    // sync_all() before rename ensures that if the OS crashes between rename and
    // the write buffer reaching disk, the file at the final path is not observed
    // as empty or truncated. Worst case after a crash is that the most recent
    // save is lost and the prior valid file is retained.
    {
        use std::io::Write as _;
        tmp_file.write_all(raw.as_bytes()).map_err(|e| {
            let _ = fs::remove_file(&tmp_path);
            format!("Could not write {}: {e}", tmp_path.display())
        })?;
        tmp_file.sync_all().map_err(|e| {
            let _ = fs::remove_file(&tmp_path);
            format!("Could not sync {}: {e}", tmp_path.display())
        })?;
    } // file closed before rename
    fs::rename(&tmp_path, &path).map_err(|e| {
        let _ = fs::remove_file(&tmp_path);
        format!("Could not commit session {}: {e}", path.display())
    })?;
    Ok(path)
}

/// #7: acquire an exclusive advisory lock on `<root>/<safe_id>.lock`,
/// creating the sentinel file if needed. Blocks until acquired — two
/// processes saving the same session id serialize rather than racing the
/// tempfile+rename cycle. The returned guard holds the lock (and keeps the
/// underlying file descriptor open) until dropped; the sentinel file itself
/// is intentionally never cleaned up (same pattern as the Python side's
/// flock-style `cron_store.py` locking) since re-creating it is free and
/// deleting a lock file out from under another lock-holder is a classic
/// TOCTOU bug.
fn lock_session_file(
    root: &Path,
    safe_id: &str,
) -> Result<fd_lock::RwLock<fs::File>, String> {
    let lock_path = root.join(format!("{safe_id}.lock"));
    let lock_file = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(&lock_path)
        .map_err(|e| format!("Could not open lock file {}: {e}", lock_path.display()))?;
    Ok(fd_lock::RwLock::new(lock_file))
}

fn create_temp_session_file(
    root: &Path,
    safe_id: &str,
) -> Result<(fs::File, PathBuf, u128), String> {
    let pid = std::process::id();
    let base_nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_nanos());
    for attempt in 0..16u8 {
        let nanos = base_nanos + attempt as u128;
        let tmp_path = root.join(format!("{safe_id}.{nanos}.{pid}.tmp"));
        match fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&tmp_path)
        {
            Ok(file) => return Ok((file, tmp_path, nanos)),
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(e) => return Err(format!("Could not create {}: {e}", tmp_path.display())),
        }
    }
    Err(format!(
        "Could not create a unique temporary session file for {safe_id} after 16 attempts"
    ))
}

fn persisted_role(role: MessageRole) -> Option<&'static str> {
    match role {
        MessageRole::User => Some("user"),
        MessageRole::Assistant => Some("assistant"),
        MessageRole::System => Some("system"),
        MessageRole::Error | MessageRole::Reasoning => None,
    }
}

fn sanitize_session_id(id: &str) -> String {
    let cleaned = id
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_'))
        .collect::<String>();
    if cleaned.is_empty() {
        "session".to_string()
    } else {
        cleaned
    }
}

pub fn load_session_messages(path: &Path) -> Result<Vec<UiMessage>, String> {
    let raw =
        fs::read_to_string(path).map_err(|e| format!("Could not read {}: {e}", path.display()))?;
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
            .and_then(|t| {
                if record_type == Some("message") {
                    // F4: records the CLI itself wrote carry `"type":"message"`
                    // and hold verbatim user/assistant text — it must round-trip
                    // even when it starts with `{` or "[Tool result:". The
                    // prefix heuristic below is only for foreign/agent-internal
                    // JSONL records that lack the marker.
                    let t = t.trim().to_string();
                    (!t.is_empty()).then_some(t)
                } else {
                    display_message_text(&t)
                }
            })
        else {
            continue;
        };
        let message_role = match role {
            Some("user") => MessageRole::User,
            Some("assistant") => MessageRole::Assistant,
            Some("system") => MessageRole::System,
            Some("compact_summary") | None if record_type == Some("compaction") => {
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
    collect_jsonl_sessions_at(root, sessions, 0);
}

fn collect_jsonl_sessions_at(root: &Path, sessions: &mut Vec<SessionEntry>, depth: usize) {
    // F17: cap recursion so a deep/self-referencing tree under a session root
    // can't stall every mode switch. Real layouts are ≤ 2 levels (date dirs).
    const MAX_DEPTH: usize = 4;
    if depth > MAX_DEPTH || !root.exists() {
        return;
    }
    if root.extension().and_then(|e| e.to_str()) == Some("jsonl") {
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
            collect_jsonl_sessions_at(&path, sessions, depth + 1);
        } else if path.extension().and_then(|e| e.to_str()) == Some("jsonl") {
            if let Some(entry) = session_entry(&path) {
                sessions.push(entry);
            }
        }
    }
}

fn session_entry(path: &Path) -> Option<SessionEntry> {
    let id = path.file_stem()?.to_string_lossy().to_string();
    let preview = preview_for_session(path);
    let context = session_context(path);
    let label = if preview == "Untitled conversation" {
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
    use std::io::Read as _;
    // F17: session files can be MBs and this runs for every session on every
    // mode switch. The preview only needs the first user line — read at most
    // 64 KiB instead of the whole file. (A record cut at the cap fails JSON
    // parsing and is skipped, same as any malformed line.)
    let Ok(file) = fs::File::open(path) else {
        return "(unreadable)".to_string();
    };
    let mut raw_bytes = Vec::new();
    if file.take(64 * 1024).read_to_end(&mut raw_bytes).is_err() {
        return "(unreadable)".to_string();
    }
    let raw = String::from_utf8_lossy(&raw_bytes);
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
            let parts: Vec<String> = items
                .iter()
                .filter_map(|item| {
                    item.get("text")
                        .and_then(Value::as_str)
                        .or_else(|| item.as_str())
                        .map(ToString::to_string)
                })
                .collect();
            if parts.is_empty() {
                None
            } else {
                Some(parts.join("\n"))
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
    let without_prefix = trimmed
        .strip_prefix("Task:")
        .or_else(|| trimmed.strip_prefix("task:"))
        .unwrap_or(trimmed)
        .trim();
    let compact = without_prefix
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    (!compact.is_empty()).then_some(compact)
}

pub fn display_message_text(text: &str) -> Option<String> {
    let trimmed = text.trim();
    if trimmed.is_empty() || trimmed.starts_with("[Tool result:") || trimmed.starts_with('{') {
        return None;
    }
    let cleaned = trimmed
        .strip_prefix("TASK_COMPLETE:")
        .or_else(|| trimmed.strip_prefix("task_complete:"))
        .map(|s| s.trim().to_string())
        .unwrap_or_else(|| trimmed.to_string());
    if cleaned.is_empty() {
        None
    } else {
        Some(cleaned)
    }
}

fn session_context(path: &Path) -> String {
    let parent = path.parent().and_then(Path::file_name).map_or_else(
        || "saved chat".to_string(),
        |n| n.to_string_lossy().to_string(),
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
    format_epoch_secs(secs)
}

/// Convert Unix epoch seconds to a human-readable timestamp.
/// Kept as a pure function so it can be tested without touching the filesystem.
fn format_epoch_secs(secs: u64) -> String {
    let (_, month, day, hour, minute) = epoch_secs_to_calendar(secs);
    let month_names = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    let month_name = month_names[month.saturating_sub(1) as usize];
    format!("{month_name} {day} {hour:02}:{minute:02}")
}

fn epoch_secs_to_calendar(secs: u64) -> (u16, u8, u8, u8, u8) {
    let hour = ((secs / 3600) % 24) as u8;
    let minute = ((secs / 60) % 60) as u8;
    let mut days = secs / 86400;
    let mut year = 1970u16;
    loop {
        let dy = if is_leap_year(year) { 366u64 } else { 365u64 };
        if days < dy {
            break;
        }
        days -= dy;
        year += 1;
    }
    let mut month = 1u8;
    for m in 1u8..=12 {
        let dm = days_in_month(m, year) as u64;
        if days < dm {
            month = m;
            break;
        }
        days -= dm;
    }
    let day = (days + 1) as u8;
    (year, month, day, hour, minute)
}

fn is_leap_year(year: u16) -> bool {
    let y = year as u32;
    (y.is_multiple_of(4) && !y.is_multiple_of(100)) || y.is_multiple_of(400)
}

fn days_in_month(month: u8, year: u16) -> u8 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => {
            if is_leap_year(year) {
                29
            } else {
                28
            }
        }
        _ => 30,
    }
}

pub(crate) fn truncate(text: &str, max: usize) -> String {
    let mut chars = text.chars();
    let truncated = chars.by_ref().take(max).collect::<String>();
    if chars.next().is_some() {
        format!("{truncated}…")
    } else {
        truncated
    }
}

/// Walk up from known locations to find the Kim repo root (has orchestrator/agent.py).
/// Used to discover sessions stored inside the repo and to run the local agent.
pub(crate) fn find_kim_repo_root() -> Option<PathBuf> {
    // 1. Explicit env override.
    if let Ok(env_root) = std::env::var("KIM_PROJECT_ROOT") {
        let p = PathBuf::from(env_root);
        if p.exists() && p.join("orchestrator").join("agent.py").exists() {
            return Some(p);
        }
    }

    // 2. ~/.kim_root written by install.sh
    if let Some(home) = dirs::home_dir() {
        let root_file = home.join(".kim_root");
        if let Ok(contents) = std::fs::read_to_string(&root_file) {
            let p = PathBuf::from(contents.trim());
            if p.exists() && p.join("orchestrator").join("agent.py").exists() {
                return Some(p);
            }
        }
    }

    // 3. Walk up from the current working directory.
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

    // 4. Walk up from the running executable.
    if let Ok(exe) = std::env::current_exe() {
        for ancestor in exe.ancestors() {
            if ancestor.join("orchestrator").join("agent.py").exists() {
                return Some(ancestor.to_path_buf());
            }
        }
    }

    None
}

fn modified_time(path: &Path) -> std::time::SystemTime {
    fs::metadata(path)
        .and_then(|m| m.modified())
        .unwrap_or(std::time::SystemTime::UNIX_EPOCH)
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

    use super::load_session_messages;
    use crate::MessageRole;

    // ── epoch_secs_to_calendar / format_epoch_secs ───────────────────────────

    #[test]
    fn epoch_calendar_unix_epoch_is_jan_1_1970() {
        let (year, month, day, hour, minute) = super::epoch_secs_to_calendar(0);
        assert_eq!((year, month, day, hour, minute), (1970, 1, 1, 0, 0));
    }

    #[test]
    fn epoch_calendar_accounts_for_leap_years_in_2024() {
        // 2024-02-29 00:00:00 UTC (leap day)
        // Days from 1970-01-01 to 2024-02-29:
        //   54 complete years (1970-2023): non-leap(40)*365 + leap(14)*366 = 19723 days
        //   Jan 2024: 31, Feb 1-28: 28 = 59 more days → total 19723+59 = 19782
        let secs = 19782u64 * 86400;
        let (year, month, day, _, _) = super::epoch_secs_to_calendar(secs);
        assert_eq!((year, month, day), (2024, 2, 29));
    }

    #[test]
    fn epoch_calendar_june_6_2026() {
        // 2026-06-06 00:00:00 UTC
        // Leap years 1970-2025: 1972,1976,1980,1984,1988,1992,1996,2000,2004,2008,2012,2016,2020,2024 = 14
        // Days 1970-2025: 42*365 + 14*366 = 15330 + 5124 = 20454
        // Jan+Feb+Mar+Apr+May+5 days of Jun in 2026 (non-leap): 31+28+31+30+31+5 = 156
        // Total: 20454 + 156 = 20610
        let secs = 20610u64 * 86400;
        let (year, month, day, _, _) = super::epoch_secs_to_calendar(secs);
        assert_eq!((year, month, day), (2026, 6, 6));
    }

    #[test]
    fn format_epoch_secs_june_6_2026_midday() {
        // 2026-06-06 14:30 UTC
        let secs = 20610u64 * 86400 + 14 * 3600 + 30 * 60;
        let s = super::format_epoch_secs(secs);
        assert_eq!(s, "Jun 6 14:30");
    }

    #[test]
    fn format_epoch_secs_dec_31_non_leap_year() {
        // 2025-12-31 23:59 UTC
        // Days 1970-2024 inclusive: 20454 (same calc as above, through 2025 exclusive)
        // 2025 is non-leap: 365 days, first 364 = Jan 1 to Dec 30 → day 364 = Dec 31
        // Actually days to 2025-12-31: 20454 + 364 = 20818
        let secs = 20818u64 * 86400 + 23 * 3600 + 59 * 60;
        let s = super::format_epoch_secs(secs);
        assert_eq!(s, "Dec 31 23:59");
    }

    #[test]
    fn loads_compaction_summary_records() {
        let path = std::env::temp_dir().join(format!(
            "kim-cli-compaction-{}.jsonl",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("clock")
                .as_nanos()
        ));
        fs::write(
            &path,
            r#"{"type":"compaction","summary":"Earlier work was compacted."}"#,
        )
        .expect("fixture write");
        let messages = load_session_messages(&path).expect("should load");
        let _ = fs::remove_file(&path);
        assert_eq!(messages.len(), 1);
        assert_eq!(messages[0].role, MessageRole::System);
        assert_eq!(messages[0].content, "Earlier work was compacted.");
    }

    // ── save_session_messages_in (atomic-write path) ─────────────────────────

    fn unique_test_dir() -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "kim-cli-save-test-{}",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("clock")
                .as_nanos()
        ));
        fs::create_dir_all(&dir).expect("test dir");
        dir
    }

    use crate::UiMessage;

    #[test]
    fn save_and_load_roundtrip_user_and_assistant() {
        let dir = unique_test_dir();
        let messages = vec![
            UiMessage {
                role: MessageRole::User,
                content: "hello".to_string(),
            },
            UiMessage {
                role: MessageRole::Assistant,
                content: "hi there".to_string(),
            },
        ];
        let path = super::save_session_messages_in(&dir, "roundtrip-test", &messages)
            .expect("save should succeed");
        let loaded = load_session_messages(&path).expect("load should succeed");
        let _ = fs::remove_dir_all(&dir);
        assert_eq!(loaded.len(), 2);
        assert_eq!(loaded[0].role, MessageRole::User);
        assert_eq!(loaded[0].content, "hello");
        assert_eq!(loaded[1].role, MessageRole::Assistant);
        assert_eq!(loaded[1].content, "hi there");
    }

    #[test]
    fn save_and_load_roundtrip_json_prefixed_content() {
        // F4: content beginning with `{` or "[Tool result:" written by the CLI
        // itself (type:"message" records) must survive a save→load round-trip.
        // The `{`-prefix heuristic is only for foreign records.
        let dir = unique_test_dir();
        let messages = vec![
            UiMessage {
                role: MessageRole::User,
                content: "{\"a\":1} — why is this invalid JSON5?".to_string(),
            },
            UiMessage {
                role: MessageRole::Assistant,
                content: "{ starts my answer too".to_string(),
            },
            UiMessage {
                role: MessageRole::User,
                content: "[Tool result: looking thing] pasted by a user".to_string(),
            },
        ];
        let path = super::save_session_messages_in(&dir, "json-prefix-test", &messages)
            .expect("save should succeed");
        let loaded = load_session_messages(&path).expect("load should succeed");
        let _ = fs::remove_dir_all(&dir);
        assert_eq!(
            loaded.len(),
            3,
            "no CLI-written message may be dropped on load"
        );
        assert_eq!(loaded[0].content, "{\"a\":1} — why is this invalid JSON5?");
        assert_eq!(loaded[1].content, "{ starts my answer too");
        assert_eq!(
            loaded[2].content,
            "[Tool result: looking thing] pasted by a user"
        );
    }

    #[test]
    fn foreign_records_without_message_type_still_filtered() {
        // Agent-internal JSONL (no type:"message") keeps the old heuristic.
        let path = std::env::temp_dir().join(format!(
            "kim-cli-foreign-{}.jsonl",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("clock")
                .as_nanos()
        ));
        fs::write(
            &path,
            concat!(
                "{\"role\":\"assistant\",\"content\":\"{\\\"internal\\\":true}\"}\n",
                "{\"role\":\"assistant\",\"content\":\"real text\"}\n",
            ),
        )
        .expect("fixture write");
        let messages = load_session_messages(&path).expect("should load");
        let _ = fs::remove_file(&path);
        assert_eq!(messages.len(), 1, "internal JSON record must be filtered");
        assert_eq!(messages[0].content, "real text");
    }

    // ── #7: cross-process save locking ────────────────────────────────────

    #[test]
    fn save_creates_a_lock_sentinel_file() {
        let dir = unique_test_dir();
        let messages = vec![UiMessage {
            role: MessageRole::User,
            content: "hi".to_string(),
        }];
        super::save_session_messages_in(&dir, "lock-sentinel-test", &messages)
            .expect("save should succeed");
        let lock_path = dir.join("lock-sentinel-test.lock");
        assert!(
            lock_path.exists(),
            "expected a .lock sentinel file after saving"
        );
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn concurrent_save_blocks_while_another_holder_has_the_lock() {
        use std::sync::mpsc;
        use std::thread;
        use std::time::Duration;

        let dir = unique_test_dir();
        let safe_id = "lock-blocks-test";

        // Simulate another process already mid-save (holding the lock).
        let mut held_lock = super::lock_session_file(&dir, safe_id).expect("open lock file");
        let guard = held_lock.write().expect("acquire lock");

        let (tx, rx) = mpsc::channel();
        let dir_clone = dir.clone();
        let handle = thread::spawn(move || {
            let messages = vec![UiMessage {
                role: MessageRole::User,
                content: "blocked writer".to_string(),
            }];
            let result = super::save_session_messages_in(&dir_clone, safe_id, &messages);
            let _ = tx.send(());
            result
        });

        // Must NOT complete while another holder still has the lock — this is
        // the "serialize instead of clobbering" guarantee (#7).
        assert!(
            rx.recv_timeout(Duration::from_millis(300)).is_err(),
            "save_session_messages_in must block while another lock holder is active"
        );

        // Releasing the lock lets the blocked save proceed promptly.
        drop(guard);
        drop(held_lock);
        assert!(
            rx.recv_timeout(Duration::from_secs(2)).is_ok(),
            "save_session_messages_in should complete once the lock is released"
        );
        handle
            .join()
            .expect("writer thread panicked")
            .expect("save should succeed after the lock is released");

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn save_leaves_no_tmp_artifact() {
        let dir = unique_test_dir();
        let messages = vec![UiMessage {
            role: MessageRole::User,
            content: "test".to_string(),
        }];
        super::save_session_messages_in(&dir, "no-tmp-test", &messages)
            .expect("save should succeed");
        let tmp_count = fs::read_dir(&dir)
            .expect("read dir")
            .flatten()
            .filter(|e| e.path().extension().and_then(|x| x.to_str()) == Some("tmp"))
            .count();
        let _ = fs::remove_dir_all(&dir);
        assert_eq!(
            tmp_count, 0,
            "no .tmp files should remain after a clean save"
        );
    }

    #[test]
    fn save_over_existing_replaces_without_truncation() {
        let dir = unique_test_dir();
        // Write a large initial session.
        let initial: Vec<UiMessage> = (0..20)
            .flat_map(|i| {
                [
                    UiMessage {
                        role: MessageRole::User,
                        content: format!("question {i}"),
                    },
                    UiMessage {
                        role: MessageRole::Assistant,
                        content: format!("answer {i}"),
                    },
                ]
            })
            .collect();
        super::save_session_messages_in(&dir, "overwrite-test", &initial).expect("initial save");

        // Overwrite with a smaller session (simulates a save after /compact).
        let updated = vec![
            UiMessage {
                role: MessageRole::User,
                content: "only message".to_string(),
            },
            UiMessage {
                role: MessageRole::Assistant,
                content: "only reply".to_string(),
            },
        ];
        super::save_session_messages_in(&dir, "overwrite-test", &updated).expect("overwrite save");

        let path = dir.join("overwrite-test.jsonl");
        let loaded = load_session_messages(&path).expect("load after overwrite");
        let _ = fs::remove_dir_all(&dir);

        // The file must contain exactly the updated content — no stale tail from
        // the old (larger) file that would appear if we wrote in-place and truncated.
        assert_eq!(
            loaded.len(),
            2,
            "overwrite must replace fully, not truncate"
        );
        assert_eq!(loaded[0].content, "only message");
        assert_eq!(loaded[1].content, "only reply");
    }

    #[test]
    fn all_filtered_messages_save_succeeds_but_load_fails() {
        // When every message is a non-persisted role (Error/Reasoning), the
        // resulting file is empty. save should not error; load should reject it.
        let dir = unique_test_dir();
        let messages = vec![
            UiMessage {
                role: MessageRole::Error,
                content: "transient".to_string(),
            },
            UiMessage {
                role: MessageRole::Reasoning,
                content: "thinking".to_string(),
            },
        ];
        let path = super::save_session_messages_in(&dir, "filtered-test", &messages)
            .expect("save of filtered-only messages should not error");
        let load_result = load_session_messages(&path);
        // No .tmp files should remain.
        let tmp_count = fs::read_dir(&dir)
            .expect("read dir")
            .flatten()
            .filter(|e| e.path().extension().and_then(|x| x.to_str()) == Some("tmp"))
            .count();
        let _ = fs::remove_dir_all(&dir);
        assert_eq!(tmp_count, 0, "no .tmp files should remain");
        assert!(
            load_result.is_err(),
            "load of an all-filtered session file must fail with no-displayable-messages"
        );
    }

    #[test]
    fn error_and_reasoning_roles_are_not_persisted() {
        let dir = unique_test_dir();
        let messages = vec![
            UiMessage {
                role: MessageRole::User,
                content: "prompt".to_string(),
            },
            UiMessage {
                role: MessageRole::Error,
                content: "transient error".to_string(),
            },
            UiMessage {
                role: MessageRole::Reasoning,
                content: "thinking...".to_string(),
            },
            UiMessage {
                role: MessageRole::Assistant,
                content: "done".to_string(),
            },
        ];
        let path = super::save_session_messages_in(&dir, "roles-test", &messages).expect("save");
        let loaded = load_session_messages(&path).expect("load");
        let _ = fs::remove_dir_all(&dir);
        assert_eq!(
            loaded.len(),
            2,
            "Error and Reasoning roles must be stripped from persisted JSONL"
        );
        assert_eq!(loaded[0].role, MessageRole::User);
        assert_eq!(loaded[1].role, MessageRole::Assistant);
    }
}

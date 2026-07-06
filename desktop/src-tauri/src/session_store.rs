// session_store.rs — on-disk session store helpers.
// Extracted from lib.rs (file-split restructure) — behavior unchanged.

use crate::*;
use std::fs;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::time::SystemTime;

/// Validate that a user-supplied `session_id` is a safe file-stem:
/// no path separators, no `..`, printable ASCII-ish. Prevents a caller
/// from escaping the per-date directory via `../../etc/passwd` etc.
pub(crate) fn validate_session_id(session_id: &str) -> Result<(), String> {
    if session_id.is_empty() {
        return Err("session_id is empty".to_string());
    }
    if session_id.len() > 128 {
        return Err("session_id is too long".to_string());
    }
    if session_id.contains('/')
        || session_id.contains('\\')
        || session_id.contains("..")
        || session_id.contains('\0')
    {
        return Err("session_id contains illegal characters".to_string());
    }
    // Only allow [A-Za-z0-9._-].
    if !session_id
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '.' || c == '_' || c == '-')
    {
        return Err("session_id contains illegal characters".to_string());
    }
    Ok(())
}

/// Validate that a user-supplied `session_date` is a safe single path
/// component (H-BRIDGE-1). Without this, a bridge caller could pass
/// `session_date="../../../../Users/<u>/.ssh"` and read/write files outside
/// the sessions tree via /v1/browser/meta, /commit-url and /restore.
/// Date buckets are created by `chrono_like_today` as `YYYY-MM-DD`, so the
/// same conservative charset used for session ids is more than enough.
pub(crate) fn validate_session_date(session_date: &str) -> Result<(), String> {
    if session_date.is_empty() {
        return Err("session_date is empty".to_string());
    }
    if session_date.len() > 64 {
        return Err("session_date is too long".to_string());
    }
    if session_date.contains('/')
        || session_date.contains('\\')
        || session_date.contains("..")
        || session_date.contains('\0')
    {
        return Err("session_date contains illegal characters".to_string());
    }
    if !session_date
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '.' || c == '_' || c == '-')
    {
        return Err("session_date contains illegal characters".to_string());
    }
    Ok(())
}

pub(crate) fn browser_session_meta_filename(session_id: &str) -> String {
    format!("{}.browser.json", session_id)
}

pub(crate) fn read_browser_session_meta_from_dir(
    date_dir: &Path,
    session_id: &str,
) -> Option<BrowserSessionMeta> {
    let path = date_dir.join(browser_session_meta_filename(session_id));
    let raw = fs::read_to_string(path).ok()?;
    serde_json::from_str::<BrowserSessionMeta>(&raw).ok()
}

pub(crate) fn write_browser_session_meta_to_dir(
    date_dir: &Path,
    session_id: &str,
    meta: &BrowserSessionMeta,
) -> Result<(), String> {
    fs::create_dir_all(date_dir).map_err(|e| e.to_string())?;
    let path = date_dir.join(browser_session_meta_filename(session_id));
    let tmp_path = date_dir.join(format!("{}.browser.json.tmp", session_id));
    let text = serde_json::to_string_pretty(meta).map_err(|e| e.to_string())?;

    // Atomic write: write to a same-directory temp file, then rename over the
    // target. This prevents partially-written .browser.json files when the UI
    // and kimctl/bridge both commit URL metadata around the same time. The last
    // successful writer wins; callers always merge against the current file
    // before writing.
    fs::write(&tmp_path, text).map_err(|e| e.to_string())?;
    match fs::rename(&tmp_path, &path) {
        Ok(()) => Ok(()),
        Err(e) => {
            #[cfg(target_os = "windows")]
            {
                let _ = &e;
                // Windows rename does not overwrite existing files. Fall back to
                // remove+rename; this is not perfectly atomic, but still avoids
                // exposing a partially-written JSON file.
                if path.exists() {
                    fs::remove_file(&path).map_err(|remove_err| remove_err.to_string())?;
                }
                fs::rename(&tmp_path, &path).map_err(|rename_err| rename_err.to_string())
            }
            #[cfg(not(target_os = "windows"))]
            {
                let _ = fs::remove_file(&tmp_path);
                Err(e.to_string())
            }
        }
    }
}

/// M-STORE-1: serialises the read→modify→write cycle on `.browser.json`
/// session metadata. The UI (session switch) and the bridge (task-end URL
/// commit) can interleave whole-file read-modify-writes; without this lock the
/// second rename discards the first writer's fields (the saved provider thread
/// URL vanished, so the next run opened a fresh thread).
static BROWSER_META_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

/// Read-modify-write a session's `.browser.json` under the process-wide lock.
/// All writers must go through this helper so concurrent commits merge instead
/// of clobbering each other.
pub(crate) fn update_browser_session_meta<F>(
    date_dir: &Path,
    session_id: &str,
    apply: F,
) -> Result<BrowserSessionMeta, String>
where
    F: FnOnce(&mut BrowserSessionMeta) -> Result<(), String>,
{
    let _guard = BROWSER_META_LOCK
        .lock()
        .map_err(|_| "browser meta lock poisoned".to_string())?;
    let mut meta = read_browser_session_meta_from_dir(date_dir, session_id).unwrap_or_default();
    apply(&mut meta)?;
    write_browser_session_meta_to_dir(date_dir, session_id, &meta)?;
    Ok(meta)
}

pub(crate) fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map(|d| d.as_millis().min(u128::from(u64::MAX)) as u64)
        .unwrap_or(0)
}

pub(crate) fn session_base_dir(
    session_type: &str,
    kim_dir: Option<String>,
    codex_dir: Option<String>,
) -> PathBuf {
    let raw = if session_type == "codex" {
        codex_dir.map(PathBuf::from)
    } else {
        kim_dir.map(PathBuf::from)
    };

    let candidate = match raw {
        Some(p) => p,
        None => return default_sessions_dir(),
    };

    // Canonicalize and enforce that the caller-supplied path stays within the
    // allowed sessions roots (#13: path traversal via kim_dir / codex_dir).
    // A path like "../../../etc" would escape the sessions tree; we reject it.
    let canonical = match candidate.canonicalize() {
        Ok(p) => p,
        Err(_) => return default_sessions_dir(),
    };
    let allowed_root = default_project_root();
    if canonical.starts_with(&allowed_root) {
        canonical
    } else {
        eprintln!(
            "[Kim] session_base_dir: rejected path outside project root: {}",
            canonical.display()
        );
        default_sessions_dir()
    }
}

pub(crate) fn resolve_session_date_dir(
    base: &Path,
    session_id: &str,
    session_date: Option<&str>,
) -> Result<PathBuf, String> {
    validate_session_id(session_id)?;

    // H-BRIDGE-1: the client-controlled date component must be validated
    // exactly like session_id — otherwise it is a path-traversal vector.
    let requested: Option<PathBuf> = match session_date.map(str::trim).filter(|d| !d.is_empty()) {
        Some(date) => {
            validate_session_date(date)?;
            Some(base.join(date))
        }
        None => None,
    };

    // Use the requested date bucket when the session actually lives there.
    if let Some(dir) = &requested {
        if dir.join(format!("{}.jsonl", session_id)).exists()
            || dir.join(browser_session_meta_filename(session_id)).exists()
        {
            return Ok(dir.clone());
        }
    }

    if base.exists() {
        let mut date_dirs: Vec<PathBuf> = fs::read_dir(base)
            .map_err(|e| e.to_string())?
            .filter_map(|e| e.ok())
            .map(|e| e.path())
            .filter(|p| p.is_dir())
            .collect();
        date_dirs.sort_by_key(|p| {
            std::cmp::Reverse(
                p.file_name()
                    .map(|s| s.to_string_lossy().to_string())
                    .unwrap_or_default(),
            )
        });
        for dir in date_dirs {
            if dir.join(format!("{}.jsonl", session_id)).exists()
                || dir.join(browser_session_meta_filename(session_id)).exists()
            {
                return Ok(dir);
            }
        }
    }

    // Brand-new session with an explicitly requested (validated) date bucket:
    // honour it only if the bucket already exists. L-STORE-2: this check runs
    // AFTER the scan above, so an existing date dir that does not contain the
    // session can no longer shadow the dir where the session actually lives.
    if let Some(dir) = requested {
        if dir.exists() {
            return Ok(dir);
        }
    }

    // New chat fallback: create today's date bucket. This keeps metadata next to
    // the session file once the first run creates it.
    let today = chrono_like_today();
    Ok(base.join(today))
}

pub(crate) fn read_sessions_from_dir(
    base: &Path,
    session_type: &str,
) -> Result<Vec<SessionInfo>, String> {
    if !base.exists() {
        return Ok(vec![]);
    }

    let mut sessions = vec![];

    let mut date_dirs: Vec<_> = fs::read_dir(base)
        .map_err(|e| e.to_string())?
        .filter_map(|e| e.ok())
        .filter(|e| e.path().is_dir())
        .collect();
    date_dirs.sort_by_key(|b| std::cmp::Reverse(b.file_name()));

    for date_entry in date_dirs {
        let date_dir = date_entry.path();
        let date_str = date_entry.file_name().to_string_lossy().to_string();

        // L-STORE-3: one unreadable date subdir must not blank the whole
        // session list — skip it and keep listing the rest.
        let dir_entries = match fs::read_dir(&date_dir) {
            Ok(entries) => entries,
            Err(e) => {
                eprintln!(
                    "[Kim] list_sessions: skipping unreadable dir {}: {}",
                    date_dir.display(),
                    e
                );
                continue;
            }
        };
        let mut jsonl_files: Vec<_> = dir_entries
            .filter_map(|e| e.ok())
            .filter(|e| {
                let name = e.file_name();
                let s = name.to_string_lossy();
                s.ends_with(".jsonl") && !s.contains(".summary")
            })
            .collect();
        jsonl_files.sort_by_key(|b| std::cmp::Reverse(b.file_name()));

        for file_entry in jsonl_files {
            let session_file = file_entry.path();
            let session_id = session_file
                .file_stem()
                .unwrap_or_default()
                .to_string_lossy()
                .to_string();

            let summary_file = date_dir.join(format!("{}.summary.txt", session_id));
            let has_summary = summary_file.exists();
            let summary = if has_summary {
                fs::read_to_string(&summary_file)
                    .ok()
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty())
            } else {
                None
            };

            let message_count = count_lines(&session_file).unwrap_or(0);
            let mut title = infer_session_title(&session_file, summary.as_ref(), &session_id);
            // K4: merge the user meta sidecar (title override + pin).
            let (meta_title, pinned) =
                crate::session_commands::read_session_meta(&date_dir, &session_id);
            if let Some(t) = meta_title {
                if !t.trim().is_empty() {
                    title = t;
                }
            }

            let browser_meta =
                read_browser_session_meta_from_dir(&date_dir, &session_id).unwrap_or_default();
            let BrowserSessionMeta {
                browser_threads,
                browser_last_site,
                browser_threads_updated_at_ms,
                last_llm_provider,
            } = browser_meta;
            sessions.push(SessionInfo {
                session_key: format!("{}:{}:{}", session_type, date_str, session_id),
                session_id,
                title,
                date: date_str.clone(),
                message_count,
                has_summary,
                summary,
                session_type: session_type.to_string(),
                pinned,
                browser_threads: if browser_threads.is_empty() {
                    None
                } else {
                    Some(browser_threads)
                },
                browser_last_site,
                browser_threads_updated_at_ms,
                last_llm_provider,
            });
        }
    }

    Ok(sessions)
}

pub(crate) fn count_lines(path: &Path) -> std::io::Result<usize> {
    let file = fs::File::open(path)?;
    let reader = BufReader::new(file);
    Ok(reader
        .lines()
        .filter(|l| l.as_ref().map(|s| !s.trim().is_empty()).unwrap_or(false))
        .count())
}

pub(crate) fn parse_jsonl(path: &Path) -> Result<Vec<KimMessage>, String> {
    let file = fs::File::open(path).map_err(|e| e.to_string())?;
    let reader = BufReader::new(file);
    let mut messages = vec![];

    for (i, line) in reader.lines().enumerate() {
        let line = line.map_err(|e| e.to_string())?;
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        match serde_json::from_str::<KimMessage>(trimmed) {
            Ok(msg) => messages.push(msg),
            Err(e) => {
                let parsed = serde_json::from_str::<serde_json::Value>(trimmed).ok();
                // Typed trace records (run_started, tool_call, llm_turn, run_checkpoint,
                // run_result) are valid JSON with a "type" key but no "role". Skip them
                // silently — they are not chat messages and are not malformed.
                let is_trace_record = parsed.as_ref().and_then(|v| v.get("type")).is_some()
                    && parsed.as_ref().and_then(|v| v.get("role")).is_none();
                if is_trace_record {
                    continue;
                }
                match parsed.and_then(codex_jsonl_line_to_kim_message) {
                    Some(msg) => messages.push(msg),
                    None => eprintln!("Skipping malformed JSONL line {}: {}", i + 1, e),
                }
            }
        }
    }

    Ok(messages)
}

pub(crate) fn codex_jsonl_line_to_kim_message(value: serde_json::Value) -> Option<KimMessage> {
    if value.get("type").and_then(|v| v.as_str()) != Some("message") {
        return None;
    }

    let message = value.get("message")?;
    let role = message.get("role")?.as_str()?.to_string();
    let content = message
        .get("blocks")
        .cloned()
        .map(normalize_codex_blocks)
        .or_else(|| message.get("content").cloned())
        .unwrap_or_else(|| serde_json::Value::String(String::new()));

    Some(KimMessage {
        role,
        content,
        tool_calls: None,
        tool_call_id: None,
        name: None,
    })
}

pub(crate) fn normalize_codex_blocks(blocks: serde_json::Value) -> serde_json::Value {
    let serde_json::Value::Array(items) = blocks else {
        return blocks;
    };

    serde_json::Value::Array(
        items
            .into_iter()
            .map(|mut block| {
                if block.get("type").and_then(|v| v.as_str()) == Some("tool_use") {
                    if let Some(raw_input) = block.get("input").and_then(|v| v.as_str()) {
                        let parsed = serde_json::from_str::<serde_json::Value>(raw_input)
                            .unwrap_or_else(|_| serde_json::json!({ "raw": raw_input }));
                        if let Some(obj) = block.as_object_mut() {
                            obj.insert("input".to_string(), parsed);
                        }
                    }
                }
                block
            })
            .collect(),
    )
}

pub(crate) fn normalize_title_text(raw: &str) -> Option<String> {
    let mut text = raw.replace('\n', " ");
    text = text.split_whitespace().collect::<Vec<_>>().join(" ");
    let mut cleaned = text.trim().to_string();

    for prefix in ["Task:", "task:", "TASK:"] {
        if cleaned.starts_with(prefix) {
            cleaned = cleaned[prefix.len()..].trim().to_string();
            break;
        }
    }

    if cleaned.is_empty() {
        return None;
    }

    let max_chars = 56usize;
    let char_count = cleaned.chars().count();
    if char_count > max_chars {
        let mut shortened: String = cleaned.chars().take(max_chars - 1).collect();
        shortened = shortened.trim_end().to_string();
        return Some(format!("{}…", shortened));
    }

    Some(cleaned)
}

pub(crate) fn extract_title_from_content(content: &serde_json::Value) -> Option<String> {
    match content {
        serde_json::Value::String(s) => normalize_title_text(s),
        serde_json::Value::Array(items) => {
            for item in items {
                let item_type = item.get("type").and_then(|v| v.as_str()).unwrap_or("");
                if item_type == "text" {
                    if let Some(text) = item.get("text").and_then(|v| v.as_str()) {
                        if let Some(title) = normalize_title_text(text) {
                            return Some(title);
                        }
                    }
                }
            }
            None
        }
        _ => None,
    }
}

pub(crate) fn infer_session_title(
    session_file: &Path,
    summary: Option<&String>,
    session_id: &str,
) -> String {
    if let Ok(file) = fs::File::open(session_file) {
        let reader = BufReader::new(file);
        for line in reader.lines().map_while(Result::ok).take(80) {
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }
            let value: serde_json::Value = match serde_json::from_str(trimmed) {
                Ok(v) => v,
                Err(_) => continue,
            };

            let role = value.get("role").and_then(|v| v.as_str()).unwrap_or("");
            if role == "user" {
                if let Some(content) = value.get("content") {
                    if let Some(title) = extract_title_from_content(content) {
                        return title;
                    }
                }
            } else if value.get("type").and_then(|v| v.as_str()) == Some("message") {
                let message = value.get("message").unwrap_or(&serde_json::Value::Null);
                let role = message.get("role").and_then(|v| v.as_str()).unwrap_or("");
                if role == "user" {
                    if let Some(content) = message.get("blocks").or_else(|| message.get("content"))
                    {
                        if let Some(title) = extract_title_from_content(content) {
                            return title;
                        }
                    }
                }
            }
        }
    }

    if let Some(s) = summary {
        if let Some(title) = normalize_title_text(s) {
            return title;
        }
    }

    let short_id: String = session_id.chars().take(8).collect();
    format!("Session {}", short_id)
}

#[cfg(test)]
mod session_store_tests {
    use super::*;

    // ── H-BRIDGE-1: session_date must be validated like session_id ──────────

    #[test]
    fn session_date_traversal_rejected() {
        for bad in [
            "../../../../Users/victim/.ssh",
            "..",
            "a/b",
            "a\\b",
            "2026-07-06/..",
            "x\0y",
            "date with spaces",
        ] {
            assert!(validate_session_date(bad).is_err(), "must reject {bad:?}");
        }
    }

    #[test]
    fn session_date_valid_forms_accepted() {
        for good in ["2026-07-06", "2025-12-31", "legacy_bucket-1.2"] {
            assert!(validate_session_date(good).is_ok(), "must accept {good:?}");
        }
    }

    #[test]
    fn resolve_rejects_traversal_session_date() {
        let base = tempfile::tempdir().unwrap();
        let err = resolve_session_date_dir(base.path(), "sess-1", Some("../../../../etc"));
        assert!(err.is_err(), "traversal date must be rejected: {err:?}");
    }

    // ── L-STORE-2: an existing-but-wrong date dir must not shadow the scan ──

    #[test]
    fn resolve_prefers_dir_that_contains_the_session() {
        let base = tempfile::tempdir().unwrap();
        let wrong = base.path().join("2026-01-01");
        let right = base.path().join("2026-07-06");
        std::fs::create_dir_all(&wrong).unwrap();
        std::fs::create_dir_all(&right).unwrap();
        std::fs::write(right.join("sess-1.jsonl"), "{}\n").unwrap();

        // Caller asks for the wrong (existing, empty) bucket — the resolver
        // must return the bucket that actually holds the session file.
        let dir = resolve_session_date_dir(base.path(), "sess-1", Some("2026-01-01")).unwrap();
        assert_eq!(dir, right, "must resolve to the dir containing the session");
    }

    #[test]
    fn resolve_honours_requested_dir_when_session_lives_there() {
        let base = tempfile::tempdir().unwrap();
        let bucket = base.path().join("2026-07-06");
        std::fs::create_dir_all(&bucket).unwrap();
        std::fs::write(bucket.join("sess-2.jsonl"), "{}\n").unwrap();
        let dir = resolve_session_date_dir(base.path(), "sess-2", Some("2026-07-06")).unwrap();
        assert_eq!(dir, bucket);
    }

    #[test]
    fn resolve_brand_new_session_uses_existing_requested_bucket() {
        let base = tempfile::tempdir().unwrap();
        let bucket = base.path().join("2026-07-01");
        std::fs::create_dir_all(&bucket).unwrap();
        // No session files anywhere: requested existing bucket is honoured.
        let dir = resolve_session_date_dir(base.path(), "brand-new", Some("2026-07-01")).unwrap();
        assert_eq!(dir, bucket);
    }

    // ── M-STORE-1: concurrent read-modify-writes must merge, not clobber ────

    #[test]
    fn concurrent_meta_updates_do_not_lose_fields() {
        let base = tempfile::tempdir().unwrap();
        let dir = base.path().to_path_buf();
        let mut handles = Vec::new();
        for i in 0..8 {
            let dir = dir.clone();
            handles.push(std::thread::spawn(move || {
                update_browser_session_meta(&dir, "sess-m", |meta| {
                    meta.browser_threads
                        .insert(format!("site{i}"), format!("https://example.com/{i}"));
                    Ok(())
                })
                .unwrap();
            }));
        }
        for h in handles {
            h.join().unwrap();
        }
        let meta = read_browser_session_meta_from_dir(&dir, "sess-m").unwrap();
        assert_eq!(
            meta.browser_threads.len(),
            8,
            "all 8 concurrent writers' fields must survive: {:?}",
            meta.browser_threads
        );
    }
}

//! Data I/O — export, import, GitHub Gist backup/restore, and PAT verification.
//!
//! Extracted from lib.rs (Phase 8 restructure).
//! Public Tauri commands: `verify_github_pat`, `export_data`, `import_data`,
//! `backup_to_gist`, `restore_from_gist`.

use std::fs;
use std::path::{Path, PathBuf};
use serde::{Deserialize, Serialize};
use crate::account::{KimAccount, account_dir, account_path};

// ---------------------------------------------------------------------------
// Shared time helpers — used by other lib.rs sections via `crate::data_io::`
// ---------------------------------------------------------------------------

pub(crate) fn chrono_now() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    unix_secs_to_utc_iso(secs)
}

pub(crate) fn unix_secs_to_utc_iso(secs: u64) -> String {
    // Convert Unix seconds to (year, month, day, h, m, s) UTC.
    let s = secs % 60;
    let m = (secs / 60) % 60;
    let h = (secs / 3600) % 24;
    // Days since Unix epoch (1970-01-01)
    let mut days = secs / 86400;
    // Gregorian calendar algorithm (Julian Day Number method)
    let mut year = 1970u64;
    loop {
        let leap = (year.is_multiple_of(4) && !year.is_multiple_of(100)) || year.is_multiple_of(400);
        let days_in_year = if leap { 366 } else { 365 };
        if days < days_in_year { break; }
        days -= days_in_year;
        year += 1;
    }
    let leap = (year.is_multiple_of(4) && !year.is_multiple_of(100)) || year.is_multiple_of(400);
    let month_days: [u64; 12] = [31, if leap { 29 } else { 28 }, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    let mut month = 1u64;
    for &md in &month_days {
        if days < md { break; }
        days -= md;
        month += 1;
    }
    let day = days + 1;
    format!("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z", year, month, day, h, m, s)
}

// ---------------------------------------------------------------------------
// GitHub PAT verification
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct GitHubUser {
    pub login: String,
    pub name: Option<String>,
    pub avatar_url: String,
}

#[tauri::command]
pub async fn verify_github_pat(token: String) -> Result<GitHubUser, String> {
    let client = reqwest::Client::new();
    let resp = client
        .get("https://api.github.com/user")
        .header("Authorization", format!("Bearer {}", token))
        .header("User-Agent", "Kim-Desktop/0.1")
        .header("Accept", "application/vnd.github+json")
        .send()
        .await
        .map_err(|e| format!("Network error: {}", e))?;

    if !resp.status().is_success() {
        return Err(format!("GitHub returned {}. Check that your token has 'read:user' scope.", resp.status()));
    }

    let user: GitHubUser = resp.json().await.map_err(|e| e.to_string())?;
    Ok(user)
}

// ---------------------------------------------------------------------------
// Data export — ZIP | JSON | Markdown
// ---------------------------------------------------------------------------

#[tauri::command]
pub async fn export_data(
    format: String,
    output_path: Option<String>,
    sessions_dir: Option<String>,
) -> Result<String, String> {
    let base = sessions_dir
        .map(PathBuf::from)
        .unwrap_or_else(crate::default_sessions_dir);
    let out = output_path
        .map(PathBuf::from)
        .unwrap_or_else(|| default_export_path(&format));

    match format.as_str() {
        "zip" => export_as_zip(&base, &out),
        "json" => export_as_json(&base, &out),
        "markdown" => export_as_markdown(&base, &out),
        _ => Err(format!("Unknown format: {}. Use 'zip', 'json', or 'markdown'.", format)),
    }
}

fn default_export_path(format: &str) -> PathBuf {
    let ext = match format {
        "markdown" => "md",
        "zip" => "zip",
        _ => "json",
    };
    let stamp = chrono_now().replace([':', '/'], "-");
    dirs::download_dir()
        .or_else(dirs::document_dir)
        .or_else(dirs::home_dir)
        .unwrap_or_else(account_dir)
        .join(format!("kim-export-{stamp}.{ext}"))
}

pub(crate) fn sanitized_account_json(gist_id_hint: Option<&str>) -> String {
    let Ok(raw) = fs::read_to_string(account_path()) else {
        return "{}".to_string();
    };
    let Ok(mut value) = serde_json::from_str::<serde_json::Value>(&raw) else {
        return "{}".to_string();
    };
    if let Some(obj) = value.as_object_mut() {
        obj.remove("github_token");
        if let Some(gist_id) = gist_id_hint.map(str::trim).filter(|s| !s.is_empty()) {
            obj.insert("gist_id".to_string(), serde_json::Value::String(gist_id.to_string()));
        }
    }
    serde_json::to_string_pretty(&value).unwrap_or_else(|_| "{}".to_string())
}

fn export_as_zip(sessions_base: &Path, out: &Path) -> Result<String, String> {
    use std::io::Write;
    use zip::write::FileOptions;

    let file = std::fs::File::create(out).map_err(|e| e.to_string())?;
    let mut zip = zip::ZipWriter::new(file);
    let opts: FileOptions<'_, ()> = FileOptions::default().compression_method(zip::CompressionMethod::Deflated);

    let mut count = 0usize;
    collect_jsonl_files(sessions_base, &mut |rel, data| {
        zip.start_file(rel, opts).ok();
        zip.write_all(data).ok();
        count += 1;
    });

    // Include account metadata, but never export credentials/tokens.
    if account_path().exists() {
        let data = sanitized_account_json(None);
        zip.start_file("account.json", opts).ok();
        let _ = zip.write_all(data.as_bytes());
    }

    zip.finish().map_err(|e| e.to_string())?;
    Ok(format!("Exported {} session files to {}", count, out.display()))
}

fn export_as_json(sessions_base: &Path, out: &Path) -> Result<String, String> {
    let mut sessions: Vec<serde_json::Value> = Vec::new();

    collect_jsonl_files(sessions_base, &mut |rel, data| {
        let text = String::from_utf8_lossy(data);
        let messages: Vec<serde_json::Value> = text
            .lines()
            .filter_map(|l| serde_json::from_str(l).ok())
            .collect();
        sessions.push(serde_json::json!({ "session": rel, "messages": messages }));
    });

    let payload = serde_json::json!({
        "version": 1,
        "exported_at": chrono_now(),
        "sessions": sessions,
    });
    let raw = serde_json::to_string_pretty(&payload).map_err(|e| e.to_string())?;
    fs::write(out, raw).map_err(|e| e.to_string())?;
    Ok(format!("Exported {} sessions to {}", sessions.len(), out.display()))
}

fn export_as_markdown(sessions_base: &Path, out: &Path) -> Result<String, String> {
    use std::fmt::Write as FmtWrite;
    let mut md = String::new();
    let _ = writeln!(md, "# Kim Session Export\n\nExported: {}\n", chrono_now());

    let mut count = 0usize;
    collect_jsonl_files(sessions_base, &mut |rel, data| {
        let _ = writeln!(md, "---\n\n## {}\n", rel);
        let text = String::from_utf8_lossy(data);
        for line in text.lines() {
            if let Ok(msg) = serde_json::from_str::<serde_json::Value>(line) {
                let role = msg["role"].as_str().unwrap_or("unknown");
                let content = match msg["content"].as_str() {
                    Some(s) => s.to_string(),
                    None => msg["content"].to_string(),
                };
                let _ = writeln!(md, "**{}**: {}\n", role, content);
            }
        }
        count += 1;
    });

    fs::write(out, &md).map_err(|e| e.to_string())?;
    Ok(format!("Exported {} sessions as Markdown to {}", count, out.display()))
}

pub(crate) fn collect_jsonl_files<F>(base: &Path, cb: &mut F)
where
    F: FnMut(String, &[u8]),
{
    if !base.exists() {
        return;
    }
    let Ok(date_dirs) = fs::read_dir(base) else { return };
    for de in date_dirs.filter_map(|e| e.ok()).filter(|e| e.path().is_dir()) {
        let date = de.file_name().to_string_lossy().to_string();
        let Ok(files) = fs::read_dir(de.path()) else { continue };
        for fe in files.filter_map(|e| e.ok()) {
            let name = fe.file_name().to_string_lossy().to_string();
            if name.ends_with(".jsonl") {
                if let Ok(data) = fs::read(fe.path()) {
                    cb(format!("{}/{}", date, name), &data);
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Data import — restore from a Kim ZIP or JSON export
// ---------------------------------------------------------------------------

#[tauri::command]
pub async fn import_data(
    file_path: Option<String>,
    path: Option<String>,
    sessions_dir: Option<String>,
) -> Result<String, String> {
    let file_path = file_path
        .or(path)
        .ok_or_else(|| "File path is required.".to_string())?;
    let src = PathBuf::from(&file_path);
    if !src.exists() {
        return Err(format!("File not found: {}", file_path));
    }

    let base = sessions_dir
        .map(PathBuf::from)
        .unwrap_or_else(crate::default_sessions_dir);
    fs::create_dir_all(&base).map_err(|e| e.to_string())?;

    let ext = src.extension().and_then(|e| e.to_str()).unwrap_or("");
    match ext {
        "zip" => import_from_zip(&src, &base),
        "json" => import_from_json(&src, &base),
        _ => Err("Unsupported file type. Use a .zip or .json exported from Kim.".to_string()),
    }
}

fn import_from_zip(src: &Path, base: &Path) -> Result<String, String> {
    use std::io::Read;

    let file = std::fs::File::open(src).map_err(|e| e.to_string())?;
    let mut zip = zip::ZipArchive::new(file).map_err(|e| e.to_string())?;
    let mut count = 0usize;

    for i in 0..zip.len() {
        let mut entry = zip.by_index(i).map_err(|e| e.to_string())?;
        let name = entry.name().to_string();

        if name == "account.json" {
            let acct_path = account_path();
            if !acct_path.exists() {
                let mut buf = Vec::new();
                entry.read_to_end(&mut buf).ok();
                if let Ok(mut account) = serde_json::from_slice::<KimAccount>(&buf) {
                    account.github_token = None;
                    if let Some(parent) = acct_path.parent() {
                        fs::create_dir_all(parent).ok();
                    }
                    if let Ok(raw) = serde_json::to_vec_pretty(&account) {
                        fs::write(&acct_path, raw).ok();
                    }
                }
            }
            continue;
        }

        if !name.ends_with(".jsonl") {
            continue;
        }

        let dest = base.join(&name);

        // Guard against path traversal attacks (e.g. "../../etc/passwd" in the ZIP).
        if let Some(parent) = dest.parent() {
            fs::create_dir_all(parent).map_err(|e| e.to_string())?;
            if let (Ok(canon_dest_parent), Ok(canon_base)) = (parent.canonicalize(), base.canonicalize()) {
                let full = canon_dest_parent.join(dest.file_name().unwrap_or_default());
                if !full.starts_with(&canon_base) {
                    continue;
                }
            }
        } else {
            fs::create_dir_all(base).map_err(|e| e.to_string())?;
        }
        let mut buf = Vec::new();
        entry.read_to_end(&mut buf).map_err(|e| e.to_string())?;
        fs::write(&dest, &buf).map_err(|e| e.to_string())?;
        count += 1;
    }

    Ok(format!("Imported {} session files.", count))
}

fn import_from_json(src: &Path, base: &Path) -> Result<String, String> {
    let raw = fs::read_to_string(src).map_err(|e| e.to_string())?;
    let payload: serde_json::Value = serde_json::from_str(&raw).map_err(|e| e.to_string())?;

    let sessions = payload["sessions"].as_array().ok_or("Invalid export format: missing 'sessions' array.")?;
    let mut count = 0usize;

    for session in sessions {
        let rel = session["session"].as_str().unwrap_or("unknown/session.jsonl");

        // Guard against path traversal attacks
        if rel.contains("..") || rel.starts_with('/') || rel.starts_with('\\') {
            return Err(format!("path traversal attempt: {:?}", rel));
        }
        if rel.len() >= 2 && rel.as_bytes()[1] == b':' {
            return Err(format!("path traversal attempt (drive prefix): {:?}", rel));
        }

        let dest = base.join(rel);

        if let Some(parent) = dest.parent() {
            fs::create_dir_all(parent).map_err(|e| e.to_string())?;
            if let (Ok(canon_dest_parent), Ok(canon_base)) = (parent.canonicalize(), base.canonicalize()) {
                let full = canon_dest_parent.join(dest.file_name().unwrap_or_default());
                if !full.starts_with(&canon_base) {
                    return Err(format!("path traversal attempt: {:?}", rel));
                }
            }
        } else {
            fs::create_dir_all(base).map_err(|e| e.to_string())?;
        }

        let messages = session["messages"].as_array().cloned().unwrap_or_default();
        let mut lines = String::new();
        for msg in &messages {
            if let Ok(line) = serde_json::to_string(msg) {
                lines.push_str(&line);
                lines.push('\n');
            }
        }
        fs::write(&dest, &lines).map_err(|e| e.to_string())?;
        count += 1;
    }

    Ok(format!("Imported {} sessions.", count))
}

// ---------------------------------------------------------------------------
// GitHub Gist backup / restore
// ---------------------------------------------------------------------------

#[tauri::command]
pub async fn backup_to_gist(
    token: String,
    sessions_dir: Option<String>,
    existing_gist_id: Option<String>,
) -> Result<String, String> {
    let base = sessions_dir
        .map(PathBuf::from)
        .unwrap_or_else(crate::default_sessions_dir);

    let mut index: Vec<serde_json::Value> = Vec::new();
    collect_jsonl_files(&base, &mut |rel, data| {
        let line_count = data.iter().filter(|&&b| b == b'\n').count();
        index.push(serde_json::json!({ "path": rel, "messages": line_count }));
    });

    let account_data = sanitized_account_json(existing_gist_id.as_deref());

    let payload = serde_json::json!({
        "version": 1,
        "backed_up_at": chrono_now(),
        "session_index": index,
    });

    let files = serde_json::json!({
        "kim_backup.json": { "content": serde_json::to_string_pretty(&payload).unwrap_or_default() },
        "kim_account.json": { "content": account_data },
    });

    let client = reqwest::Client::new();

    let gist_url = match &existing_gist_id {
        Some(id) => format!("https://api.github.com/gists/{}", id),
        None => "https://api.github.com/gists".to_string(),
    };

    let body = serde_json::json!({
        "description": "Kim Desktop backup (private)",
        "public": false,
        "files": files,
    });

    let req = if existing_gist_id.is_some() {
        client.patch(&gist_url)
    } else {
        client.post(&gist_url)
    };

    let resp = req
        .header("Authorization", format!("Bearer {}", token))
        .header("User-Agent", "Kim-Desktop/0.1")
        .header("Accept", "application/vnd.github+json")
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("Network error: {}", e))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let err = resp.text().await.unwrap_or_default();
        return Err(format!("GitHub returned {}: {}", status, err));
    }

    let gist: serde_json::Value = resp.json().await.map_err(|e| e.to_string())?;
    let gist_id = gist["id"].as_str().unwrap_or("").to_string();
    Ok(gist_id)
}

#[tauri::command]
pub async fn restore_from_gist(
    token: String,
    gist_id: String,
) -> Result<KimAccount, String> {
    let client = reqwest::Client::new();
    let resp = client
        .get(format!("https://api.github.com/gists/{}", gist_id))
        .header("Authorization", format!("Bearer {}", token))
        .header("User-Agent", "Kim-Desktop/0.1")
        .header("Accept", "application/vnd.github+json")
        .send()
        .await
        .map_err(|e| format!("Network error: {}", e))?;

    if !resp.status().is_success() {
        return Err(format!("GitHub returned {}. Check the Gist ID.", resp.status()));
    }

    let gist: serde_json::Value = resp.json().await.map_err(|e| e.to_string())?;

    if let Some(acct_content) = gist["files"]["kim_account.json"]["content"].as_str() {
        if let Ok(mut account) = serde_json::from_str::<KimAccount>(acct_content) {
            account.github_token = None;
            account.gist_id = Some(gist_id.clone());
            if !account_path().exists() {
                let dir = account_dir();
                fs::create_dir_all(&dir).ok();
                if let Ok(raw) = serde_json::to_string_pretty(&account) {
                    fs::write(account_path(), raw).ok();
                }
            }
            return Ok(account);
        }
    }

    Err("Gist found but kim_account.json was empty or invalid.".to_string())
}

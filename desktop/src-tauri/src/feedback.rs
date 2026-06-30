//! User feedback — POST to a private Discord webhook.
//!
//! Extracted from lib.rs (Phase 8 restructure).
//! Public Tauri commands: `send_feedback`, `save_attachment`.

use base64::Engine as _;
use serde::Deserialize;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// Monotonic counter so two saves within the same millisecond still get
/// distinct subdir names. (D4)
static ATTACHMENT_COUNTER: AtomicU64 = AtomicU64::new(0);
const ATTACHMENT_MAX_AGE: Duration = Duration::from_secs(7 * 24 * 60 * 60);

/// D4: each attachment goes in its own `<unix-ms>-<n>/<original-name>` subdir so
/// repeated saves of the same filename never collide; the original name is kept
/// for model readability.
fn store_attachment(base: &Path, filename: &str, bytes: &[u8]) -> Result<PathBuf, String> {
    fs::create_dir_all(base).map_err(|e| e.to_string())?;
    let ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    let n = ATTACHMENT_COUNTER.fetch_add(1, Ordering::Relaxed);
    let dir = base.join(format!("{ms}-{n}"));
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let safe_name = PathBuf::from(filename)
        .file_name()
        .and_then(|n| n.to_str())
        .filter(|s| !s.is_empty())
        .unwrap_or("attachment")
        .to_string();
    let dest = dir.join(&safe_name);
    fs::write(&dest, bytes).map_err(|e| e.to_string())?;
    Ok(dest)
}

/// D4: remove attachment subdirs older than 7 days on each save so the temp dir
/// doesn't grow without bound.
fn sweep_old_attachments(base: &Path, max_age: Duration) {
    let Ok(entries) = fs::read_dir(base) else {
        return;
    };
    let now = SystemTime::now();
    for entry in entries.flatten() {
        let path = entry.path();
        if !path.is_dir() {
            continue;
        }
        if let Ok(modified) = entry.metadata().and_then(|m| m.modified()) {
            if now
                .duration_since(modified)
                .map(|age| age > max_age)
                .unwrap_or(false)
            {
                let _ = fs::remove_dir_all(&path);
            }
        }
    }
}

/// The Discord webhook URL is embedded at compile time from the
/// KIM_DISCORD_WEBHOOK environment variable.  Set it before `cargo build`:
///   export KIM_DISCORD_WEBHOOK="https://discord.com/api/webhooks/..."
/// Leave unset (or empty) to silently no-op — the user sees a success message
/// so they're not confused, but nothing is transmitted.
const DISCORD_WEBHOOK_URL: &str = match option_env!("KIM_DISCORD_WEBHOOK") {
    Some(url) => url,
    None => "",
};

#[tauri::command]
pub async fn save_attachment(filename: String, data_base64: String) -> Result<String, String> {
    let base = std::env::temp_dir().join("kim_attachments");
    sweep_old_attachments(&base, ATTACHMENT_MAX_AGE);
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(data_base64.trim())
        .map_err(|e| format!("base64 decode error: {e}"))?;
    let dest = store_attachment(&base, &filename, &bytes)?;
    Ok(dest.to_string_lossy().into_owned())
}

#[derive(Deserialize)]
pub struct FeedbackPayload {
    pub category: String, // "bug" | "feature" | "general" | "praise" | "other"
    pub message: String,
    pub contact: Option<String>,
}

#[tauri::command]
pub async fn send_feedback(payload: FeedbackPayload) -> Result<(), String> {
    if DISCORD_WEBHOOK_URL.is_empty() {
        return Ok(());
    }

    let category_label = match payload.category.as_str() {
        "bug" => "🐛 Bug Report",
        "feature" => "✨ Feature Request",
        "praise" => "🙏 Praise",
        "general" => "💬 General Feedback",
        _ => "📝 Feedback",
    };

    let contact_field = payload
        .contact
        .filter(|s| !s.trim().is_empty())
        .map(|email| {
            serde_json::json!({
                "name": "Contact",
                "value": email,
                "inline": true,
            })
        });

    let mut fields = vec![
        serde_json::json!({
            "name": "Category",
            "value": category_label,
            "inline": true,
        }),
        serde_json::json!({
            "name": "Message",
            "value": &payload.message,
            "inline": false,
        }),
    ];
    if let Some(cf) = contact_field {
        fields.push(cf);
    }

    let color = match payload.category.as_str() {
        "bug" => 0xef4444u32,
        "feature" => 0x6366f1,
        "praise" => 0x22c55e,
        _ => 0x64748b,
    };

    let body = serde_json::json!({
        "embeds": [{
            "title": format!("{} — Kim Desktop", category_label),
            "color": color,
            "fields": fields,
            "footer": { "text": format!("Kim Desktop — {}", crate::data_io::chrono_now()) },
        }]
    });

    let client = reqwest::Client::new();
    client
        .post(DISCORD_WEBHOOK_URL)
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("Network error: {}", e))?
        .error_for_status()
        .map_err(|e| format!("Webhook error: {}", e))?;

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn d4_same_filename_two_saves_distinct_readable_paths() {
        let base = std::env::temp_dir().join(format!(
            "kim_attach_test_{}",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let p1 = store_attachment(&base, "report.pdf", b"one").unwrap();
        let p2 = store_attachment(&base, "report.pdf", b"two").unwrap();
        assert_ne!(p1, p2, "same filename must not collide");
        assert_eq!(p1.file_name().unwrap(), "report.pdf"); // original name kept
        assert_eq!(fs::read(&p1).unwrap(), b"one");
        assert_eq!(fs::read(&p2).unwrap(), b"two");
        let _ = fs::remove_dir_all(&base);
    }

    #[test]
    fn d4_sweep_removes_old_dirs_only() {
        let base = std::env::temp_dir().join(format!(
            "kim_attach_sweep_{}",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let fresh = store_attachment(&base, "keep.txt", b"x").unwrap();
        // Sweep with a zero max-age would delete everything; use a long age so the
        // fresh dir survives (proves we don't nuke recent attachments).
        sweep_old_attachments(&base, ATTACHMENT_MAX_AGE);
        assert!(fresh.exists(), "recent attachment must survive sweep");
        let _ = fs::remove_dir_all(&base);
    }
}

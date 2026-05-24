//! User feedback — POST to a private Discord webhook.
//!
//! Extracted from lib.rs (Phase 8 restructure).
//! Public Tauri commands: `send_feedback`, `save_attachment`.

use std::fs;
use base64::Engine as _;
use serde::Deserialize;

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
    use std::path::PathBuf;
    let dir = std::env::temp_dir().join("kim_attachments");
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let safe_name = PathBuf::from(&filename)
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("attachment")
        .to_string();
    let dest = dir.join(&safe_name);
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(data_base64.trim())
        .map_err(|e| format!("base64 decode error: {e}"))?;
    fs::write(&dest, bytes).map_err(|e| e.to_string())?;
    Ok(dest.to_string_lossy().into_owned())
}

#[derive(Deserialize)]
pub struct FeedbackPayload {
    pub category: String,    // "bug" | "feature" | "general" | "praise" | "other"
    pub message: String,
    pub contact: Option<String>,
}

#[tauri::command]
pub async fn send_feedback(payload: FeedbackPayload) -> Result<(), String> {
    if DISCORD_WEBHOOK_URL.is_empty() {
        return Ok(());
    }

    let category_label = match payload.category.as_str() {
        "bug"     => "🐛 Bug Report",
        "feature" => "✨ Feature Request",
        "praise"  => "🙏 Praise",
        "general" => "💬 General Feedback",
        _         => "📝 Feedback",
    };

    let contact_field = payload.contact
        .filter(|s| !s.trim().is_empty())
        .map(|email| serde_json::json!({
            "name": "Contact",
            "value": email,
            "inline": true,
        }));

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
        "bug"     => 0xef4444u32,
        "feature" => 0x6366f1,
        "praise"  => 0x22c55e,
        _         => 0x64748b,
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

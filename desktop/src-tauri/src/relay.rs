//! Phone-relay configuration and pairing commands.
//!
//! Extracted from lib.rs (Phase 8 restructure).
//! Public Tauri commands: `read_relay_config`, `write_relay_url`,
//! `relay_pair_init`, `relay_pair_status`.

use std::fs;
use std::path::PathBuf;
use std::time::Duration;
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct RelayConfig {
    /// e.g. "https://kim-relay.fly.dev". Empty string = phone-relay disabled.
    pub url: String,
    /// True when RELAY_PC_API_KEY is present in the environment. We never
    /// expose the key itself to the frontend — only whether it's set, so the
    /// UI can prompt the user before they try to pair.
    pub pc_key_configured: bool,
}

/// Same shape as `extract_voice_scalar` but parameterised over the top-level
/// block name. Used for any flat `block: { key: value, ... }` shape.
fn extract_block_scalar<'a>(yaml: &'a str, block: &str, key: &str) -> Option<&'a str> {
    let block_marker = format!("{}:", block);
    let key_prefix = format!("{}:", key);
    let mut in_block = false;
    for line in yaml.lines() {
        if !line.starts_with(char::is_whitespace) && line.trim_end().ends_with(':') {
            in_block = line.trim_end() == block_marker;
            continue;
        }
        if in_block {
            let trimmed = line.trim_start_matches([' ', '\t']);
            let indent = line.len() - trimmed.len();
            if (1..=4).contains(&indent) {
                if let Some(rest) = trimmed.strip_prefix(&key_prefix) {
                    let v = rest.trim().trim_matches(|c| c == '"' || c == '\'');
                    return Some(v);
                }
            }
        }
    }
    None
}

/// Like `upsert_voice_scalar`, but generic over the block name.
fn upsert_block_scalar(yaml: &str, block: &str, key: &str, value: &str) -> String {
    let block_marker = format!("{}:", block);
    let key_prefix = format!("{}:", key);
    let mut out: Vec<String> = Vec::with_capacity(yaml.lines().count() + 1);
    let mut in_block = false;
    let mut block_start: Option<usize> = None;
    let mut block_end: Option<usize> = None;
    let mut replaced = false;

    for line in yaml.lines() {
        if !line.starts_with(char::is_whitespace) && line.trim_end().ends_with(':') {
            if in_block {
                block_end = Some(out.len());
            }
            in_block = line.trim_end() == block_marker;
            if in_block {
                block_start = Some(out.len());
            }
            out.push(line.to_string());
            continue;
        }

        if in_block {
            let trimmed = line.trim_start();
            let indent = line.len() - trimmed.len();
            if indent == 2 && trimmed.starts_with(&key_prefix) {
                out.push(format!("  {}: {}", key, value));
                replaced = true;
                continue;
            }
        }
        out.push(line.to_string());
    }
    if in_block {
        block_end = Some(out.len());
    }

    if !replaced {
        match (block_start, block_end) {
            (Some(_), Some(end)) => {
                out.insert(end, format!("  {}: {}", key, value));
            }
            _ => {
                out.push(format!("{}:", block));
                out.push(format!("  {}: {}", key, value));
            }
        }
    }

    let mut s = out.join("\n");
    if yaml.ends_with('\n') && !s.ends_with('\n') {
        s.push('\n');
    }
    s
}

pub(crate) fn read_pc_api_key(project_root: Option<String>) -> String {
    // Env wins so deployments can override without editing files.
    if let Ok(v) = std::env::var("RELAY_PC_API_KEY") {
        if !v.is_empty() {
            return v;
        }
    }
    // Fall back to .env in the project root.
    let env_path = project_root
        .as_deref()
        .map(PathBuf::from)
        .unwrap_or_else(crate::default_project_root)
        .join(".env");
    if let Ok(contents) = fs::read_to_string(&env_path) {
        for line in contents.lines() {
            let line = line.trim();
            if let Some(rest) = line.strip_prefix("RELAY_PC_API_KEY=") {
                return rest.trim().trim_matches(|c| c == '"' || c == '\'').to_string();
            }
        }
    }
    String::new()
}

#[tauri::command]
pub async fn read_relay_config(project_root: Option<String>) -> Result<RelayConfig, String> {
    let path = crate::config_yaml_path(project_root.clone());
    let url = if path.exists() {
        let yaml = fs::read_to_string(&path).map_err(|e| e.to_string())?;
        extract_block_scalar(&yaml, "relay", "url")
            .unwrap_or("")
            .to_string()
    } else {
        String::new()
    };
    let pc_key_configured = !read_pc_api_key(project_root).is_empty();
    Ok(RelayConfig { url, pc_key_configured })
}

#[tauri::command]
pub async fn write_relay_url(url: String, project_root: Option<String>) -> Result<(), String> {
    let path = crate::config_yaml_path(project_root);
    let original = if path.exists() {
        fs::read_to_string(&path).map_err(|e| e.to_string())?
    } else {
        String::from("relay:\n")
    };
    let updated = upsert_block_scalar(&original, "relay", "url", url.trim());
    fs::write(&path, updated).map_err(|e| e.to_string())?;
    Ok(())
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct RelayPairInit {
    pub pair_code: String,
    pub expires_at: String,
    /// The relay URL we used — echoed back so the frontend can encode it in
    /// the QR payload without re-reading config.
    pub url: String,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct RelayPairStatus {
    pub claimed: bool,
    pub expired: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub device_name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub claimed_at: Option<String>,
}

/// Strip a trailing "/" so we never end up with "https://relay//pair/init".
fn trim_url(url: &str) -> &str {
    url.trim().trim_end_matches('/')
}

#[tauri::command]
pub async fn relay_pair_init(project_root: Option<String>) -> Result<RelayPairInit, String> {
    let cfg = read_relay_config(project_root.clone()).await?;
    let url = trim_url(&cfg.url);
    if url.is_empty() {
        return Err(
            "No relay URL configured. Set `relay.url` in config.yaml before pairing.".into(),
        );
    }
    let pc_key = read_pc_api_key(project_root);
    if pc_key.is_empty() {
        return Err(
            "RELAY_PC_API_KEY isn't set. Add it to .env so the PC can authenticate to the relay."
                .into(),
        );
    }

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(15))
        .build()
        .map_err(|e| e.to_string())?;
    let resp = client
        .post(format!("{}/pair/init", url))
        .header("X-API-Key", &pc_key)
        .header("Content-Type", "application/json")
        .body("{}")
        .send()
        .await
        .map_err(|e| format!("Network error talking to relay: {}", e))?;
    let status = resp.status();
    let text = resp.text().await.unwrap_or_default();
    if !status.is_success() {
        return Err(format!("Relay /pair/init returned {}: {}", status.as_u16(), text));
    }
    #[derive(Deserialize)]
    struct Body {
        pair_code: String,
        expires_at: String,
    }
    let body: Body = serde_json::from_str(&text).map_err(|e| format!(
        "Relay returned unparseable JSON: {} (raw: {})", e, text
    ))?;
    Ok(RelayPairInit {
        pair_code: body.pair_code,
        expires_at: body.expires_at,
        url: url.to_string(),
    })
}

#[tauri::command]
pub async fn relay_pair_status(
    pair_code: String,
    project_root: Option<String>,
) -> Result<RelayPairStatus, String> {
    let cfg = read_relay_config(project_root.clone()).await?;
    let url = trim_url(&cfg.url);
    if url.is_empty() {
        return Err("No relay URL configured.".into());
    }
    let pc_key = read_pc_api_key(project_root);
    if pc_key.is_empty() {
        return Err("RELAY_PC_API_KEY isn't set.".into());
    }
    let code = pair_code.trim().to_uppercase();
    if code.is_empty() {
        return Err("Empty pair_code.".into());
    }

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
        .map_err(|e| e.to_string())?;
    let resp = client
        .get(format!("{}/pair/status/{}", url, code))
        .header("X-API-Key", &pc_key)
        .send()
        .await
        .map_err(|e| format!("Network error: {}", e))?;
    let status = resp.status();
    let text = resp.text().await.unwrap_or_default();
    if status.as_u16() == 404 {
        // Treat unknown codes as expired so the UI gives up cleanly instead
        // of looping forever on a stale code.
        return Ok(RelayPairStatus {
            claimed: false,
            expired: true,
            device_name: None,
            claimed_at: None,
        });
    }
    if !status.is_success() {
        return Err(format!("Relay /pair/status returned {}: {}", status.as_u16(), text));
    }
    let parsed: RelayPairStatus = serde_json::from_str(&text)
        .map_err(|e| format!("Relay returned unparseable JSON: {} (raw: {})", e, text))?;
    Ok(parsed)
}

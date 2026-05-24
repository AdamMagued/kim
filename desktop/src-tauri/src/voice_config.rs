//! Voice configuration helpers — read/write the `voice:` block in config.yaml.
//!
//! Extracted from lib.rs (Phase 8 restructure).
//! Public Tauri commands: `read_voice_config`, `write_voice_config`.

use std::fs;
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct VoiceConfig {
    pub enabled: bool,
    pub engine: String,    // "kokoro" | "maya1" | "http" | "hume"
    pub voice_id: String,  // kokoro voice_id OR hume voice_name OR http voice
}

/// Extract a scalar value from a single line inside a top-level `voice:` block.
/// We use this instead of a full YAML parser to keep dependencies minimal and
/// to preserve the user's existing comments/ordering on write.
fn extract_voice_scalar<'a>(yaml: &'a str, key: &str) -> Option<&'a str> {
    let mut in_voice = false;
    for line in yaml.lines() {
        // A new top-level key ends the voice block.
        if !line.starts_with(char::is_whitespace) && line.trim_end().ends_with(':') {
            in_voice = line.trim_end() == "voice:";
            continue;
        }
        if in_voice {
            // Accept direct children indented with 1–4 spaces or a tab.
            let trimmed = line.trim_start_matches([' ', '\t']);
            let indent = line.len() - trimmed.len();
            if (1..=4).contains(&indent) {
                if let Some(rest) = trimmed.strip_prefix(&format!("{}:", key)) {
                    let v = rest.trim().trim_matches(|c| c == '"' || c == '\'');
                    return Some(v);
                }
            }
        }
    }
    None
}

#[tauri::command]
pub async fn read_voice_config(project_root: Option<String>) -> Result<VoiceConfig, String> {
    let path = crate::config_yaml_path(project_root);
    if !path.exists() {
        return Ok(VoiceConfig {
            enabled: true,
            engine: "kokoro".to_string(),
            voice_id: "af_heart".to_string(),
        });
    }
    let yaml = fs::read_to_string(&path).map_err(|e| e.to_string())?;

    let enabled = extract_voice_scalar(&yaml, "enabled")
        .map(|v| v.eq_ignore_ascii_case("true"))
        .unwrap_or(true);
    let engine = extract_voice_scalar(&yaml, "engine")
        .unwrap_or("kokoro")
        .to_string();
    let voice_id = extract_voice_scalar(&yaml, "voice_id")
        .unwrap_or("af_heart")
        .to_string();

    Ok(VoiceConfig { enabled, engine, voice_id })
}

/// Replace one `  key: value` line inside the `voice:` block, or insert it
/// if missing. Preserves all other lines (comments, formatting, order).
fn upsert_voice_scalar(yaml: &str, key: &str, value: &str) -> String {
    let mut out: Vec<String> = Vec::with_capacity(yaml.lines().count() + 1);
    let mut in_voice = false;
    let mut voice_block_start: Option<usize> = None;
    let mut voice_block_end: Option<usize> = None;
    let mut replaced = false;

    for line in yaml.lines() {
        if !line.starts_with(char::is_whitespace) && line.trim_end().ends_with(':') {
            if in_voice {
                voice_block_end = Some(out.len());
            }
            in_voice = line.trim_end() == "voice:";
            if in_voice {
                voice_block_start = Some(out.len());
            }
            out.push(line.to_string());
            continue;
        }

        if in_voice {
            let trimmed = line.trim_start();
            let indent = line.len() - trimmed.len();
            if indent == 2 && trimmed.starts_with(&format!("{}:", key)) {
                out.push(format!("  {}: {}", key, value));
                replaced = true;
                continue;
            }
        }
        out.push(line.to_string());
    }
    if in_voice {
        voice_block_end = Some(out.len());
    }

    if !replaced {
        match (voice_block_start, voice_block_end) {
            (Some(_), Some(end)) => {
                out.insert(end, format!("  {}: {}", key, value));
            }
            _ => {
                // No voice: block at all — append one.
                out.push("voice:".to_string());
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

#[tauri::command]
pub async fn write_voice_config(
    config: VoiceConfig,
    project_root: Option<String>,
) -> Result<(), String> {
    let path = crate::config_yaml_path(project_root);

    let original = if path.exists() {
        fs::read_to_string(&path).map_err(|e| e.to_string())?
    } else {
        String::from("voice:\n")
    };

    let mut updated = upsert_voice_scalar(&original, "enabled", if config.enabled { "true" } else { "false" });
    updated = upsert_voice_scalar(&updated, "engine", &config.engine);
    updated = upsert_voice_scalar(&updated, "voice_id", &config.voice_id);

    fs::write(&path, updated).map_err(|e| e.to_string())?;
    Ok(())
}

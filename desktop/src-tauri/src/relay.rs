//! Phone-relay configuration and pairing commands.
//!
//! Extracted from lib.rs (Phase 8 restructure).
//! Public Tauri commands: `read_relay_config`, `write_relay_url`,
//! `relay_pair_init`, `relay_pair_status`.

use serde::{Deserialize, Serialize};
use std::fs;
use std::path::PathBuf;
use std::time::Duration;

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
        if !line.starts_with(char::is_whitespace) {
            // Any non-indented line ends the previous block.
            in_block = line.trim_end().ends_with(':') && line.trim_end() == block_marker;
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
        if !line.starts_with(char::is_whitespace) {
            // Any non-indented line ends the previous block.
            if in_block {
                block_end = Some(out.len());
            }
            in_block = line.trim_end().ends_with(':') && line.trim_end() == block_marker;
            if in_block {
                block_start = Some(out.len());
            }
            out.push(line.to_string());
            continue;
        }

        if in_block {
            let trimmed = line.trim_start_matches([' ', '\t']);
            let indent = line.len() - trimmed.len();
            if (1..=4).contains(&indent) && trimmed.starts_with(&key_prefix) {
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
                return rest
                    .trim()
                    .trim_matches(|c| c == '"' || c == '\'')
                    .to_string();
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
    Ok(RelayConfig {
        url,
        pc_key_configured,
    })
}

#[tauri::command]
pub async fn write_relay_url(url: String, project_root: Option<String>) -> Result<(), String> {
    let trimmed = url.trim();
    // Allow clearing the URL (empty string = relay disabled) but reject any
    // non-https value so a bad URL can never be persisted and later used to
    // leak the PC API key.
    if !trimmed.is_empty() {
        require_https(trimmed)?;
    }
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

/// Reject relay URLs that are not https to prevent sending the PC API key in
/// cleartext or to an attacker-controlled endpoint via an http:// URL.
fn require_https(url: &str) -> Result<(), String> {
    if !url.starts_with("https://") {
        return Err(format!(
            "Relay URL must start with https:// (got {:?}). \
             Refusing to send API key over an insecure connection.",
            url
        ));
    }
    Ok(())
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
    require_https(url)?;
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
        return Err(format!(
            "Relay /pair/init returned {}: {}",
            status.as_u16(),
            text
        ));
    }
    #[derive(Deserialize)]
    struct Body {
        pair_code: String,
        expires_at: String,
    }
    let body: Body = serde_json::from_str(&text)
        .map_err(|e| format!("Relay returned unparseable JSON: {} (raw: {})", e, text))?;
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
    require_https(url)?;
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
        return Err(format!(
            "Relay /pair/status returned {}: {}",
            status.as_u16(),
            text
        ));
    }
    let parsed: RelayPairStatus = serde_json::from_str(&text)
        .map_err(|e| format!("Relay returned unparseable JSON: {} (raw: {})", e, text))?;
    Ok(parsed)
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── 1. require_https ──────────────────────────────────────────────────────

    /// http:// must be rejected; https:// must be accepted.
    /// Empty string is intentionally NOT passed to require_https — write_relay_url
    /// skips the call when the value is empty, allowing the URL to be cleared.
    #[test]
    fn require_https_rejects_http() {
        assert!(
            require_https("http://x").is_err(),
            "plain http should be rejected"
        );
        assert!(
            require_https("http://example.com/relay").is_err(),
            "http with a full path should be rejected"
        );
    }

    #[test]
    fn require_https_accepts_https() {
        assert!(
            require_https("https://x").is_ok(),
            "bare https host should be accepted"
        );
        assert!(
            require_https("https://kim-relay.fly.dev").is_ok(),
            "real-world https URL should be accepted"
        );
    }

    /// Non-http / non-https schemes (ws://, ftp://, bare hostname) must also fail.
    #[test]
    fn require_https_rejects_other_schemes() {
        assert!(require_https("ws://example.com").is_err());
        assert!(require_https("ftp://example.com").is_err());
        assert!(require_https("example.com").is_err());
    }

    // ── 2. upsert_block_scalar keeps the key inside the relay block ───────────

    /// Insert a new url key into an existing relay block that is followed by a
    /// trailing top-level scalar (max_iterations: 30).  The inserted line must
    /// appear between the relay: marker and max_iterations:, never after it.
    #[test]
    fn upsert_inserts_url_in_relay_block_before_trailing_top_level_scalar() {
        let yaml = "relay:\nmax_iterations: 30\n";
        let result = upsert_block_scalar(yaml, "relay", "url", "https://r.example.com");

        // The url line must exist somewhere in the output.
        assert!(
            result.contains("  url: https://r.example.com"),
            "url line missing from output:\n{result}"
        );

        // It must come BEFORE max_iterations, not after.
        let url_pos = result.find("  url:").expect("url line should be present");
        let max_pos = result
            .find("max_iterations:")
            .expect("max_iterations should be preserved");
        assert!(
            url_pos < max_pos,
            "url was inserted after max_iterations instead of inside the relay block:\n{result}"
        );
    }

    /// Replacing an existing url key must also stay inside the relay block.
    #[test]
    fn upsert_replaces_url_in_relay_block_before_trailing_top_level_scalar() {
        let yaml = "relay:\n  url: https://old.example.com\nmax_iterations: 30\n";
        let result = upsert_block_scalar(yaml, "relay", "url", "https://new.example.com");

        assert!(
            result.contains("  url: https://new.example.com"),
            "replaced url missing:\n{result}"
        );
        assert!(
            !result.contains("https://old.example.com"),
            "old url should be gone:\n{result}"
        );

        let url_pos = result.find("  url:").unwrap();
        let max_pos = result.find("max_iterations:").unwrap();
        assert!(
            url_pos < max_pos,
            "url ended up after max_iterations:\n{result}"
        );

        // url must not appear before the relay: marker either.
        let relay_pos = result.find("relay:").unwrap();
        assert!(
            url_pos > relay_pos,
            "url appeared before relay: marker:\n{result}"
        );
    }

    /// The url line must not be emitted at the top level (zero indentation).
    #[test]
    fn upsert_url_never_lands_at_top_level() {
        let yaml = "relay:\n  other: val\nmax_iterations: 30\n";
        let result = upsert_block_scalar(yaml, "relay", "url", "https://r.example.com");

        // Every line that contains "url:" must start with whitespace (be indented).
        for line in result.lines() {
            if line.contains("url:") {
                assert!(
                    line.starts_with(' ') || line.starts_with('\t'),
                    "url ended up at top level in line: {line:?}\nfull output:\n{result}"
                );
            }
        }
    }

    // ── 3. in_block reset on any non-indented line ────────────────────────────

    /// A non-indented scalar line (e.g. `some_key: value`) that is NOT a block
    /// marker must end the relay block.  Any subsequent indented content must
    /// therefore NOT be attributed to relay.
    #[test]
    fn in_block_reset_on_any_nonindented_line() {
        // `some_key: value` is non-indented but is NOT a block marker (does not
        // end with just ':').  It must end the relay block so the indented url
        // that follows is not returned.
        let yaml = "relay:\n  other: val\nsome_key: value\n  url: https://trap.com\n";
        let result = extract_block_scalar(yaml, "relay", "url");
        assert_eq!(
            result, None,
            "url found after in_block should have been reset by non-indented scalar: {result:?}"
        );
    }

    /// Corollary: the url IS returned when it appears before the non-indented
    /// terminator — confirms the reset happens at the right moment.
    #[test]
    fn in_block_returns_value_before_nonindented_terminator() {
        let yaml = "relay:\n  url: https://good.com\nsome_key: value\n";
        let result = extract_block_scalar(yaml, "relay", "url");
        assert_eq!(result, Some("https://good.com"));
    }

    /// A value that lives under a different top-level block must not be returned
    /// when querying the relay block, even when the key name is the same.
    #[test]
    fn in_block_does_not_bleed_into_other_blocks() {
        let yaml = "relay:\n  other: x\nvoice:\n  url: https://voice-trap.com\n";
        let result = extract_block_scalar(yaml, "relay", "url");
        assert_eq!(
            result, None,
            "url from a different block should not be returned: {result:?}"
        );
    }

    // ── 4. indent range 1..=4 accepted symmetrically ─────────────────────────

    /// extract_block_scalar must accept any indentation from 1 to 4 spaces.
    #[test]
    fn indent_range_accepted_by_extract() {
        for indent in 1usize..=4 {
            let spaces = " ".repeat(indent);
            let yaml = format!("relay:\n{spaces}url: https://ind{indent}.com\n");
            let result = extract_block_scalar(&yaml, "relay", "url");
            let expected = format!("https://ind{indent}.com");
            assert_eq!(
                result,
                Some(expected.as_str()),
                "extract failed for {indent}-space indent"
            );
        }
    }

    /// upsert_block_scalar must recognise and replace an existing key regardless
    /// of whether it was written with 1, 2, 3, or 4 spaces of indentation.
    /// The output is always normalised to 2-space indentation.
    #[test]
    fn indent_range_accepted_by_upsert_replaces_and_normalises() {
        for indent in 1usize..=4 {
            let spaces = " ".repeat(indent);
            let yaml = format!("relay:\n{spaces}url: https://old.com\n");
            let result = upsert_block_scalar(&yaml, "relay", "url", "https://new.com");

            // Must contain the new value.
            assert!(
                result.contains("  url: https://new.com"),
                "replacement missing for {indent}-space indent:\n{result}"
            );
            // Old value must be gone.
            assert!(
                !result.contains("https://old.com"),
                "old value still present for {indent}-space indent:\n{result}"
            );
            // Output is normalised to exactly 2 spaces.
            let url_line = result
                .lines()
                .find(|l| l.contains("url:"))
                .expect("url line must exist");
            assert!(
                url_line.starts_with("  ") && !url_line.starts_with("   "),
                "expected exactly 2-space indent in output for original {indent}-space indent; \
                 got: {url_line:?}"
            );
        }
    }

    /// Inserting a new key into a relay block that was written with non-standard
    /// indentation must still place the key correctly.
    #[test]
    fn indent_range_accepted_by_upsert_inserts_when_key_absent() {
        for indent in 1usize..=4 {
            let spaces = " ".repeat(indent);
            let yaml = format!("relay:\n{spaces}other: val\n");
            let result = upsert_block_scalar(&yaml, "relay", "url", "https://new.com");

            assert!(
                result.contains("  url: https://new.com"),
                "inserted url missing for {indent}-space existing indent:\n{result}"
            );
        }
    }
}

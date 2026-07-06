//! Ollama local daemon status, sign-in launcher, and model management.
//!
//! Extracted from lib.rs (Phase 8 restructure).
//! Public Tauri commands: `ollama_get_status`, `ollama_test_model`,
//! `ollama_signin`, `ollama_pull_model`.

use serde::{Deserialize, Serialize};
use std::time::Duration;
use tauri::Emitter;

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub(crate) struct OllamaModelInfo {
    pub(crate) name: String,
    pub(crate) size: u64,
    pub(crate) modified_at: Option<String>,
    pub(crate) family: Option<String>,
    pub(crate) parameter_size: Option<String>,
    pub(crate) quantization_level: Option<String>,
    pub(crate) cloud: bool,
    pub(crate) installed: bool,
}

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct OllamaStatus {
    installed: bool,
    running: bool,
    version: Option<String>,
    state: String,
    message: String,
    installed_path: Option<String>,
    local_models: Vec<OllamaModelInfo>,
    cloud_models: Vec<OllamaModelInfo>,
    cloud_connected: bool,
    cloud_message: Option<String>,
    selected_model: Option<String>,
    selected_model_available: bool,
    selected_mode: String,
    context_limit: Option<u32>,
    context_limit_source: Option<String>,
    error: Option<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
struct OllamaPullProgress {
    model: String,
    line: String,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
struct OllamaPullFinished {
    model: String,
    success: bool,
    error: Option<String>,
}

#[derive(Deserialize, Default)]
struct OllamaTagsResponse {
    #[serde(default)]
    models: Vec<OllamaTagModel>,
}

#[derive(Deserialize, Default)]
struct OllamaTagModel {
    name: String,
    #[serde(default)]
    size: u64,
    #[serde(default)]
    modified_at: Option<String>,
    #[serde(default)]
    details: Option<OllamaTagDetails>,
}

#[derive(Deserialize, Default)]
struct OllamaTagDetails {
    #[serde(default)]
    family: Option<String>,
    #[serde(default)]
    parameter_size: Option<String>,
    #[serde(default)]
    quantization_level: Option<String>,
}

#[derive(Deserialize, Default)]
struct OllamaVersionResponse {
    #[serde(default)]
    version: Option<String>,
}

#[derive(Deserialize, Default)]
struct OllamaShowResponse {
    #[serde(default)]
    parameters: Option<String>,
    #[serde(default)]
    modelfile: Option<String>,
}

/// Suggested Ollama cloud models shown in the picker (#32).
///
/// Display suggestions only — the daemon is authoritative and the selected
/// model is validated at request time (a wrong name fails there, not here).
/// Trimmed to real `-cloud` tags: the fabricated `deepseek-coder-v4:cloud` and
/// `mistral-large:latest-cloud` (non-existent / malformed tags) were removed.
/// Keep in sync with the CLI copy in `cli/src/commands.rs`.
fn known_ollama_cloud_models() -> Vec<String> {
    vec![
        // OpenAI open-source models (Ollama cloud routing)
        "gpt-oss:20b-cloud".to_string(),
        "gpt-oss:120b-cloud".to_string(),
        // Llama (Meta)
        "llama3.3:70b-cloud".to_string(),
        "llama3.1:405b-cloud".to_string(),
        // Qwen (Alibaba)
        "qwen2.5:72b-cloud".to_string(),
        "qwen2.5-coder:32b-cloud".to_string(),
        // DeepSeek
        "deepseek-r1:671b-cloud".to_string(),
        "deepseek-v3:685b-cloud".to_string(),
        // Gemma (Google)
        "gemma3:27b-cloud".to_string(),
    ]
}

fn find_ollama_binary() -> Option<String> {
    #[cfg(windows)]
    let probe = ("where", vec!["ollama"]);
    #[cfg(not(windows))]
    let probe = ("which", vec!["ollama"]);

    let out = std::process::Command::new(probe.0)
        .args(probe.1)
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let first = String::from_utf8_lossy(&out.stdout)
        .lines()
        .map(str::trim)
        .find(|line| !line.is_empty())?
        .to_string();
    Some(first)
}

fn parse_ollama_num_ctx(text: &str) -> Option<u32> {
    for line in text.lines() {
        let normalized = line.trim().replace(['=', ':'], " ");
        let parts: Vec<&str> = normalized.split_whitespace().collect();
        for idx in 0..parts.len().saturating_sub(1) {
            if parts[idx] == "num_ctx" {
                if let Ok(n) = parts[idx + 1].parse::<u32>() {
                    return Some(n);
                }
            }
        }
    }
    None
}

fn parse_context_column(raw: &str) -> Option<u32> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return None;
    }
    let (num, mult) = if let Some(num) = trimmed.strip_suffix(['K', 'k']) {
        (num, 1_000f64)
    } else if let Some(num) = trimmed.strip_suffix(['M', 'm']) {
        (num, 1_000_000f64)
    } else {
        (trimmed, 1f64)
    };
    let parsed = num.trim().parse::<f64>().ok()?;
    Some((parsed * mult).round() as u32)
}

fn parse_ollama_ps_context(stdout: &str, model: &str) -> Option<u32> {
    let wanted = model.trim().to_lowercase();
    for line in stdout.lines() {
        let stripped = line.trim();
        if stripped.is_empty() || stripped.to_ascii_lowercase().starts_with("name ") {
            continue;
        }
        if !stripped.to_ascii_lowercase().starts_with(&wanted) {
            continue;
        }
        let cols: Vec<&str> = stripped.split_whitespace().collect();
        if cols.len() < 2 {
            continue;
        }
        return cols.last().and_then(|s| parse_context_column(s));
    }
    None
}

async fn ollama_context_from_ps(model: &str) -> Option<u32> {
    let out = tokio::time::timeout(
        Duration::from_secs(5),
        tokio::process::Command::new("ollama").arg("ps").output(),
    )
    .await
    .ok()?
    .ok()?;
    if !out.status.success() {
        return None;
    }
    parse_ollama_ps_context(&String::from_utf8_lossy(&out.stdout), model)
}

async fn ollama_context_from_show(base_url: &str, model: &str) -> Option<u32> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
        .ok()?;
    let resp = client
        .post(format!("{}/api/show", base_url.trim_end_matches('/')))
        .json(&serde_json::json!({ "model": model }))
        .send()
        .await
        .ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let payload: OllamaShowResponse = resp.json().await.ok()?;
    payload
        .parameters
        .as_deref()
        .and_then(parse_ollama_num_ctx)
        .or_else(|| payload.modelfile.as_deref().and_then(parse_ollama_num_ctx))
}

async fn ollama_version(base_url: &str) -> Result<Option<String>, String> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
        .map_err(|e| e.to_string())?;
    let resp = client
        .get(format!("{}/api/version", base_url.trim_end_matches('/')))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    if !resp.status().is_success() {
        return Err(format!("HTTP {}", resp.status()));
    }
    let payload: OllamaVersionResponse = resp.json().await.map_err(|e| e.to_string())?;
    Ok(payload.version)
}

pub(crate) async fn ollama_tags(base_url: &str) -> Result<Vec<OllamaModelInfo>, String> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
        .map_err(|e| e.to_string())?;
    let resp = client
        .get(format!("{}/api/tags", base_url.trim_end_matches('/')))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    if !resp.status().is_success() {
        return Err(format!("HTTP {}", resp.status()));
    }
    let payload: OllamaTagsResponse = resp.json().await.map_err(|e| e.to_string())?;
    Ok(payload
        .models
        .into_iter()
        .map(|m| OllamaModelInfo {
            name: m.name,
            size: m.size,
            modified_at: m.modified_at,
            family: m.details.as_ref().and_then(|d| d.family.clone()),
            parameter_size: m.details.as_ref().and_then(|d| d.parameter_size.clone()),
            quantization_level: m
                .details
                .as_ref()
                .and_then(|d| d.quantization_level.clone()),
            cloud: false,
            installed: true,
        })
        .collect())
}

async fn ollama_chat_probe(base_url: &str, model: &str) -> Result<(), String> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .build()
        .map_err(|e| e.to_string())?;
    let resp = client
        .post(format!("{}/api/chat", base_url.trim_end_matches('/')))
        .json(&serde_json::json!({
            "model": model,
            "messages": [{ "role": "user", "content": "Reply with OK." }],
            "stream": false
        }))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    if resp.status().is_success() {
        return Ok(());
    }
    let status = resp.status();
    let detail = resp.text().await.unwrap_or_default();
    Err(if detail.trim().is_empty() {
        format!("HTTP {}", status)
    } else {
        detail
    })
}

/// Classify `ollama whoami` output into a sign-in verdict (#22).
///
/// Ok(true)  — signed in (command succeeded and printed an account).
/// Ok(false) — definitively not signed in.
/// Err(_)    — indeterminate (old CLI without `whoami`, timeout, other error);
///             callers should fall back to the previous liveness-only behavior
///             rather than hard-blocking cloud tasks.
fn classify_whoami_output(success: bool, stdout: &str, stderr: &str) -> Result<bool, String> {
    if success {
        return Ok(!stdout.trim().is_empty());
    }
    let combined = format!("{} {}", stdout, stderr).to_lowercase();
    if combined.contains("not signed in")
        || combined.contains("sign in")
        || combined.contains("signin")
        || combined.contains("unauthorized")
        || combined.contains("401")
    {
        return Ok(false);
    }
    if combined.contains("unknown command") || combined.contains("unknown flag") {
        return Err("ollama CLI does not support `whoami`".to_string());
    }
    Err(if combined.trim().is_empty() {
        "whoami failed".to_string()
    } else {
        combined
    })
}

/// Real ollama.com sign-in probe: `ollama whoami` (#22).
/// The previous check hit `/api/version`, which is a daemon-liveness endpoint
/// that never requires an account — it reported "connected" even when the
/// user had never run `ollama signin`.
async fn ollama_cloud_signed_in(binary: &str) -> Result<bool, String> {
    let out = tokio::time::timeout(
        Duration::from_secs(5),
        tokio::process::Command::new(binary).arg("whoami").output(),
    )
    .await
    .map_err(|_| "whoami timed out".to_string())?
    .map_err(|e| e.to_string())?;
    classify_whoami_output(
        out.status.success(),
        &String::from_utf8_lossy(&out.stdout),
        &String::from_utf8_lossy(&out.stderr),
    )
}

#[tauri::command]
pub async fn ollama_get_status(
    base_url: Option<String>,
    selected_model: Option<String>,
    mode: Option<String>,
    context_limit_override: Option<u32>,
    app_handle: tauri::AppHandle,
) -> Result<OllamaStatus, String> {
    use tauri::Manager;
    let app_config = app_handle.state::<crate::config::AppConfig>();
    let ollama_fallback = app_config
        .default_model
        .get("ollama")
        .cloned()
        .unwrap_or_else(|| "gpt-oss:120b-cloud".to_string());
    let base_url = base_url.unwrap_or_else(|| "http://localhost:11434".to_string());
    let selected_mode = mode.unwrap_or_else(|| "local".to_string()).to_lowercase();
    let installed_path = find_ollama_binary();
    if installed_path.is_none() {
        return Ok(OllamaStatus {
            installed: false,
            running: false,
            version: None,
            state: "not_installed".to_string(),
            message: "Ollama is not installed".to_string(),
            installed_path: None,
            local_models: vec![],
            cloud_models: known_ollama_cloud_models()
                .into_iter()
                .map(|name| OllamaModelInfo {
                    name,
                    size: 0,
                    modified_at: None,
                    family: None,
                    parameter_size: None,
                    quantization_level: None,
                    cloud: true,
                    installed: false,
                })
                .collect(),
            cloud_connected: false,
            cloud_message: Some("Install Ollama from ollama.com/download".to_string()),
            selected_model,
            selected_model_available: false,
            selected_mode,
            context_limit: context_limit_override,
            context_limit_source: context_limit_override.map(|_| "override".to_string()),
            error: None,
        });
    }

    let version = match ollama_version(&base_url).await {
        Ok(version) => version,
        Err(_) => {
            return Ok(OllamaStatus {
                installed: true,
                running: false,
                version: None,
                state: "installed_not_running".to_string(),
                message: "Ollama is installed but not running".to_string(),
                installed_path,
                local_models: vec![],
                cloud_models: known_ollama_cloud_models()
                    .into_iter()
                    .map(|name| OllamaModelInfo {
                        name,
                        size: 0,
                        modified_at: None,
                        family: None,
                        parameter_size: None,
                        quantization_level: None,
                        cloud: true,
                        installed: false,
                    })
                    .collect(),
                cloud_connected: false,
                cloud_message: Some("Start Ollama to use local or cloud models".to_string()),
                selected_model,
                selected_model_available: false,
                selected_mode,
                context_limit: context_limit_override,
                context_limit_source: context_limit_override.map(|_| "override".to_string()),
                error: None,
            });
        }
    };

    let local_models = ollama_tags(&base_url).await.unwrap_or_default();
    let local_names: std::collections::HashSet<String> =
        local_models.iter().map(|m| m.name.to_lowercase()).collect();
    let mut cloud_models: Vec<OllamaModelInfo> = known_ollama_cloud_models()
        .into_iter()
        .map(|name| OllamaModelInfo {
            installed: local_names.contains(&name.to_lowercase()),
            cloud: true,
            name,
            size: 0,
            modified_at: None,
            family: None,
            parameter_size: None,
            quantization_level: None,
        })
        .collect();
    if let Some(extra) = selected_model.as_ref().filter(|m| !m.trim().is_empty()) {
        if !cloud_models.iter().any(|m| m.name == *extra) {
            cloud_models.push(OllamaModelInfo {
                installed: local_names.contains(&extra.to_lowercase()),
                cloud: true,
                name: extra.clone(),
                size: 0,
                modified_at: None,
                family: None,
                parameter_size: None,
                quantization_level: None,
            });
        }
    }

    let selected = selected_model
        .clone()
        .filter(|m| !m.trim().is_empty())
        .or_else(|| {
            if selected_mode == "cloud" {
                Some(ollama_fallback.clone())
            } else {
                local_models.first().map(|m| m.name.clone())
            }
        });
    let selected_available = selected
        .as_ref()
        .map(|m| {
            if selected_mode == "cloud" {
                !m.trim().is_empty()
            } else {
                local_names.contains(&m.to_lowercase())
            }
        })
        .unwrap_or(false);

    // Cloud mode: check the real ollama.com sign-in state via `ollama whoami`
    // (#22). Free and local — no generation request, no token cost. If the
    // installed CLI predates `whoami` (or the probe errors), fall back to the
    // old daemon-liveness semantics instead of hard-blocking cloud tasks.
    // Explicit model validation is available via `ollama_test_model`.
    let (cloud_connected, cloud_message) = if selected_mode == "cloud" {
        let binary = installed_path.as_deref().unwrap_or("ollama");
        match ollama_cloud_signed_in(binary).await {
            Ok(true) => (true, Some("Connected to Ollama".to_string())),
            Ok(false) => (
                false,
                Some("Sign in to Ollama to use cloud models".to_string()),
            ),
            Err(_) => (true, Some("Connected to Ollama".to_string())),
        }
    } else {
        (
            false,
            Some("Local models work without any Ollama account.".to_string()),
        )
    };

    let (context_limit, context_limit_source) = if let Some(model) = selected.as_ref() {
        if let Some(limit) = ollama_context_from_ps(model).await {
            (Some(limit), Some("ollama_ps".to_string()))
        } else if let Some(limit) = ollama_context_from_show(&base_url, model).await {
            (Some(limit), Some("api_show".to_string()))
        } else if let Some(limit) = context_limit_override {
            (Some(limit), Some("override".to_string()))
        } else {
            (None, Some("unknown".to_string()))
        }
    } else if let Some(limit) = context_limit_override {
        (Some(limit), Some("override".to_string()))
    } else {
        (None, Some("unknown".to_string()))
    };

    let state = if selected_mode == "cloud" {
        if cloud_connected {
            "connected"
        } else {
            "running_not_signed_in"
        }
    } else if !local_models.is_empty() {
        "connected"
    } else {
        "error"
    };
    let message = match state {
        "connected" => "Connected to Ollama".to_string(),
        "running_not_signed_in" => "Sign in to Ollama to use cloud models".to_string(),
        _ => {
            if local_models.is_empty() {
                "Ollama is running, but no local models are installed".to_string()
            } else {
                "Ollama status unavailable".to_string()
            }
        }
    };

    Ok(OllamaStatus {
        installed: true,
        running: true,
        version,
        state: state.to_string(),
        message,
        installed_path,
        local_models,
        cloud_models,
        cloud_connected,
        cloud_message,
        selected_model: selected,
        selected_model_available: selected_available,
        selected_mode,
        context_limit,
        context_limit_source,
        error: None,
    })
}

#[tauri::command]
pub async fn ollama_test_model(base_url: Option<String>, model: String) -> Result<bool, String> {
    let base_url = base_url.unwrap_or_else(|| "http://localhost:11434".to_string());
    ollama_chat_probe(&base_url, &model).await.map(|_| true)
}

#[tauri::command]
pub async fn ollama_signin() -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        let script = r#"tell application "Terminal"
activate
do script "ollama signin"
end tell"#;
        std::process::Command::new("osascript")
            .arg("-e")
            .arg(script)
            .spawn()
            .map_err(|e| e.to_string())?;
        Ok(())
    }
    #[cfg(target_os = "windows")]
    {
        std::process::Command::new("cmd")
            .args(["/C", "start", "", "cmd", "/K", "ollama signin"])
            .spawn()
            .map_err(|e| e.to_string())?;
        Ok(())
    }
    #[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
    {
        let candidates = [
            (
                "x-terminal-emulator",
                vec!["-e", "sh", "-lc", "ollama signin"],
            ),
            ("gnome-terminal", vec!["--", "sh", "-lc", "ollama signin"]),
            ("konsole", vec!["-e", "sh", "-lc", "ollama signin"]),
        ];
        for (cmd, args) in candidates {
            if std::process::Command::new(cmd)
                .args(args.clone())
                .spawn()
                .is_ok()
            {
                return Ok(());
            }
        }
        Err("Could not launch a terminal for `ollama signin`.".to_string())
    }
}

#[tauri::command]
pub async fn ollama_pull_model(model: String, app_handle: tauri::AppHandle) -> Result<(), String> {
    let model_name = model.trim().to_string();
    if model_name.is_empty() {
        return Err("Model name is required".to_string());
    }
    tokio::spawn(async move {
        use tokio::io::AsyncBufReadExt;
        use tokio::process::Command;

        let mut cmd = Command::new("ollama");
        cmd.arg("pull")
            .arg(&model_name)
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped());

        match cmd.spawn() {
            Ok(mut child) => {
                if let Some(stdout) = child.stdout.take() {
                    let app = app_handle.clone();
                    let model = model_name.clone();
                    tokio::spawn(async move {
                        let mut lines = tokio::io::BufReader::new(stdout).lines();
                        while let Ok(Some(line)) = lines.next_line().await {
                            let _ = app.emit(
                                "ollama-pull-progress",
                                OllamaPullProgress {
                                    model: model.clone(),
                                    line,
                                },
                            );
                        }
                    });
                }
                if let Some(stderr) = child.stderr.take() {
                    let app = app_handle.clone();
                    let model = model_name.clone();
                    tokio::spawn(async move {
                        let mut lines = tokio::io::BufReader::new(stderr).lines();
                        while let Ok(Some(line)) = lines.next_line().await {
                            let _ = app.emit(
                                "ollama-pull-progress",
                                OllamaPullProgress {
                                    model: model.clone(),
                                    line,
                                },
                            );
                        }
                    });
                }
                let finished = child.wait().await.map_err(|e| e.to_string());
                match finished {
                    Ok(status) if status.success() => {
                        let _ = app_handle.emit(
                            "ollama-pull-finished",
                            OllamaPullFinished {
                                model: model_name,
                                success: true,
                                error: None,
                            },
                        );
                    }
                    Ok(status) => {
                        let _ = app_handle.emit(
                            "ollama-pull-finished",
                            OllamaPullFinished {
                                model: model_name,
                                success: false,
                                error: Some(format!("`ollama pull` exited with {}", status)),
                            },
                        );
                    }
                    Err(err) => {
                        let _ = app_handle.emit(
                            "ollama-pull-finished",
                            OllamaPullFinished {
                                model: model_name,
                                success: false,
                                error: Some(err),
                            },
                        );
                    }
                }
            }
            Err(err) => {
                let _ = app_handle.emit(
                    "ollama-pull-finished",
                    OllamaPullFinished {
                        model: model_name,
                        success: false,
                        error: Some(err.to_string()),
                    },
                );
            }
        }
    });
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::classify_whoami_output;

    #[test]
    fn whoami_success_with_account_is_signed_in() {
        assert_eq!(
            classify_whoami_output(true, "alice@example.com\n", ""),
            Ok(true)
        );
    }

    #[test]
    fn whoami_success_with_empty_output_is_not_signed_in() {
        assert_eq!(classify_whoami_output(true, "  \n", ""), Ok(false));
    }

    #[test]
    fn whoami_not_signed_in_error_is_definitive_false() {
        assert_eq!(
            classify_whoami_output(false, "", "Error: you are not signed in"),
            Ok(false)
        );
        assert_eq!(
            classify_whoami_output(false, "", "Error: unauthorized"),
            Ok(false)
        );
    }

    #[test]
    fn whoami_unknown_command_is_indeterminate() {
        // Old ollama CLI without `whoami` — must NOT report signed-out,
        // callers fall back to liveness-only semantics.
        assert!(classify_whoami_output(false, "", "Error: unknown command \"whoami\"").is_err());
    }

    #[test]
    fn whoami_other_failure_is_indeterminate() {
        assert!(classify_whoami_output(false, "", "some transient failure").is_err());
    }
}

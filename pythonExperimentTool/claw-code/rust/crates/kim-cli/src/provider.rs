use std::path::{Path, PathBuf};

use base64::Engine;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::config::KimConfig;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChatMessage {
    pub role: String,
    pub content: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ImageAttachment {
    name: String,
    path: PathBuf,
    mime_type: &'static str,
    data_base64: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ProviderInfo {
    pub name: &'static str,
    pub default_model: &'static str,
    pub key_env: Option<&'static str>,
    pub default_base_url: &'static str,
}

pub const PROVIDERS: &[ProviderInfo] = &[
    ProviderInfo {
        name: "ollama",
        default_model: "llama3.2",
        key_env: None,
        default_base_url: "http://127.0.0.1:11434/v1",
    },
    ProviderInfo {
        name: "openai",
        default_model: "gpt-4o-mini",
        key_env: Some("OPENAI_API_KEY"),
        default_base_url: "https://api.openai.com/v1",
    },
    ProviderInfo {
        name: "claude",
        default_model: "claude-3-5-sonnet-latest",
        key_env: Some("ANTHROPIC_API_KEY"),
        default_base_url: "https://api.anthropic.com/v1",
    },
    ProviderInfo {
        name: "gemini",
        default_model: "gemini-2.0-flash",
        key_env: Some("GEMINI_API_KEY"),
        default_base_url: "https://generativelanguage.googleapis.com/v1beta/openai",
    },
    ProviderInfo {
        name: "deepseek",
        default_model: "deepseek-chat",
        key_env: Some("DEEPSEEK_API_KEY"),
        default_base_url: "https://api.deepseek.com/v1",
    },
    ProviderInfo {
        name: "desktop",
        default_model: "kim-bridge",
        key_env: None,
        default_base_url: "http://127.0.0.1:18991",
    },
];

pub fn provider_info(name: &str) -> Option<ProviderInfo> {
    PROVIDERS
        .iter()
        .copied()
        .find(|provider| provider.name == name.trim().to_ascii_lowercase())
}

pub async fn send_chat(config: &KimConfig, messages: &[ChatMessage]) -> Result<String, String> {
    match config.provider.as_str() {
        "desktop" => send_desktop_bridge(config, messages).await,
        "claude" => send_anthropic(config, messages).await,
        _ => send_openai_compatible(config, messages).await,
    }
}

async fn send_openai_compatible(
    config: &KimConfig,
    messages: &[ChatMessage],
) -> Result<String, String> {
    let provider = provider_info(&config.provider)
        .ok_or_else(|| format!("Unknown provider: {}", config.provider))?;
    let base_url = if provider.name == "ollama" {
        trim_base_url(&config.ollama_base_url)
    } else {
        provider.default_base_url.to_string()
    };
    let api_key = provider
        .key_env
        .and_then(|key| std::env::var(key).ok())
        .or_else(|| config.api_keys.get(provider.name).cloned())
        .unwrap_or_else(|| {
            if provider.name == "ollama" {
                "ollama".to_string()
            } else {
                String::new()
            }
        });
    if api_key.is_empty() {
        return Err(format!("Run /login {} first.", provider.name));
    }

    let client = reqwest::Client::new();
    let request_messages = openai_compatible_messages(messages)?;
    let response = client
        .post(format!(
            "{}/chat/completions",
            base_url.trim_end_matches('/')
        ))
        .bearer_auth(api_key)
        .json(&json!({
            "model": config.model,
            "messages": request_messages,
            "stream": false
        }))
        .send()
        .await
        .map_err(|error| format!("Provider request failed: {error}"))?;

    let status = response.status();
    let body = response
        .text()
        .await
        .map_err(|error| format!("Provider response failed: {error}"))?;
    if !status.is_success() {
        return Err(format!("Provider returned {status}: {body}"));
    }
    let payload: Value =
        serde_json::from_str(&body).map_err(|error| format!("Bad provider JSON: {error}"))?;
    payload["choices"][0]["message"]["content"]
        .as_str()
        .map(ToOwned::to_owned)
        .filter(|text| !text.trim().is_empty())
        .ok_or_else(|| "Provider returned no message content.".to_string())
}

async fn send_anthropic(config: &KimConfig, messages: &[ChatMessage]) -> Result<String, String> {
    let api_key = std::env::var("ANTHROPIC_API_KEY")
        .ok()
        .or_else(|| config.api_keys.get("claude").cloned())
        .ok_or_else(|| "Run /login claude first.".to_string())?;
    let client = reqwest::Client::new();
    let request_messages = anthropic_messages(messages)?;
    let response = client
        .post("https://api.anthropic.com/v1/messages")
        .header("x-api-key", api_key)
        .header("anthropic-version", "2023-06-01")
        .json(&json!({
            "model": config.model,
            "max_tokens": 4096,
            "messages": request_messages,
        }))
        .send()
        .await
        .map_err(|error| format!("Claude request failed: {error}"))?;
    let status = response.status();
    let body = response
        .text()
        .await
        .map_err(|error| format!("Claude response failed: {error}"))?;
    if !status.is_success() {
        return Err(format!("Claude returned {status}: {body}"));
    }
    let payload: Value =
        serde_json::from_str(&body).map_err(|error| format!("Bad Claude JSON: {error}"))?;
    let mut text = String::new();
    if let Some(blocks) = payload["content"].as_array() {
        for block in blocks {
            if let Some(part) = block["text"].as_str() {
                if !text.is_empty() {
                    text.push('\n');
                }
                text.push_str(part);
            }
        }
    }
    if text.trim().is_empty() {
        Err("Claude returned no text content.".to_string())
    } else {
        Ok(text)
    }
}

async fn send_desktop_bridge(
    config: &KimConfig,
    messages: &[ChatMessage],
) -> Result<String, String> {
    let prompt = messages
        .iter()
        .rev()
        .find(|message| message.role == "user")
        .map(|message| message.content.as_str())
        .unwrap_or_default();
    if prompt.trim().is_empty() {
        return Err("Nothing to send to Kim desktop bridge.".to_string());
    }
    let attachments = image_attachments_from_prompt(prompt)?;
    let response = reqwest::Client::new()
        .post(format!(
            "{}/v1/task",
            config.desktop_bridge_url.trim_end_matches('/')
        ))
        .json(&json!({
            "task": prompt,
            "provider": config.provider,
            "model": config.model,
            "attachments": attachments.iter().map(|attachment| json!({
                "name": attachment.name,
                "mime_type": attachment.mime_type,
                "data_base64": attachment.data_base64,
            })).collect::<Vec<_>>(),
        }))
        .send()
        .await
        .map_err(|error| format!("Desktop bridge request failed: {error}"))?;
    let status = response.status();
    let body = response
        .text()
        .await
        .map_err(|error| format!("Desktop bridge response failed: {error}"))?;
    if status.is_success() {
        Ok(format!("Sent to Kim desktop bridge.\n{body}"))
    } else {
        Err(format!("Desktop bridge returned {status}: {body}"))
    }
}

fn openai_compatible_messages(messages: &[ChatMessage]) -> Result<Vec<Value>, String> {
    messages
        .iter()
        .map(|message| {
            let attachments = image_attachments_from_prompt(&message.content)?;
            if attachments.is_empty() {
                return Ok(json!({
                    "role": message.role,
                    "content": message.content,
                }));
            }
            let mut content = vec![json!({
                "type": "text",
                "text": message.content,
            })];
            for attachment in attachments {
                content.push(json!({
                    "type": "image_url",
                    "image_url": {
                        "url": format!("data:{};base64,{}", attachment.mime_type, attachment.data_base64),
                    },
                }));
            }
            Ok(json!({
                "role": message.role,
                "content": content,
            }))
        })
        .collect()
}

fn anthropic_messages(messages: &[ChatMessage]) -> Result<Vec<Value>, String> {
    messages
        .iter()
        .map(|message| {
            let attachments = image_attachments_from_prompt(&message.content)?;
            if attachments.is_empty() {
                return Ok(json!({
                    "role": message.role,
                    "content": message.content,
                }));
            }
            let mut content = vec![json!({
                "type": "text",
                "text": message.content,
            })];
            for attachment in attachments {
                content.push(json!({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": attachment.mime_type,
                        "data": attachment.data_base64,
                    },
                }));
            }
            Ok(json!({
                "role": message.role,
                "content": content,
            }))
        })
        .collect()
}

fn image_attachments_from_prompt(prompt: &str) -> Result<Vec<ImageAttachment>, String> {
    let mut paths = prompt
        .lines()
        .filter_map(|line| line.trim().strip_prefix("- "))
        .filter_map(|line| {
            let path = PathBuf::from(line.trim());
            image_mime_type(&path).map(|mime_type| (path, mime_type))
        })
        .collect::<Vec<_>>();
    paths.sort_by(|left, right| left.0.cmp(&right.0));
    paths.dedup_by(|left, right| left.0 == right.0);
    paths
        .into_iter()
        .map(|(path, mime_type)| {
            let bytes = std::fs::read(&path)
                .map_err(|error| format!("Could not read image {}: {error}", path.display()))?;
            Ok(ImageAttachment {
                name: path
                    .file_name()
                    .and_then(|name| name.to_str())
                    .unwrap_or("image")
                    .to_string(),
                path,
                mime_type,
                data_base64: base64::engine::general_purpose::STANDARD.encode(bytes),
            })
        })
        .collect()
}

fn image_mime_type(path: &Path) -> Option<&'static str> {
    match path
        .extension()
        .and_then(|extension| extension.to_str())
        .map(str::to_ascii_lowercase)
        .as_deref()
    {
        Some("png") => Some("image/png"),
        Some("jpg" | "jpeg") => Some("image/jpeg"),
        Some("webp") => Some("image/webp"),
        Some("gif") => Some("image/gif"),
        _ => None,
    }
}

fn trim_base_url(base_url: &str) -> String {
    let trimmed = base_url.trim().trim_end_matches('/');
    if trimmed.ends_with("/v1") {
        trimmed.to_string()
    } else {
        format!("{trimmed}/v1")
    }
}

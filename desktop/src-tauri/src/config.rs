use serde::Deserialize;
use std::collections::HashMap;
use std::fs;
use std::path::Path;

fn default_bridge_timeout_secs() -> u64 {
    720
}
fn default_screenshot_flash_duration_ms() -> u64 {
    3300
}
fn default_max_iterations() -> u32 {
    25
}
fn default_ipc_protocol() -> String {
    "typed".to_string()
}
fn default_schedules_enabled() -> bool {
    true
}

fn default_model_map() -> HashMap<String, String> {
    let mut map = HashMap::new();
    // Keep these in sync with `config.yaml`'s `model:` block so the config-absent
    // fallback matches the shipped file (see tests/test_config_parity.py).
    map.insert("claude".to_string(), "claude-opus-4-6".to_string());
    map.insert("openai".to_string(), "gpt-4o".to_string());
    map.insert("deepseek".to_string(), "deepseek-chat".to_string());
    map.insert("gemini".to_string(), "gemini-2.0-flash".to_string());
    map.insert("ollama".to_string(), "gpt-oss:120b-cloud".to_string());
    map
}

#[derive(Debug, Deserialize, Clone)]
pub struct AppConfig {
    #[serde(default = "default_model_map", rename = "model")]
    pub default_model: HashMap<String, String>,

    #[serde(default = "default_bridge_timeout_secs")]
    pub bridge_timeout_secs: u64,

    #[serde(default = "default_screenshot_flash_duration_ms")]
    pub screenshot_flash_duration_ms: u64,

    #[serde(default = "default_max_iterations")]
    pub max_iterations: u32,

    #[serde(default = "default_ipc_protocol")]
    pub ipc_protocol: String,

    /// D6: master switch for the in-app scheduler tick loop.
    #[serde(default = "default_schedules_enabled")]
    pub schedules_enabled: bool,
}

impl Default for AppConfig {
    fn default() -> Self {
        Self {
            default_model: default_model_map(),
            bridge_timeout_secs: default_bridge_timeout_secs(),
            screenshot_flash_duration_ms: default_screenshot_flash_duration_ms(),
            max_iterations: default_max_iterations(),
            ipc_protocol: default_ipc_protocol(),
            schedules_enabled: default_schedules_enabled(),
        }
    }
}

pub fn load_config(path: &Path) -> AppConfig {
    if !path.exists() {
        return AppConfig::default();
    }
    match fs::read_to_string(path) {
        Ok(content) => match serde_yaml::from_str::<AppConfig>(&content) {
            Ok(config) => config,
            Err(e) => {
                eprintln!(
                    "[Kim] Warning: Failed to parse config.yaml: {}. Using defaults.",
                    e
                );
                AppConfig::default()
            }
        },
        Err(e) => {
            eprintln!(
                "[Kim] Warning: Failed to read config.yaml: {}. Using defaults.",
                e
            );
            AppConfig::default()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_config_load_from_yaml() {
        let yaml_str = r#"
model:
  openai: gpt-4o-custom
  deepseek: deepseek-chat-custom
  gemini: gemini-2.0-flash-custom
  ollama: gpt-oss-custom
max_iterations: 30
bridge_timeout_secs: 500
screenshot_flash_duration_ms: 2000
ipc_protocol: typed
"#;
        let config: AppConfig = serde_yaml::from_str(yaml_str).unwrap();
        assert_eq!(config.default_model.get("openai").unwrap(), "gpt-4o-custom");
        assert_eq!(
            config.default_model.get("deepseek").unwrap(),
            "deepseek-chat-custom"
        );
        assert_eq!(
            config.default_model.get("gemini").unwrap(),
            "gemini-2.0-flash-custom"
        );
        assert_eq!(
            config.default_model.get("ollama").unwrap(),
            "gpt-oss-custom"
        );
        assert_eq!(config.max_iterations, 30);
        assert_eq!(config.bridge_timeout_secs, 500);
        assert_eq!(config.screenshot_flash_duration_ms, 2000);
        assert_eq!(config.ipc_protocol, "typed");
    }

    #[test]
    fn test_config_defaults() {
        let yaml_str = "{}";
        let config: AppConfig = serde_yaml::from_str(yaml_str).unwrap();
        assert_eq!(config.default_model.get("openai").unwrap(), "gpt-4o");
        assert_eq!(config.max_iterations, 25);
        assert_eq!(config.bridge_timeout_secs, 720);
        assert_eq!(config.screenshot_flash_duration_ms, 3300);
        assert_eq!(config.ipc_protocol, "typed");
    }

    #[test]
    fn test_load_config_from_file() {
        let tmp = tempfile::NamedTempFile::new().expect("failed to create temp file");
        let yaml_content = r#"
max_iterations: 50
bridge_timeout_secs: 300
ipc_protocol: typed
"#;
        std::fs::write(tmp.path(), yaml_content).unwrap();
        let config = load_config(tmp.path());
        assert_eq!(config.max_iterations, 50);
        assert_eq!(config.bridge_timeout_secs, 300);
        assert_eq!(config.ipc_protocol, "typed");
        // Unset fields should use defaults
        assert_eq!(config.screenshot_flash_duration_ms, 3300);
    }

    #[test]
    fn test_load_config_missing_file_returns_defaults() {
        let config = load_config(Path::new("/nonexistent/path/config.yaml"));
        assert_eq!(config.max_iterations, 25);
        assert_eq!(config.bridge_timeout_secs, 720);
        assert_eq!(config.ipc_protocol, "typed");
    }

    #[test]
    fn test_load_config_invalid_yaml_returns_defaults() {
        let tmp = tempfile::NamedTempFile::new().expect("failed to create temp file");
        std::fs::write(tmp.path(), "{{{{ this is not valid yaml !@#$").unwrap();
        let config = load_config(tmp.path());
        assert_eq!(config.max_iterations, 25);
        assert_eq!(config.bridge_timeout_secs, 720);
    }
}

use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

const CONFIG_DIR_NAME: &str = ".kim";
const CONFIG_FILE_NAME: &str = "cli-config.json";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ThemeName {
    DarkNeovim,
    QuietLight,
}

impl ThemeName {
    pub fn from_input(input: &str) -> Option<Self> {
        match input.trim().to_ascii_lowercase().as_str() {
            "dark" | "dark-neovim" | "neovim" => Some(Self::DarkNeovim),
            "light" | "quiet" | "quiet-light" | "novel" | "soft" => Some(Self::QuietLight),
            _ => None,
        }
    }

    pub const fn label(self) -> &'static str {
        match self {
            Self::DarkNeovim => "dark-neovim",
            Self::QuietLight => "quiet-light",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct KimConfig {
    pub provider: String,
    pub model: String,
    pub theme: ThemeName,
    pub ollama_base_url: String,
    pub desktop_bridge_url: String,
    pub api_keys: BTreeMap<String, String>,
}

impl Default for KimConfig {
    fn default() -> Self {
        Self {
            provider: "ollama".to_string(),
            model: "llama3.2".to_string(),
            theme: ThemeName::DarkNeovim,
            ollama_base_url: "http://127.0.0.1:11434".to_string(),
            desktop_bridge_url: "http://127.0.0.1:18991".to_string(),
            api_keys: BTreeMap::new(),
        }
    }
}

impl KimConfig {
    pub fn load() -> Self {
        let Some(path) = config_path() else {
            return Self::default();
        };
        let Ok(raw) = fs::read_to_string(path) else {
            return Self::default();
        };
        serde_json::from_str(&raw).unwrap_or_default()
    }

    pub fn save(&self) -> io::Result<()> {
        let Some(path) = config_path() else {
            return Ok(());
        };
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        let raw = serde_json::to_string_pretty(self)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        fs::write(path, raw)
    }
}

pub fn config_path() -> Option<PathBuf> {
    dirs::home_dir().map(|home| home.join(CONFIG_DIR_NAME).join(CONFIG_FILE_NAME))
}

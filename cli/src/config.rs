use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

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
        Self::load_from(&path)
    }

    pub fn save(&self) -> io::Result<()> {
        let Some(path) = config_path() else {
            return Ok(());
        };
        self.save_to(&path)
    }

    fn load_from(path: &Path) -> Self {
        let Ok(raw) = fs::read_to_string(path) else {
            return Self::default();
        };
        serde_json::from_str(&raw).unwrap_or_default()
    }

    fn save_to(&self, path: &Path) -> io::Result<()> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        let raw = serde_json::to_string_pretty(self)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        atomic_write(path, &raw)
    }
}

// Writes `content` to `path` atomically: serialise to a sibling temp file,
// sync to disk, then rename over the final path. On Unix, rename(2) replaces
// the destination atomically when src and dst share a filesystem. On platforms
// where replacing an existing file is not supported, commit fails and the prior
// config remains intact.
fn atomic_write(path: &Path, content: &str) -> io::Result<()> {
    let dir = path.parent().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "config path has no parent directory",
        )
    })?;
    let pid = std::process::id();
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_nanos());
    let tmp_path = dir.join(format!(".kim-config.{pid}.{nanos}.tmp"));

    {
        use std::io::Write as _;
        let mut options = fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options.open(&tmp_path).map_err(|e| {
            io::Error::new(
                e.kind(),
                format!("could not create temp config {}: {e}", tmp_path.display()),
            )
        })?;
        file.write_all(content.as_bytes()).map_err(|e| {
            let _ = fs::remove_file(&tmp_path);
            e
        })?;
        file.sync_all().map_err(|e| {
            let _ = fs::remove_file(&tmp_path);
            e
        })?;
    } // file closed before rename

    fs::rename(&tmp_path, path).map_err(|e| {
        let _ = fs::remove_file(&tmp_path);
        io::Error::new(
            e.kind(),
            format!("could not commit config to {}: {e}", path.display()),
        )
    })
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

    use super::{KimConfig, ThemeName};

    fn unique_config_path() -> std::path::PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("kim-cli-config-test-{nanos}"));
        fs::create_dir_all(&dir).expect("test dir");
        dir.join("cli-config.json")
    }

    #[test]
    fn config_roundtrip_save_and_load() {
        let path = unique_config_path();
        let mut api_keys = BTreeMap::new();
        api_keys.insert("claude".to_string(), "sk-test-key".to_string());
        let original = KimConfig {
            provider: "claude".to_string(),
            model: "claude-opus-4-7".to_string(),
            theme: ThemeName::QuietLight,
            api_keys,
            ..KimConfig::default()
        };
        original.save_to(&path).expect("save should succeed");
        let loaded = KimConfig::load_from(&path);
        let _ = fs::remove_dir_all(path.parent().unwrap());
        assert_eq!(loaded, original);
    }

    #[test]
    fn config_overwrite_smaller_no_stale_tail() {
        let path = unique_config_path();
        // Write a config with many API keys so the file is large.
        let large = KimConfig {
            api_keys: (0..10)
                .map(|i| (format!("provider-{i}"), format!("key-{i:0>64}")))
                .collect(),
            ..KimConfig::default()
        };
        large.save_to(&path).expect("initial save");

        // Overwrite with the minimal default (smaller content).
        // Atomic rename means the file is replaced entirely, leaving no stale
        // tail from the previous larger write.
        let small = KimConfig::default();
        small.save_to(&path).expect("overwrite save");

        let loaded = KimConfig::load_from(&path);
        let _ = fs::remove_dir_all(path.parent().unwrap());
        assert_eq!(
            loaded, small,
            "overwrite must replace file completely — no stale keys from the larger predecessor"
        );
    }

    #[test]
    fn config_save_leaves_no_tmp_files() {
        let path = unique_config_path();
        KimConfig::default()
            .save_to(&path)
            .expect("save should succeed");
        let dir = path.parent().unwrap();
        let tmp_count = fs::read_dir(dir)
            .expect("read dir")
            .flatten()
            .filter(|e| {
                e.path()
                    .extension()
                    .and_then(|x| x.to_str())
                    .map_or(false, |x| x == "tmp")
            })
            .count();
        let _ = fs::remove_dir_all(dir);
        assert_eq!(
            tmp_count, 0,
            "no .tmp files should remain after a clean save"
        );
    }

    #[cfg(unix)]
    #[test]
    fn config_save_uses_private_file_permissions() {
        use std::os::unix::fs::PermissionsExt;

        let path = unique_config_path();
        KimConfig::default()
            .save_to(&path)
            .expect("save should succeed");
        let mode = fs::metadata(&path).expect("metadata").permissions().mode() & 0o777;
        let _ = fs::remove_dir_all(path.parent().unwrap());
        assert_eq!(mode, 0o600, "config stores API keys and must be private");
    }

    #[test]
    fn default_config_round_trips_without_api_keys() {
        let path = unique_config_path();
        let original = KimConfig::default();
        original.save_to(&path).expect("save");
        let loaded = KimConfig::load_from(&path);
        let _ = fs::remove_dir_all(path.parent().unwrap());
        assert_eq!(loaded, original);
        assert!(loaded.api_keys.is_empty());
    }

    #[test]
    fn load_from_missing_file_returns_default() {
        let path =
            std::env::temp_dir().join(format!("kim-no-such-config-{}.json", std::process::id()));
        let loaded = KimConfig::load_from(&path);
        assert_eq!(loaded, KimConfig::default());
    }

    #[test]
    fn load_from_corrupt_file_returns_default() {
        let path = unique_config_path();
        fs::write(&path, b"not valid json {{{{").expect("write fixture");
        let loaded = KimConfig::load_from(&path);
        let _ = fs::remove_dir_all(path.parent().unwrap());
        assert_eq!(loaded, KimConfig::default());
    }
}

pub fn config_path() -> Option<PathBuf> {
    #[cfg(test)]
    {
        Some(
            std::env::temp_dir()
                .join(format!("kim-cli-test-{}", std::process::id()))
                .join(CONFIG_DIR_NAME)
                .join(CONFIG_FILE_NAME),
        )
    }

    #[cfg(not(test))]
    {
        dirs::home_dir().map(|home| home.join(CONFIG_DIR_NAME).join(CONFIG_FILE_NAME))
    }
}

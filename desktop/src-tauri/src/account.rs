//! Kim account — `~/.config/kim/account.json` persistence.
//!
//! Extracted from lib.rs (Phase 8 restructure).
//! Public Tauri commands: `load_account`, `save_account`, `clear_account`,
//! `reset_onboarding`, `delete_all_sessions`.

use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex as StdMutex;

/// Guards `save_account` against concurrent full-overwrites.
///
/// The atomic tmp+rename below makes every individual write crash-safe
/// regardless of lock ordering.
static ACCOUNT_SAVE_LOCK: StdMutex<()> = StdMutex::new(());

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct GoogleAccountEntry {
    pub email: String,
    pub authuser_index: u32,
}

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct GoogleApiAccount {
    #[serde(default)]
    pub email: String,
    #[serde(default)]
    pub connected: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub expires_at: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub project_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub needs_reauth: Option<bool>,
}

#[derive(Deserialize)]
#[serde(untagged)]
enum GoogleAccountEntryCompat {
    Entry(GoogleAccountEntry),
    Email(String),
}

fn deserialize_google_accounts<'de, D>(deserializer: D) -> Result<Vec<GoogleAccountEntry>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let raw = Vec::<GoogleAccountEntryCompat>::deserialize(deserializer)?;
    let mut seen = std::collections::HashSet::new();
    let mut accounts = Vec::new();
    for (idx, item) in raw.into_iter().enumerate() {
        let entry = match item {
            GoogleAccountEntryCompat::Entry(entry) => {
                if entry.email.trim().is_empty() {
                    None
                } else {
                    Some(GoogleAccountEntry {
                        email: entry.email.trim().to_lowercase(),
                        authuser_index: entry.authuser_index,
                    })
                }
            }
            GoogleAccountEntryCompat::Email(email) => {
                let email = email.trim().to_lowercase();
                if email.is_empty() {
                    None
                } else {
                    Some(GoogleAccountEntry {
                        email,
                        authuser_index: idx as u32,
                    })
                }
            }
        };
        if let Some(entry) = entry {
            if seen.insert(entry.email.clone()) {
                accounts.push(entry);
            }
        }
    }
    Ok(accounts)
}

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct KimAccount {
    pub display_name: String,
    pub github_username: Option<String>,
    /// Personal Access Token. In-memory only: persisted to the OS keychain via
    /// `secrets.rs`, stripped by `save_account` so it is never written to disk.
    pub github_token: Option<String>,
    pub github_avatar_url: Option<String>,
    /// ID of the private backup Gist so restore can find it later.
    pub gist_id: Option<String>,
    pub created_at: String,
    /// Explicit project root paths the user has added to the Code tab.
    #[serde(default)]
    pub code_projects: Vec<String>,
    /// Google accounts with their authuser indices for Gemini browser sign-in.
    #[serde(default, deserialize_with = "deserialize_google_accounts")]
    pub google_accounts: Vec<GoogleAccountEntry>,
    /// Active Google account email for Gemini browser sessions.
    pub google_active_account: Option<String>,
    /// Official Gemini API OAuth connection metadata.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub google_api_account: Option<GoogleApiAccount>,
}

pub(crate) fn account_dir() -> PathBuf {
    dirs::config_dir()
        .or_else(dirs::home_dir)
        .unwrap_or_else(|| PathBuf::from("."))
        .join("kim")
}

pub(crate) fn account_path() -> PathBuf {
    account_dir().join("account.json")
}

/// Write `account` atomically: serialise to a sibling `.tmp` file, then rename.
/// The rename is atomic on POSIX (same filesystem); on Windows it is best-effort
/// via `fs::rename` (atomic enough given the mutex guard).
fn write_account_atomic_inner(acct_path: &Path, account: &KimAccount) -> Result<(), String> {
    let json = serde_json::to_string_pretty(account).map_err(|e| e.to_string())?;
    let tmp = acct_path.with_extension("json.tmp");
    fs::write(&tmp, &json).map_err(|e| e.to_string())?;
    fs::rename(&tmp, acct_path).map_err(|e| {
        let _ = fs::remove_file(&tmp);
        e.to_string()
    })
}

#[tauri::command]
pub async fn load_account() -> Result<Option<KimAccount>, String> {
    let path = account_path();
    if !path.exists() {
        return Ok(None);
    }
    let raw = fs::read_to_string(&path).map_err(|e| e.to_string())?;
    let mut account: KimAccount = serde_json::from_str(&raw).map_err(|e| e.to_string())?;
    // Hydrate the PAT from the OS keychain so callers get a fully-populated
    // account without needing a separate keychain round-trip.
    match account.github_token.as_deref() {
        // Legacy account.json (pre-keychain): a real token still lives on disk.
        // Migrate it into the keychain now so the very next save_account (which
        // strips the field) doesn't orphan it and break gist backup.
        Some(tok) if !tok.trim().is_empty() => {
            crate::secrets::save_token_to_keychain(tok);
        }
        // Normal path: token isn't on disk — pull it from the keychain.
        _ => {
            account.github_token = crate::secrets::load_token_from_keychain();
        }
    }
    Ok(Some(account))
}

#[tauri::command]
pub async fn save_account(account: KimAccount) -> Result<(), String> {
    let dir = account_dir();
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let path = account_path();

    // Strip the PAT before persisting — it lives in the OS keychain, never on disk.
    let mut to_write = account;
    to_write.github_token = None;

    let _guard = ACCOUNT_SAVE_LOCK.lock().map_err(|e| e.to_string())?;
    write_account_atomic_inner(&path, &to_write)
}

#[tauri::command]
pub async fn clear_account() -> Result<(), String> {
    let path = account_path();
    if path.exists() {
        fs::remove_file(&path).map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
pub async fn reset_onboarding() -> Result<(), String> {
    clear_account().await
}

#[tauri::command]
pub async fn delete_all_sessions() -> Result<(), String> {
    let base = crate::default_sessions_dir();
    fs::create_dir_all(&base).map_err(|e| e.to_string())?;
    for entry in fs::read_dir(&base).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().to_string();
        if name.starts_with('.') {
            continue;
        }
        if path.is_dir() {
            fs::remove_dir_all(path).map_err(|e| e.to_string())?;
        } else if path
            .extension()
            .and_then(|e| e.to_str())
            .is_some_and(|ext| matches!(ext, "jsonl" | "json" | "md" | "zip"))
        {
            fs::remove_file(path).map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod account_tests {
    use super::*;
    use std::fs;

    fn tmp_dir() -> PathBuf {
        // Uniqueness must not rely on the clock alone: parallel test threads can
        // call this within the same nanosecond and collide, then cross-delete
        // each other's dirs (one test's remove_dir_all wipes another's file
        // mid-write). Combine the timestamp with a per-process atomic counter.
        use std::sync::atomic::{AtomicU64, Ordering};
        static COUNTER: AtomicU64 = AtomicU64::new(0);
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let n = COUNTER.fetch_add(1, Ordering::Relaxed);
        let p = std::env::temp_dir().join(format!("kim-acct-{nanos}-{n}"));
        fs::create_dir_all(&p).unwrap();
        p
    }

    fn minimal_account() -> KimAccount {
        KimAccount {
            display_name: "Test User".to_string(),
            github_username: Some("octocat".to_string()),
            github_token: None,
            github_avatar_url: None,
            gist_id: None,
            created_at: "2024-01-01T00:00:00Z".to_string(),
            code_projects: vec![],
            google_accounts: vec![],
            google_active_account: None,
            google_api_account: None,
        }
    }

    /// Regression: write_account_atomic_inner produces valid pretty JSON that
    /// round-trips back to an equal KimAccount.
    #[test]
    fn write_account_atomic_inner_roundtrip() {
        let dir = tmp_dir();
        let path = dir.join("account.json");
        let original = minimal_account();

        write_account_atomic_inner(&path, &original).expect("write must succeed");

        let raw = fs::read_to_string(&path).expect("file must exist after write");
        // Verify it is valid pretty JSON (indented).
        assert!(
            raw.contains('\n'),
            "expected pretty-printed JSON with newlines"
        );

        let restored: KimAccount =
            serde_json::from_str(&raw).expect("must deserialize without error");

        assert_eq!(restored.display_name, original.display_name);
        assert_eq!(restored.github_username, original.github_username);
        assert_eq!(restored.created_at, original.created_at);
        assert_eq!(restored.code_projects, original.code_projects);

        let _ = fs::remove_dir_all(&dir);
    }

    /// Regression: no `.json.tmp` sibling is left on disk after a successful write.
    #[test]
    fn no_tmp_leftover() {
        let dir = tmp_dir();
        let path = dir.join("account.json");
        let account = minimal_account();

        write_account_atomic_inner(&path, &account).expect("write must succeed");

        let tmp = path.with_extension("json.tmp");
        assert!(
            !tmp.exists(),
            "sibling .json.tmp must be gone after successful atomic rename"
        );

        let _ = fs::remove_dir_all(&dir);
    }

    /// Regression: an account whose github_token is None serialises to JSON with
    /// the field absent or null — never an actual PAT string.  This locks in the
    /// strip-before-persist contract: no real token may reach disk.
    #[test]
    fn github_token_serialization_shape() {
        let dir = tmp_dir();
        let path = dir.join("account.json");
        let mut account = minimal_account();
        account.github_token = None;

        write_account_atomic_inner(&path, &account).expect("write must succeed");

        let raw = fs::read_to_string(&path).expect("file must exist");

        // The field must never carry an actual string value on disk.
        // serde serialises Option::None without skip_serializing_if as `null`;
        // either null or absent is acceptable — a quoted string is not.
        assert!(
            !raw.contains("\"github_token\": \""),
            "github_token must not be a string value in persisted JSON; got: {raw}"
        );

        // Cross-check via round-trip deserialization.
        let restored: KimAccount =
            serde_json::from_str(&raw).expect("must deserialize without error");
        assert!(
            restored.github_token.is_none(),
            "github_token must deserialize back to None"
        );

        let _ = fs::remove_dir_all(&dir);
    }
}

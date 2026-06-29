//! OS-keychain storage for the GitHub Personal Access Token.
//!
//! Uses the `keyring` crate which delegates to:
//!   macOS  — Keychain Services
//!   Windows — Credential Manager
//!   Linux  — Secret Service (libsecret) / kernel keystore
//!
//! Public Tauri commands: `store_github_token`, `get_github_token`,
//! `delete_github_token`.
//! Internal helper: `load_token_from_keychain` (called by `account::load_account`
//! to hydrate the in-memory struct without exposing the keychain to the frontend
//! via an extra round-trip).

use keyring::{Entry, Error as KeyringError};

const SERVICE: &str = "kim-desktop";
const GITHUB_PAT_ACCOUNT: &str = "github-pat";

fn entry() -> Result<Entry, String> {
    Entry::new(SERVICE, GITHUB_PAT_ACCOUNT).map_err(|e| e.to_string())
}

/// Store (or overwrite) the GitHub PAT in the OS keychain.
#[tauri::command]
pub async fn store_github_token(token: String) -> Result<(), String> {
    entry()?.set_password(&token).map_err(|e| e.to_string())
}

/// Retrieve the GitHub PAT from the OS keychain.
/// Returns `None` when no entry has been stored yet.
#[tauri::command]
pub async fn get_github_token() -> Result<Option<String>, String> {
    match entry()?.get_password() {
        Ok(t) => Ok(Some(t)),
        Err(KeyringError::NoEntry) => Ok(None),
        Err(e) => Err(e.to_string()),
    }
}

/// Delete the GitHub PAT from the OS keychain (idempotent: no error if absent).
#[tauri::command]
pub async fn delete_github_token() -> Result<(), String> {
    match entry()?.delete_password() {
        Ok(()) => Ok(()),
        Err(KeyringError::NoEntry) => Ok(()),
        Err(e) => Err(e.to_string()),
    }
}

/// Non-command helper used by `account::load_account` to hydrate the in-memory
/// `KimAccount.github_token` field without triggering a frontend invoke round-trip.
/// Best-effort: returns `None` on any error (including missing entry or keychain
/// permission denial).
pub fn load_token_from_keychain() -> Option<String> {
    entry().ok()?.get_password().ok()
}

/// Non-command helper used by `account::load_account` to migrate a legacy PAT that
/// still lives in account.json into the OS keychain on first load after upgrade.
/// Best-effort: silently ignores errors (the in-memory token still works for the
/// current session even if the keychain write fails).
pub fn save_token_to_keychain(token: &str) {
    if let Ok(e) = entry() {
        let _ = e.set_password(token);
    }
}

//! Run history persistence and platform/update commands.
//!
//! Extracted from lib.rs (Phase 8 restructure).
//! Public Tauri commands: `save_run_history`, `load_run_history`,
//! `get_platform_info`, `run_update`.

use std::fs;
use std::path::PathBuf;
use std::time::Duration;
use tauri::Emitter;

fn find_session_date_dir(
    session_id: &str,
    session_date: Option<&str>,
    kim_dir: Option<&str>,
    codex_dir: Option<&str>,
) -> Option<PathBuf> {
    let mut dirs: Vec<PathBuf> = vec![kim_dir
        .map(PathBuf::from)
        .unwrap_or_else(crate::default_sessions_dir)];
    if let Some(p) = codex_dir {
        dirs.push(PathBuf::from(p));
    }
    for base in &dirs {
        if !base.exists() {
            continue;
        }
        if let Some(date) = session_date {
            let dd = base.join(date);
            if dd.join(format!("{}.jsonl", session_id)).exists() {
                return Some(dd);
            }
        }
        if let Ok(entries) = fs::read_dir(base) {
            for entry in entries.filter_map(|e| e.ok()) {
                let dd = entry.path();
                if dd.is_dir() && dd.join(format!("{}.jsonl", session_id)).exists() {
                    return Some(dd);
                }
            }
        }
    }
    None
}

#[tauri::command]
pub async fn save_run_history(
    session_id: String,
    session_date: Option<String>,
    kim_dir: Option<String>,
    codex_dir: Option<String>,
    runs: serde_json::Value,
) -> Result<(), String> {
    crate::validate_session_id(&session_id)?;
    if let Some(d) = session_date.as_deref() {
        crate::validate_session_id(d)?;
    }

    let dir = find_session_date_dir(
        &session_id,
        session_date.as_deref(),
        kim_dir.as_deref(),
        codex_dir.as_deref(),
    )
    .or_else(|| {
        let base = kim_dir
            .as_deref()
            .map(PathBuf::from)
            .unwrap_or_else(crate::default_sessions_dir);
        let today = crate::data_io::chrono_now()
            .get(0..10)
            .map(|s| s.to_string())
            .unwrap_or_default();
        let dd = base.join(today);
        fs::create_dir_all(&dd).ok()?;
        Some(dd)
    })
    .ok_or_else(|| "Could not locate a session directory to save runs.json into".to_string())?;

    let path = dir.join(format!("{}.runs.json", session_id));
    let tmp_path = dir.join(format!("{}.runs.json.tmp", session_id));
    let body = serde_json::to_string(&runs).map_err(|e| e.to_string())?;

    // Atomic write: write to a same-directory temp file, then rename over the
    // target.  A crash mid-write leaves the previous .runs.json intact rather
    // than producing a truncated/empty file.
    fs::write(&tmp_path, body).map_err(|e| e.to_string())?;
    match fs::rename(&tmp_path, &path) {
        Ok(()) => Ok(()),
        Err(e) => {
            #[cfg(target_os = "windows")]
            {
                let _ = &e;
                // Windows rename does not overwrite existing files. Fall back to
                // remove+rename; this is not perfectly atomic, but still avoids
                // exposing a partially-written JSON file.
                if path.exists() {
                    fs::remove_file(&path).map_err(|remove_err| remove_err.to_string())?;
                }
                fs::rename(&tmp_path, &path).map_err(|rename_err| rename_err.to_string())
            }
            #[cfg(not(target_os = "windows"))]
            {
                let _ = fs::remove_file(&tmp_path);
                Err(e.to_string())
            }
        }
    }
}

#[tauri::command]
pub async fn load_run_history(
    session_id: String,
    session_date: Option<String>,
    kim_dir: Option<String>,
    codex_dir: Option<String>,
) -> Result<serde_json::Value, String> {
    crate::validate_session_id(&session_id)?;
    if let Some(d) = session_date.as_deref() {
        crate::validate_session_id(d)?;
    }

    let dir = match find_session_date_dir(
        &session_id,
        session_date.as_deref(),
        kim_dir.as_deref(),
        codex_dir.as_deref(),
    ) {
        Some(d) => d,
        None => return Ok(serde_json::json!([])),
    };
    let path = dir.join(format!("{}.runs.json", session_id));
    if !path.exists() {
        return Ok(serde_json::json!([]));
    }
    let body = fs::read_to_string(&path).map_err(|e| e.to_string())?;
    serde_json::from_str(&body).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn get_platform_info() -> String {
    let os = std::env::consts::OS;
    let arch = std::env::consts::ARCH;
    match (os, arch) {
        ("macos", "aarch64") => "macOS (Apple Silicon)".into(),
        ("macos", _) => "macOS (Intel)".into(),
        ("windows", "x86_64") => "Windows x64".into(),
        ("windows", "x86") => "Windows x86".into(),
        ("linux", "aarch64") => "Linux ARM64".into(),
        ("linux", _) => "Linux x64".into(),
        (os, arch) => format!("{os} ({arch})"),
    }
}

/// Expected git remote host for update integrity checks.
/// Updates are rejected if the configured remote URL does not match this host,
/// guarding against a tampered local git config pointing at a rogue mirror.
const EXPECTED_REMOTE_HOST: &str = "github.com";

/// AUDIT FIX #2: extract the actual host component from a git remote URL,
/// rather than relying on a substring match (`remote_url.contains("github.com")`),
/// which a lookalike host like `https://github.com.evil.example/...` or
/// `https://evil.example/x?host=github.com` would also satisfy.
///
/// Handles the two URL shapes `git remote get-url` can return:
///   - Standard URL form: `https://github.com/owner/repo.git`,
///     `ssh://git@github.com/owner/repo.git`, `git://github.com/owner/repo.git`
///   - SCP-like SSH shorthand (not a URI `url` can parse): `git@github.com:owner/repo.git`
///
/// Returns `None` if no host could be extracted (malformed remote).
fn extract_remote_host(remote_url: &str) -> Option<String> {
    let trimmed = remote_url.trim();
    if trimmed.is_empty() {
        return None;
    }

    // SCP-like syntax has no "://" scheme separator: `[user@]host:path`.
    // Guard against Windows-style absolute paths (`C:\...`) being misread as
    // scp syntax by requiring the part before ':' to contain no path
    // separators and, if it has an '@', to look like user@host.
    if !trimmed.contains("://") {
        if let Some(colon_idx) = trimmed.find(':') {
            let before_colon = &trimmed[..colon_idx];
            if !before_colon.is_empty()
                && !before_colon.contains('/')
                && !before_colon.contains('\\')
            {
                let host_part = before_colon.rsplit('@').next().unwrap_or(before_colon);
                if !host_part.is_empty() {
                    return Some(host_part.to_ascii_lowercase());
                }
            }
        }
        return None;
    }

    // Standard URL form: parse properly and take the host component only
    // (never the path/query, which is what made the old substring check
    // spoofable).
    url::Url::parse(trimmed)
        .ok()
        .and_then(|u| u.host_str().map(|h| h.to_ascii_lowercase()))
}

/// True when `remote_url`'s host is exactly the expected host (or a direct
/// subdomain of it), never merely a substring match anywhere in the URL.
fn remote_host_is_expected(remote_url: &str) -> bool {
    match extract_remote_host(remote_url) {
        Some(host) => {
            host == EXPECTED_REMOTE_HOST
                || host.ends_with(&format!(".{EXPECTED_REMOTE_HOST}"))
        }
        None => false,
    }
}

#[tauri::command]
pub async fn run_update(app_handle: tauri::AppHandle) -> Result<(), String> {
    let kim_root = crate::default_project_root();

    let git_cmd = if cfg!(target_os = "windows") {
        "git.exe"
    } else {
        "git"
    };

    // --- Supply-chain integrity: verify remote URL before pulling -----------------
    // Reject the update if the configured 'origin' remote does not point at the
    // expected host.  This prevents a tampered local git config (or a poisoned
    // DNS entry) from silently substituting a rogue upstream.
    let remote_out = std::process::Command::new(git_cmd)
        .args(["remote", "get-url", "origin"])
        .current_dir(&kim_root)
        .output()
        .map_err(|e| format!("git not found — make sure Git is installed: {e}"))?;

    let remote_url = String::from_utf8_lossy(&remote_out.stdout)
        .trim()
        .to_string();
    if !remote_out.status.success() {
        // AUDIT FIX #2: `git remote get-url origin` failing means the remote
        // cannot be verified at all (detached HEAD, no remote configured,
        // corrupted repo, ...). Previously this warned and proceeded straight
        // to `git pull` anyway -- an update against an *unverified* remote is
        // exactly the supply-chain risk this check exists to prevent. Abort
        // instead.
        let stderr = String::from_utf8_lossy(&remote_out.stderr).trim().to_string();
        return Err(format!(
            "Update aborted: could not read the 'origin' remote URL ({}). \
             Configure a git remote pointing at {EXPECTED_REMOTE_HOST} before updating.",
            if stderr.is_empty() { "no output".to_string() } else { stderr }
        ));
    }
    // AUDIT FIX #2: compare the parsed host component exactly, not a
    // substring match. `remote_url.contains("github.com")` previously
    // accepted lookalike hosts such as `https://github.com.evil.example/...`
    // or any URL that merely embedded the string "github.com" in its path
    // or query. `remote_host_is_expected` parses both the standard URL form
    // (https://github.com/..., ssh://git@github.com/...) and the SCP-like
    // SSH shorthand (git@github.com:owner/repo.git) and checks the actual
    // host component.
    if !remote_host_is_expected(&remote_url) {
        return Err(format!(
            "Update aborted: remote origin '{remote_url}' does not match expected host '{EXPECTED_REMOTE_HOST}'. \
             Verify your git remote configuration before updating."
        ));
    }

    let _ = app_handle.emit("kim-update-progress", "Pulling latest source from GitHub…");

    let git_out = std::process::Command::new(git_cmd)
        .args(["pull", "--ff-only"])
        .current_dir(&kim_root)
        .output()
        .map_err(|e| format!("git pull failed to spawn: {e}"))?;

    if !git_out.status.success() {
        let stderr = String::from_utf8_lossy(&git_out.stderr);
        return Err(format!("git pull failed: {stderr}"));
    }
    let git_stdout = String::from_utf8_lossy(&git_out.stdout).trim().to_string();
    let already_latest =
        git_stdout.contains("Already up to date") || git_stdout.contains("Already up-to-date");
    let _ = app_handle.emit(
        "kim-update-progress",
        if git_stdout.is_empty() {
            "Source updated.".to_string()
        } else {
            git_stdout.clone()
        },
    );

    if already_latest {
        let _ = app_handle.emit(
            "kim-update-progress",
            "Source is already up to date — no restart needed.",
        );
        return Ok(());
    }

    // --- Supply-chain integrity: best-effort GPG commit signature verification ----
    // `git verify-commit` requires GPG and a trusted key in the local keyring.
    // We emit a warning rather than hard-failing because most self-hosted
    // deployments do not have a signing key imported.  Operators who want to
    // enforce signed commits should set `commit.gpgSign` and import the project
    // key; in that case `git pull --ff-only` itself will refuse unsigned commits.
    let verify_out = std::process::Command::new(git_cmd)
        .args(["verify-commit", "HEAD"])
        .current_dir(&kim_root)
        .output();

    match verify_out {
        Ok(out) if out.status.success() => {
            let _ = app_handle.emit("kim-update-progress", "Commit signature verified.");
        }
        Ok(out) => {
            let msg = String::from_utf8_lossy(&out.stderr).trim().to_string();
            let _ = app_handle.emit(
                "kim-update-progress",
                format!(
                    "Warning: commit signature could not be verified ({msg}). \
                         Import the project GPG key to enforce verification."
                ),
            );
        }
        Err(e) => {
            let _ = app_handle.emit(
                "kim-update-progress",
                format!("Warning: git verify-commit unavailable ({e}) — skipping signature check."),
            );
        }
    }

    let _ = app_handle.emit("kim-update-progress", "Updating Python dependencies…");

    let python =
        crate::find_python_interpreter(&kim_root).map_err(|e| format!("Python not found: {e}"))?;

    let pip_out = std::process::Command::new(&python)
        .args([
            "-m",
            "pip",
            "install",
            "-r",
            "requirements.txt",
            "-q",
            "--disable-pip-version-check",
        ])
        .current_dir(&kim_root)
        .output();

    match pip_out {
        Ok(out) if out.status.success() => {
            let _ = app_handle.emit("kim-update-progress", "Python dependencies updated.");
        }
        Ok(out) => {
            let msg = String::from_utf8_lossy(&out.stderr).trim().to_string();
            let _ = app_handle.emit("kim-update-progress", format!("Warning (pip): {msg}"));
        }
        Err(e) => {
            let _ = app_handle.emit(
                "kim-update-progress",
                format!("Warning: pip update skipped ({e})."),
            );
        }
    }

    let _ = app_handle.emit("kim-update-progress", "Update complete — restarting Kim…");
    tokio::time::sleep(Duration::from_millis(800)).await;

    #[cfg(target_os = "macos")]
    {
        // M-UPDATE-1: running `open -a Kim` while THIS instance is still alive
        // only ACTIVATES it (macOS never launches a second instance), so the
        // subsequent exit() left the app closed instead of restarted. Detach a
        // shell that waits for us to exit, then reopens the bundle.
        let app_target: String = std::env::current_exe()
            .ok()
            .and_then(|exe| {
                let mut path = exe.as_path();
                loop {
                    if path.extension().is_some_and(|e| e == "app") {
                        return Some(path.to_string_lossy().into_owned());
                    }
                    match path.parent() {
                        Some(p) => path = p,
                        None => return None,
                    }
                }
            })
            .unwrap_or_else(|| "Kim".to_string());
        // `open` takes either an .app path or (-a) an app name.
        let open_cmd = if app_target.ends_with(".app") {
            format!("sleep 1; open \"{}\"", app_target.replace('"', ""))
        } else {
            "sleep 1; open -a \"Kim\"".to_string()
        };
        let _ = std::process::Command::new("sh")
            .arg("-c")
            .arg(&open_cmd)
            .spawn();
        // Use app_handle.exit() instead of std::process::exit() so that Tauri's
        // cleanup handlers and Rust Drop implementations run before the process
        // terminates.  std::process::exit() skips all of this.
        // Note: app_handle.exit() returns () and does NOT diverge (it schedules
        // exit asynchronously), so we must explicitly return Ok(()) here so the
        // function's return type (Result<(), String>) is satisfied.
        app_handle.exit(0);
        Ok(())
    }
    #[cfg(not(target_os = "macos"))]
    {
        app_handle.restart();
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // -- extract_remote_host --

    #[test]
    fn test_extract_host_https() {
        assert_eq!(
            extract_remote_host("https://github.com/AdamMagued/kim.git"),
            Some("github.com".to_string())
        );
    }

    #[test]
    fn test_extract_host_https_no_dot_git_suffix() {
        assert_eq!(
            extract_remote_host("https://github.com/AdamMagued/kim"),
            Some("github.com".to_string())
        );
    }

    #[test]
    fn test_extract_host_ssh_url_form() {
        assert_eq!(
            extract_remote_host("ssh://git@github.com/AdamMagued/kim.git"),
            Some("github.com".to_string())
        );
    }

    #[test]
    fn test_extract_host_scp_like_ssh_shorthand() {
        assert_eq!(
            extract_remote_host("git@github.com:AdamMagued/kim.git"),
            Some("github.com".to_string())
        );
    }

    #[test]
    fn test_extract_host_is_case_insensitive() {
        assert_eq!(
            extract_remote_host("https://GitHub.COM/AdamMagued/kim.git"),
            Some("github.com".to_string())
        );
    }

    #[test]
    fn test_extract_host_empty_or_malformed() {
        assert_eq!(extract_remote_host(""), None);
        assert_eq!(extract_remote_host("   "), None);
        assert_eq!(extract_remote_host("not a url at all"), None);
    }

    // -- remote_host_is_expected: the security-relevant assertions --

    #[test]
    fn test_rejects_lookalike_subdomain_suffix_attack() {
        // github.com.evil.example -- old `.contains("github.com")` check
        // would have wrongly accepted this.
        assert!(!remote_host_is_expected(
            "https://github.com.evil.example/AdamMagued/kim.git"
        ));
    }

    #[test]
    fn test_rejects_github_com_embedded_in_path() {
        // "github.com" appears in the URL, but not as the host.
        assert!(!remote_host_is_expected(
            "https://evil.example/github.com/AdamMagued/kim.git"
        ));
    }

    #[test]
    fn test_rejects_github_com_embedded_in_query_string() {
        assert!(!remote_host_is_expected(
            "https://evil.example/repo.git?x=github.com"
        ));
    }

    #[test]
    fn test_rejects_userinfo_spoof() {
        // A URL with "github.com" stuffed into userinfo before the real
        // (malicious) host -- host_str() must still resolve to evil.example.
        assert!(!remote_host_is_expected(
            "https://github.com@evil.example/AdamMagued/kim.git"
        ));
    }

    #[test]
    fn test_accepts_https_github() {
        assert!(remote_host_is_expected(
            "https://github.com/AdamMagued/kim.git"
        ));
    }

    #[test]
    fn test_accepts_ssh_scp_like_github() {
        assert!(remote_host_is_expected("git@github.com:AdamMagued/kim.git"));
    }

    #[test]
    fn test_accepts_ssh_url_form_github() {
        assert!(remote_host_is_expected(
            "ssh://git@github.com/AdamMagued/kim.git"
        ));
    }

    #[test]
    fn test_rejects_unrelated_host() {
        assert!(!remote_host_is_expected("https://gitlab.com/foo/bar.git"));
    }

    #[test]
    fn test_rejects_empty_remote() {
        assert!(!remote_host_is_expected(""));
    }
}

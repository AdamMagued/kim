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
    let mut dirs: Vec<PathBuf> = vec![
        kim_dir.map(PathBuf::from).unwrap_or_else(crate::default_sessions_dir),
    ];
    if let Some(p) = codex_dir { dirs.push(PathBuf::from(p)); }
    for base in &dirs {
        if !base.exists() { continue; }
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
    if let Some(d) = session_date.as_deref() { crate::validate_session_id(d)?; }

    let dir = find_session_date_dir(&session_id, session_date.as_deref(), kim_dir.as_deref(), codex_dir.as_deref())
        .or_else(|| {
            let base = kim_dir.as_deref().map(PathBuf::from).unwrap_or_else(crate::default_sessions_dir);
            let today = crate::data_io::chrono_now().get(0..10).map(|s| s.to_string()).unwrap_or_default();
            let dd = base.join(today);
            fs::create_dir_all(&dd).ok()?;
            Some(dd)
        })
        .ok_or_else(|| "Could not locate a session directory to save runs.json into".to_string())?;

    let path = dir.join(format!("{}.runs.json", session_id));
    let body = serde_json::to_string(&runs).map_err(|e| e.to_string())?;
    fs::write(&path, body).map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
pub async fn load_run_history(
    session_id: String,
    session_date: Option<String>,
    kim_dir: Option<String>,
    codex_dir: Option<String>,
) -> Result<serde_json::Value, String> {
    crate::validate_session_id(&session_id)?;
    if let Some(d) = session_date.as_deref() { crate::validate_session_id(d)?; }

    let dir = match find_session_date_dir(&session_id, session_date.as_deref(), kim_dir.as_deref(), codex_dir.as_deref()) {
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
        ("macos",   "aarch64") => "macOS (Apple Silicon)".into(),
        ("macos",   _        ) => "macOS (Intel)".into(),
        ("windows", "x86_64" ) => "Windows x64".into(),
        ("windows", "x86"    ) => "Windows x86".into(),
        ("linux",   "aarch64") => "Linux ARM64".into(),
        ("linux",   _        ) => "Linux x64".into(),
        (os, arch)             => format!("{os} ({arch})"),
    }
}

#[tauri::command]
pub async fn run_update(app_handle: tauri::AppHandle) -> Result<(), String> {
    let kim_root = crate::default_project_root();

    let _ = app_handle.emit("kim-update-progress", "Pulling latest source from GitHub…");

    let git_cmd = if cfg!(target_os = "windows") { "git.exe" } else { "git" };
    let git_out = std::process::Command::new(git_cmd)
        .args(["pull", "--ff-only"])
        .current_dir(&kim_root)
        .output()
        .map_err(|e| format!("git not found — make sure Git is installed: {e}"))?;

    if !git_out.status.success() {
        let stderr = String::from_utf8_lossy(&git_out.stderr);
        return Err(format!("git pull failed: {stderr}"));
    }
    let git_stdout = String::from_utf8_lossy(&git_out.stdout).trim().to_string();
    let already_latest = git_stdout.contains("Already up to date") || git_stdout.contains("Already up-to-date");
    let _ = app_handle.emit(
        "kim-update-progress",
        if git_stdout.is_empty() { "Source updated.".to_string() } else { git_stdout.clone() },
    );

    if already_latest {
        let _ = app_handle.emit("kim-update-progress", "Source is already up to date — no restart needed.");
        return Ok(());
    }

    let _ = app_handle.emit("kim-update-progress", "Updating Python dependencies…");

    let python = crate::find_python_interpreter(&kim_root)
        .map_err(|e| format!("Python not found: {e}"))?;

    let pip_out = std::process::Command::new(&python)
        .args(["-m", "pip", "install", "-r", "requirements.txt", "-q", "--disable-pip-version-check"])
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
            let _ = app_handle.emit("kim-update-progress", format!("Warning: pip update skipped ({e})."));
        }
    }

    let _ = app_handle.emit("kim-update-progress", "Update complete — restarting Kim…");
    tokio::time::sleep(Duration::from_millis(800)).await;

    #[cfg(target_os = "macos")]
    {
        let reopened = std::process::Command::new("open")
            .args(["-a", "Kim"])
            .spawn()
            .is_ok();
        if !reopened {
            if let Ok(exe) = std::env::current_exe() {
                let mut path = exe.as_path();
                loop {
                    if path.extension().is_some_and(|e| e == "app") {
                        let _ = std::process::Command::new("open").arg(path).spawn();
                        break;
                    }
                    match path.parent() {
                        Some(p) => path = p,
                        None => break,
                    }
                }
            }
        }
        std::process::exit(0);
    }
    #[cfg(not(target_os = "macos"))]
    {
        app_handle.restart();
    }
}

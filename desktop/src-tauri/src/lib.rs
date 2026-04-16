use std::fs;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use serde::{Deserialize, Serialize};
use tauri::Emitter;

// ---------------------------------------------------------------------------
// Data types
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct SessionInfo {
    pub session_id: String,
    pub date: String,
    pub message_count: usize,
    pub has_summary: bool,
    pub summary: Option<String>,
    pub session_type: String, // "kim" or "claw"
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct KimMessage {
    pub role: String,
    pub content: serde_json::Value,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_calls: Option<Vec<serde_json::Value>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn default_sessions_dir() -> PathBuf {
    if let Some(home) = dirs::home_dir() {
        // Dev layout: ~/Desktop/kim/kim_sessions
        let dev_path = home.join("Desktop").join("kim").join("kim_sessions");
        if dev_path.exists() {
            return dev_path;
        }
        home.join(".kim").join("sessions")
    } else {
        PathBuf::from("kim_sessions")
    }
}

fn default_project_root() -> PathBuf {
    if let Some(home) = dirs::home_dir() {
        let dev_path = home.join("Desktop").join("kim");
        if dev_path.exists() {
            return dev_path;
        }
        home.join(".kim")
    } else {
        PathBuf::from(".")
    }
}

fn read_sessions_from_dir(base: &Path, session_type: &str) -> Result<Vec<SessionInfo>, String> {
    if !base.exists() {
        return Ok(vec![]);
    }

    let mut sessions = vec![];

    let mut date_dirs: Vec<_> = fs::read_dir(base)
        .map_err(|e| e.to_string())?
        .filter_map(|e| e.ok())
        .filter(|e| e.path().is_dir())
        .collect();
    date_dirs.sort_by(|a, b| b.file_name().cmp(&a.file_name()));

    for date_entry in date_dirs {
        let date_dir = date_entry.path();
        let date_str = date_entry.file_name().to_string_lossy().to_string();

        let mut jsonl_files: Vec<_> = fs::read_dir(&date_dir)
            .map_err(|e| e.to_string())?
            .filter_map(|e| e.ok())
            .filter(|e| {
                let name = e.file_name();
                let s = name.to_string_lossy();
                s.ends_with(".jsonl") && !s.contains(".summary")
            })
            .collect();
        jsonl_files.sort_by(|a, b| b.file_name().cmp(&a.file_name()));

        for file_entry in jsonl_files {
            let session_file = file_entry.path();
            let session_id = session_file
                .file_stem()
                .unwrap_or_default()
                .to_string_lossy()
                .to_string();

            let summary_file = date_dir.join(format!("{}.summary.txt", session_id));
            let has_summary = summary_file.exists();
            let summary = if has_summary {
                fs::read_to_string(&summary_file)
                    .ok()
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty())
            } else {
                None
            };

            let message_count = count_lines(&session_file).unwrap_or(0);

            sessions.push(SessionInfo {
                session_id,
                date: date_str.clone(),
                message_count,
                has_summary,
                summary,
                session_type: session_type.to_string(),
            });
        }
    }

    Ok(sessions)
}

fn count_lines(path: &Path) -> std::io::Result<usize> {
    let file = fs::File::open(path)?;
    let reader = BufReader::new(file);
    Ok(reader
        .lines()
        .filter(|l| {
            l.as_ref()
                .map(|s| !s.trim().is_empty())
                .unwrap_or(false)
        })
        .count())
}

fn parse_jsonl(path: &Path) -> Result<Vec<KimMessage>, String> {
    let file = fs::File::open(path).map_err(|e| e.to_string())?;
    let reader = BufReader::new(file);
    let mut messages = vec![];

    for (i, line) in reader.lines().enumerate() {
        let line = line.map_err(|e| e.to_string())?;
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        match serde_json::from_str::<KimMessage>(trimmed) {
            Ok(msg) => messages.push(msg),
            Err(e) => eprintln!("Skipping malformed JSONL line {}: {}", i + 1, e),
        }
    }

    Ok(messages)
}

// ---------------------------------------------------------------------------
// Tauri commands
// ---------------------------------------------------------------------------

#[tauri::command]
async fn list_sessions(
    kim_dir: Option<String>,
    claw_dir: Option<String>,
) -> Result<Vec<SessionInfo>, String> {
    let kim_base = kim_dir
        .map(PathBuf::from)
        .unwrap_or_else(default_sessions_dir);

    let mut sessions = read_sessions_from_dir(&kim_base, "kim")?;

    if let Some(claw_path) = claw_dir {
        let claw_base = PathBuf::from(claw_path);
        let claw_sessions = read_sessions_from_dir(&claw_base, "claw")?;
        sessions.extend(claw_sessions);
    }

    Ok(sessions)
}

#[tauri::command]
async fn load_session_messages(
    session_id: String,
    kim_dir: Option<String>,
    claw_dir: Option<String>,
) -> Result<Vec<KimMessage>, String> {
    // Search kim dir first, then claw dir
    let dirs_to_search: Vec<PathBuf> = {
        let mut v = vec![kim_dir
            .map(PathBuf::from)
            .unwrap_or_else(default_sessions_dir)];
        if let Some(claw_path) = claw_dir {
            v.push(PathBuf::from(claw_path));
        }
        v
    };

    for base in &dirs_to_search {
        if !base.exists() {
            continue;
        }
        let mut date_dirs: Vec<_> = fs::read_dir(base)
            .map_err(|e| e.to_string())?
            .filter_map(|e| e.ok())
            .filter(|e| e.path().is_dir())
            .collect();
        date_dirs.sort_by(|a, b| b.file_name().cmp(&a.file_name()));

        for date_entry in date_dirs {
            let candidate = date_entry
                .path()
                .join(format!("{}.jsonl", session_id));
            if candidate.exists() {
                return parse_jsonl(&candidate);
            }
        }
    }

    Err(format!("Session not found: {}", session_id))
}

#[tauri::command]
async fn get_app_version() -> String {
    env!("CARGO_PKG_VERSION").to_string()
}

#[tauri::command]
async fn send_task(
    task: String,
    provider: Option<String>,
    project_root: Option<String>,
    app_handle: tauri::AppHandle,
) -> Result<String, String> {
    use std::process::Stdio;
    use tokio::io::AsyncBufReadExt;
    use tokio::process::Command;

    let root = project_root
        .map(PathBuf::from)
        .unwrap_or_else(default_project_root);

    // Prefer venv python
    let python = {
        let unix = root.join("venv").join("bin").join("python");
        let win = root.join("venv").join("Scripts").join("python.exe");
        if unix.exists() {
            unix.to_string_lossy().to_string()
        } else if win.exists() {
            win.to_string_lossy().to_string()
        } else {
            "python".to_string()
        }
    };

    let mut cmd = Command::new(&python);
    cmd.args(["-m", "orchestrator.agent"])
        .arg("--task")
        .arg(&task)
        .current_dir(&root)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    if let Some(p) = provider {
        cmd.arg("--provider").arg(p);
    }

    let mut child = cmd
        .spawn()
        .map_err(|e| format!("Failed to start Kim: {}", e))?;

    if let Some(stdout) = child.stdout.take() {
        let reader = tokio::io::BufReader::new(stdout);
        let app = app_handle.clone();
        tokio::spawn(async move {
            let mut lines = reader.lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let _ = app.emit("kim-agent-output", line);
            }
        });
    }

    if let Some(stderr) = child.stderr.take() {
        let reader = tokio::io::BufReader::new(stderr);
        let app = app_handle.clone();
        tokio::spawn(async move {
            let mut lines = reader.lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let _ = app.emit("kim-agent-error", line);
            }
        });
    }

    let status = child.wait().await.map_err(|e| e.to_string())?;
    let _ = app_handle.emit("kim-agent-done", status.success());

    if status.success() {
        Ok("Task completed".to_string())
    } else {
        Err(format!("Agent exited with status: {}", status))
    }
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            list_sessions,
            load_session_messages,
            get_app_version,
            send_task,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

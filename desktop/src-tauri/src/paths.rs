// paths.rs — project-root / sessions-dir / date resolution helpers.
// Extracted from lib.rs (file-split restructure) — behavior unchanged.

use std::path::PathBuf;
use std::time::SystemTime;

/// Ancestors of the current executable, used to locate an installed Kim
/// project root (`kim/` containing orchestrator/). This lets the packaged
/// desktop app find its sibling Python project without any hardcoded user
/// directories.
pub(crate) fn exe_ancestor_kim_root() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    for ancestor in exe.ancestors() {
        // Heuristic: an ancestor that contains `orchestrator/agent.py` is
        // a valid Kim root. Works for both `kim/desktop/…/desktop` dev and
        // packaged-app layouts where the binary lives beside the project.
        if ancestor.join("orchestrator").join("agent.py").exists() {
            return Some(ancestor.to_path_buf());
        }
    }
    None
}

pub(crate) fn default_project_root() -> PathBuf {
    // 0a. Compile-time baked path — the only reliable option when the app runs
    //     from inside a .app bundle where no ancestor contains orchestrator/.
    //     Set by build.rs from CARGO_MANIFEST_DIR at build time.
    if let Some(baked) = option_env!("KIM_COMPILE_TIME_ROOT") {
        let p = PathBuf::from(baked);
        if p.exists() && p.join("orchestrator").join("agent.py").exists() {
            return p;
        }
    }

    // 0b. ~/.kim_root — written by install.sh so even a moved/renamed project
    //     can be found at runtime without a rebuild.
    if let Some(home) = dirs::home_dir() {
        let root_file = home.join(".kim_root");
        if let Ok(contents) = std::fs::read_to_string(&root_file) {
            let p = PathBuf::from(contents.trim());
            if p.exists() && p.join("orchestrator").join("agent.py").exists() {
                return p;
            }
        }
    }

    // 1. Environment override wins (explicit user intent).
    if let Ok(env_root) = std::env::var("KIM_PROJECT_ROOT") {
        let p = PathBuf::from(env_root);
        if p.exists() {
            return p;
        }
    }
    // 2. Walk up from the executable.
    if let Some(root) = exe_ancestor_kim_root() {
        return root;
    }
    // 3. ~/.kim (standard per-user install).
    if let Some(home) = dirs::home_dir() {
        let user = home.join(".kim");
        if user.exists() {
            return user;
        }
        // Return the default location even if not yet created
        return user;
    }
    PathBuf::from(".")
}

pub(crate) fn default_sessions_dir() -> PathBuf {
    // Environment override.
    if let Ok(env_dir) = std::env::var("KIM_SESSIONS_DIR") {
        let p = PathBuf::from(env_dir);
        if p.exists() {
            return p;
        }
    }
    // Project-root/kim_sessions if the project root was detected.
    let root = default_project_root();
    let root_sessions = root.join("kim_sessions");
    if root_sessions.exists() {
        return root_sessions;
    }
    // ~/.kim/sessions fallback.
    if let Some(home) = dirs::home_dir() {
        return home.join(".kim").join("sessions");
    }
    PathBuf::from("kim_sessions")
}

pub(crate) fn chrono_like_today() -> String {
    // Avoid adding a new dependency. Good enough for naming a fallback date dir;
    // most existing call sites pass the real session date.
    let secs = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let days = secs / 86_400;
    // Civil date conversion from days since Unix epoch.
    let z = days as i64 + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = mp + if mp < 10 { 3 } else { -9 };
    let year = y + if m <= 2 { 1 } else { 0 };
    format!("{:04}-{:02}-{:02}", year, m, d)
}

pub(crate) fn config_yaml_path(project_root: Option<String>) -> PathBuf {
    project_root
        .map(PathBuf::from)
        .unwrap_or_else(default_project_root)
        .join("config.yaml")
}

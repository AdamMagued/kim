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

/// True when *p* is a usable Kim project root (contains `orchestrator/agent.py`).
fn is_kim_root(p: &std::path::Path) -> bool {
    p.exists() && p.join("orchestrator").join("agent.py").exists()
}

/// Pure precedence resolver for the project root, with every environment /
/// home input injected so the ordering is deterministically testable.
///
/// F-D-2: the `KIM_PROJECT_ROOT` env override now WINS over the compile-time
/// baked path and `~/.kim_root`, as its own comment always claimed ("explicit
/// user intent"). Previously it was consulted third — so a developer who built
/// the `.app` and then set `KIM_PROJECT_ROOT` to a second checkout still got the
/// original baked tree (config.yaml, sessions, orchestrator all loaded from the
/// wrong root, silently). To keep the promotion safe, an env root is only
/// accepted when it is a real Kim root (`orchestrator/agent.py` present); an env
/// value that does not resolve falls through to the baked/`~/.kim_root` chain
/// exactly as before.
fn resolve_project_root(
    env_project_root: Option<String>,
    baked: Option<&str>,
    home: Option<PathBuf>,
) -> PathBuf {
    // 1. Explicit env override wins (verified to be a real Kim root).
    if let Some(env_root) = env_project_root {
        let p = PathBuf::from(env_root);
        if is_kim_root(&p) {
            return p;
        }
    }

    // 2. Compile-time baked path — the only reliable option when the app runs
    //    from inside a .app bundle where no ancestor contains orchestrator/.
    //    Set by build.rs from CARGO_MANIFEST_DIR at build time.
    if let Some(baked) = baked {
        let p = PathBuf::from(baked);
        if is_kim_root(&p) {
            return p;
        }
    }

    // 3. ~/.kim_root — written by install.sh so even a moved/renamed project
    //    can be found at runtime without a rebuild.
    if let Some(home) = &home {
        let root_file = home.join(".kim_root");
        if let Ok(contents) = std::fs::read_to_string(&root_file) {
            let p = PathBuf::from(contents.trim());
            if is_kim_root(&p) {
                return p;
            }
        }
    }

    // 4. Walk up from the executable.
    if let Some(root) = exe_ancestor_kim_root() {
        return root;
    }

    // 5. ~/.kim (standard per-user install). Return the default location even
    //    if not yet created.
    if let Some(home) = home {
        return home.join(".kim");
    }
    PathBuf::from(".")
}

pub(crate) fn default_project_root() -> PathBuf {
    resolve_project_root(
        std::env::var("KIM_PROJECT_ROOT").ok(),
        option_env!("KIM_COMPILE_TIME_ROOT"),
        dirs::home_dir(),
    )
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

#[cfg(test)]
mod tests {
    use super::*;

    /// Build a directory that looks like a real Kim root (`orchestrator/agent.py`).
    fn make_kim_root(dir: &std::path::Path) {
        let orch = dir.join("orchestrator");
        std::fs::create_dir_all(&orch).unwrap();
        std::fs::write(orch.join("agent.py"), b"# fake").unwrap();
    }

    #[test]
    fn env_project_root_wins_over_baked_and_kim_root_file() {
        let tmp = tempfile::tempdir().unwrap();
        let env_root = tmp.path().join("env_checkout");
        let baked_root = tmp.path().join("baked_checkout");
        let home = tmp.path().join("home");
        make_kim_root(&env_root);
        make_kim_root(&baked_root);
        // ~/.kim_root also points at yet another valid root.
        let file_root = tmp.path().join("file_checkout");
        make_kim_root(&file_root);
        std::fs::create_dir_all(&home).unwrap();
        std::fs::write(home.join(".kim_root"), file_root.to_string_lossy().as_bytes()).unwrap();

        let resolved = resolve_project_root(
            Some(env_root.to_string_lossy().into_owned()),
            Some(baked_root.to_str().unwrap()),
            Some(home.clone()),
        );
        // F-D-2: the explicit env override must win over BOTH the baked path
        // and ~/.kim_root, even though all three are valid Kim roots.
        assert_eq!(resolved, env_root);
    }

    #[test]
    fn invalid_env_root_falls_through_to_baked() {
        let tmp = tempfile::tempdir().unwrap();
        // Env points at a dir that exists but is NOT a Kim root (no agent.py).
        let bogus_env = tmp.path().join("not_kim");
        std::fs::create_dir_all(&bogus_env).unwrap();
        let baked_root = tmp.path().join("baked_checkout");
        make_kim_root(&baked_root);

        let resolved = resolve_project_root(
            Some(bogus_env.to_string_lossy().into_owned()),
            Some(baked_root.to_str().unwrap()),
            None,
        );
        // An env value that is not a real Kim root does not hijack resolution.
        assert_eq!(resolved, baked_root);
    }

    #[test]
    fn baked_wins_when_no_env_override() {
        let tmp = tempfile::tempdir().unwrap();
        let baked_root = tmp.path().join("baked_checkout");
        make_kim_root(&baked_root);
        let resolved =
            resolve_project_root(None, Some(baked_root.to_str().unwrap()), None);
        assert_eq!(resolved, baked_root);
    }
}

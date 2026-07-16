//! Resolves the codex/kimcli backend binary for the Code tab (#61).
//!
//! Extracted out of `subprocess.rs` — that file is already at the file-size
//! gate's cap (see `scripts/check_file_size_gate.py`) and may not grow, so
//! the fallback chain lives here instead.
//!
//! Fallback chain (first match wins):
//!   1. `CODEX_BIN` env var, if it names an existing file
//!   2. `~/.kim/bin/kimcli` (scripts/install_kimcli.sh's install target)
//!   3. `kimcli` resolved on PATH
//!   4. `codex` resolved on PATH

use std::path::{Path, PathBuf};

use crate::subprocess::executable_on_path;

/// Locate the codex/kimcli backend binary. See module docs for the chain.
pub(crate) fn resolve_code_backend() -> Option<PathBuf> {
    resolve_code_backend_with(
        std::env::var("CODEX_BIN").ok(),
        dirs::home_dir(),
        |p: &Path| p.is_file(),
        executable_on_path,
    )
}

/// Pure core of `resolve_code_backend`, parameterized over its I/O so the
/// fallback ordering is unit-testable without touching real env vars, the
/// filesystem, or PATH.
fn resolve_code_backend_with(
    codex_bin_env: Option<String>,
    home: Option<PathBuf>,
    is_file: impl Fn(&Path) -> bool,
    on_path: impl Fn(&str) -> Option<PathBuf>,
) -> Option<PathBuf> {
    if let Some(raw) = codex_bin_env {
        let path = PathBuf::from(raw);
        if is_file(&path) {
            return Some(path);
        }
    }
    if let Some(home) = home {
        let kimcli = home.join(".kim").join("bin").join("kimcli");
        if is_file(&kimcli) {
            return Some(kimcli);
        }
    }
    on_path("kimcli").or_else(|| on_path("codex"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn codex_bin_env_wins_when_it_names_a_real_file() {
        let result = resolve_code_backend_with(
            Some("/custom/codex-bin".to_string()),
            Some(PathBuf::from("/home/kimuser")),
            |p| p == Path::new("/custom/codex-bin"),
            |_| panic!("should not fall through to PATH lookup"),
        );
        assert_eq!(result, Some(PathBuf::from("/custom/codex-bin")));
    }

    #[test]
    fn codex_bin_env_pointing_at_missing_file_falls_through() {
        let result = resolve_code_backend_with(
            Some("/does/not/exist".to_string()),
            Some(PathBuf::from("/home/kimuser")),
            |p| p == Path::new("/home/kimuser/.kim/bin/kimcli"),
            |_| None,
        );
        assert_eq!(
            result,
            Some(PathBuf::from("/home/kimuser/.kim/bin/kimcli"))
        );
    }

    #[test]
    fn kim_bin_kimcli_used_when_no_env_and_file_exists() {
        let result = resolve_code_backend_with(
            None,
            Some(PathBuf::from("/home/kimuser")),
            |p| p == Path::new("/home/kimuser/.kim/bin/kimcli"),
            |_| panic!("should not fall through to PATH lookup"),
        );
        assert_eq!(
            result,
            Some(PathBuf::from("/home/kimuser/.kim/bin/kimcli"))
        );
    }

    #[test]
    fn which_kimcli_used_when_no_env_and_no_kim_bin_install() {
        let result = resolve_code_backend_with(
            None,
            Some(PathBuf::from("/home/kimuser")),
            |_| false,
            |name| (name == "kimcli").then(|| PathBuf::from("/opt/homebrew/bin/kimcli")),
        );
        assert_eq!(result, Some(PathBuf::from("/opt/homebrew/bin/kimcli")));
    }

    #[test]
    fn which_codex_used_when_kimcli_absent_everywhere() {
        let result = resolve_code_backend_with(
            None,
            None,
            |_| false,
            |name| (name == "codex").then(|| PathBuf::from("/usr/local/bin/codex")),
        );
        assert_eq!(result, Some(PathBuf::from("/usr/local/bin/codex")));
    }

    #[test]
    fn none_when_nothing_matches() {
        let result = resolve_code_backend_with(None, None, |_| false, |_| None);
        assert_eq!(result, None);
    }

    #[test]
    fn missing_home_dir_skips_kim_bin_check_without_panicking() {
        let result = resolve_code_backend_with(
            None,
            None,
            |_| panic!("is_file should not be probed when there is no home dir to build a path from"),
            |name| (name == "codex").then(|| PathBuf::from("/usr/bin/codex")),
        );
        assert_eq!(result, Some(PathBuf::from("/usr/bin/codex")));
    }
}

//! kimcli/codex binary resolution. Split out of the former single-file
//! `commands/tui.rs` — pure relocation, no behavior changes.

use std::path::PathBuf;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum KimcliResolution {
    /// The real `kimcli` binary was found.
    Kimcli(PathBuf),
    /// No `kimcli` binary anywhere; falling back to the upstream `codex`
    /// binary on PATH. Callers should warn — the branding (and possibly some
    /// behavior) differs from a proper kimcli install.
    CodexFallback(PathBuf),
}

/// Resolution order: `$CODEX_BIN` env var → `~/.kim/bin/kimcli` (via
/// `config::kim_home()`) → `kimcli` on PATH → `codex` on PATH (fallback,
/// upstream branding) → an actionable error mentioning
/// `scripts/install_kimcli.sh`.
///
/// Dependency-injected (`kim_home_dir`, `which_fn`) so binary resolution is
/// unit-testable without touching the real filesystem or PATH.
pub(crate) fn resolve_kimcli_binary_with<W>(
    codex_bin_env: Option<String>,
    kim_home_dir: Option<PathBuf>,
    which_fn: W,
) -> Result<KimcliResolution, String>
where
    W: Fn(&str) -> Option<PathBuf>,
{
    if let Some(explicit) = codex_bin_env.filter(|v| !v.trim().is_empty()) {
        return Ok(KimcliResolution::Kimcli(PathBuf::from(explicit)));
    }
    if let Some(home) = kim_home_dir {
        let bin_name = if cfg!(windows) {
            "kimcli.exe"
        } else {
            "kimcli"
        };
        let candidate = home.join(".kim").join("bin").join(bin_name);
        if candidate.is_file() {
            return Ok(KimcliResolution::Kimcli(candidate));
        }
    }
    if let Some(path) = which_fn("kimcli") {
        return Ok(KimcliResolution::Kimcli(path));
    }
    if let Some(path) = which_fn("codex") {
        return Ok(KimcliResolution::CodexFallback(path));
    }
    Err(
        "kimcli binary not found (checked $CODEX_BIN, ~/.kim/bin/kimcli, and PATH — including \
         the upstream 'codex' binary). Install it by running scripts/install_kimcli.sh from the \
         Kim repo, or set CODEX_BIN to an explicit path."
            .to_string(),
    )
}

pub(crate) fn resolve_kimcli_binary() -> Result<KimcliResolution, String> {
    resolve_kimcli_binary_with(
        std::env::var("CODEX_BIN").ok(),
        crate::config::kim_home(),
        crate::agentic::which,
    )
}

#[cfg(test)]
mod tests {
    use super::{resolve_kimcli_binary_with, KimcliResolution};
    use std::path::PathBuf;

    #[test]
    fn resolve_kimcli_binary_prefers_codex_bin_env_over_everything() {
        let resolved = resolve_kimcli_binary_with(
            Some("/explicit/codex-bin".to_string()),
            Some(PathBuf::from("/home/whoever")),
            |_| Some(PathBuf::from("/usr/bin/kimcli")),
        )
        .unwrap();
        assert_eq!(
            resolved,
            KimcliResolution::Kimcli(PathBuf::from("/explicit/codex-bin"))
        );
    }

    #[test]
    fn resolve_kimcli_binary_blank_codex_bin_env_is_ignored() {
        let dir = tempfile::tempdir().unwrap();
        let bin_dir = dir.path().join(".kim").join("bin");
        std::fs::create_dir_all(&bin_dir).unwrap();
        let bin_name = if cfg!(windows) {
            "kimcli.exe"
        } else {
            "kimcli"
        };
        let kimcli_path = bin_dir.join(bin_name);
        std::fs::write(&kimcli_path, b"stub").unwrap();

        let resolved = resolve_kimcli_binary_with(
            Some("   ".to_string()),
            Some(dir.path().to_path_buf()),
            |_| None,
        )
        .unwrap();
        assert_eq!(resolved, KimcliResolution::Kimcli(kimcli_path));
    }

    #[test]
    fn resolve_kimcli_binary_finds_kim_home_bin_kimcli() {
        let dir = tempfile::tempdir().unwrap();
        let bin_dir = dir.path().join(".kim").join("bin");
        std::fs::create_dir_all(&bin_dir).unwrap();
        let bin_name = if cfg!(windows) {
            "kimcli.exe"
        } else {
            "kimcli"
        };
        let kimcli_path = bin_dir.join(bin_name);
        std::fs::write(&kimcli_path, b"stub").unwrap();

        let resolved =
            resolve_kimcli_binary_with(None, Some(dir.path().to_path_buf()), |_| None).unwrap();
        assert_eq!(resolved, KimcliResolution::Kimcli(kimcli_path));
    }

    #[test]
    fn resolve_kimcli_binary_falls_back_to_path_kimcli() {
        let dir = tempfile::tempdir().unwrap(); // no .kim/bin/kimcli inside
        let resolved = resolve_kimcli_binary_with(None, Some(dir.path().to_path_buf()), |name| {
            (name == "kimcli").then(|| PathBuf::from("/usr/local/bin/kimcli"))
        })
        .unwrap();
        assert_eq!(
            resolved,
            KimcliResolution::Kimcli(PathBuf::from("/usr/local/bin/kimcli"))
        );
    }

    #[test]
    fn resolve_kimcli_binary_falls_back_to_codex_with_warning_variant() {
        let dir = tempfile::tempdir().unwrap();
        let resolved = resolve_kimcli_binary_with(None, Some(dir.path().to_path_buf()), |name| {
            (name == "codex").then(|| PathBuf::from("/usr/local/bin/codex"))
        })
        .unwrap();
        assert_eq!(
            resolved,
            KimcliResolution::CodexFallback(PathBuf::from("/usr/local/bin/codex"))
        );
    }

    #[test]
    fn resolve_kimcli_binary_none_found_is_an_actionable_error() {
        let dir = tempfile::tempdir().unwrap();
        let err =
            resolve_kimcli_binary_with(None, Some(dir.path().to_path_buf()), |_| None).unwrap_err();
        assert!(err.contains("install_kimcli.sh"), "got: {err}");
        assert!(err.contains("CODEX_BIN"), "got: {err}");
    }
}

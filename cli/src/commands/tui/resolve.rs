//! kimcli binary resolution. Split out of the former single-file
//! `commands/tui.rs` — pure relocation, no behavior changes.

use std::path::PathBuf;

/// Resolution order: `$CODEX_BIN` env var → `~/.kim/bin/kimcli` (via
/// `config::kim_home()`) → `kimcli` on PATH → an actionable error mentioning
/// `scripts/install_kimcli.sh`.
///
/// kimcli is a standalone product: this deliberately does NOT fall back to
/// an upstream `codex` binary that might happen to be on PATH (e.g. a dev
/// machine with `@openai/codex` installed via npm). Falling back there would
/// make a dev machine behave differently from a clean user machine and mask
/// real kimcli bugs behind an unbranded binary — see issue #61.
///
/// **Self-spawn guard (#61 review, FIX 2):** the `kim-cli` crate ships a
/// SECOND bin also named `kimcli` — the `kim tui` launcher itself
/// (`src/bin/kimcli.rs`, `Cargo.toml`'s two `[[bin]]` targets). `cargo
/// install --path cli` puts both `kim` and `kimcli` on PATH, so on a machine
/// without `~/.kim/bin/kimcli` the PATH fallback would resolve the LAUNCHER
/// as if it were the real TUI binary (the rebranded-codex-cli fork built and
/// installed separately by `scripts/install_kimcli.sh`), spawn it with the
/// `-c key=value` override argv (`argv.rs`), and it would die with
/// `kim tui: unknown option '-c'` (exit 2) after uselessly starting a proxy
/// — see `parse_tui_flags`'s `other => Err(...)` arm, which the launcher's
/// own `main()` runs on those same args. Every candidate is therefore
/// checked against `std::env::current_exe()` (canonicalized, so symlinks
/// and `..` components can't hide the match) and rejected if it IS this
/// process's own binary — including an explicit `$CODEX_BIN` override,
/// since a self-path is never correct there either, it just means the
/// caller's environment is misconfigured. A rejected candidate is treated
/// exactly like "not found" at that stage and resolution keeps going, so a
/// legitimate `~/.kim/bin/kimcli` or PATH `kimcli` still wins if one exists;
/// if nothing else resolves, the existing actionable install-script error
/// fires. (A `--version`-output check was considered too — the real binary
/// prints `kimcli X.Y.Z (rebranded codex-cli A.B.C)`, see
/// `tests/test_kimcli_binary.py` — but that requires spawning a subprocess
/// on every `kim tui` launch just to validate resolution; the cheap,
/// subprocess-free canonical-path comparison already closes the specific
/// self-spawn loop this bug describes.)
///
/// Dependency-injected (`kim_home_dir`, `which_fn`, `current_exe`) so binary
/// resolution is unit-testable without touching the real filesystem, PATH,
/// or the test binary's own path.
pub(crate) fn resolve_kimcli_binary_with<W>(
    codex_bin_env: Option<String>,
    kim_home_dir: Option<PathBuf>,
    which_fn: W,
    current_exe: Option<PathBuf>,
) -> Result<PathBuf, String>
where
    W: Fn(&str) -> Option<PathBuf>,
{
    let canonical_current_exe = current_exe.and_then(|exe| exe.canonicalize().ok());
    let is_self_path = |candidate: &PathBuf| -> bool {
        match (&canonical_current_exe, candidate.canonicalize()) {
            (Some(exe), Ok(canon_candidate)) => *exe == canon_candidate,
            _ => false,
        }
    };

    if let Some(explicit) = codex_bin_env.filter(|v| !v.trim().is_empty()) {
        let candidate = PathBuf::from(explicit);
        if !is_self_path(&candidate) {
            return Ok(candidate);
        }
        // An explicit $CODEX_BIN pointing at ourselves is still wrong (it
        // would self-spawn the launcher with `-c` argv it doesn't
        // understand) — fall through to the remaining resolution stages
        // instead of trusting it blindly.
    }
    if let Some(home) = kim_home_dir {
        let bin_name = if cfg!(windows) {
            "kimcli.exe"
        } else {
            "kimcli"
        };
        let candidate = home.join(".kim").join("bin").join(bin_name);
        if candidate.is_file() && !is_self_path(&candidate) {
            return Ok(candidate);
        }
    }
    if let Some(path) = which_fn("kimcli") {
        if !is_self_path(&path) {
            return Ok(path);
        }
    }
    Err(
        "kimcli binary not found (checked $CODEX_BIN, ~/.kim/bin/kimcli, and PATH). Install it \
         by running scripts/install_kimcli.sh from the Kim repo, or set CODEX_BIN to an explicit \
         path."
            .to_string(),
    )
}

pub(crate) fn resolve_kimcli_binary() -> Result<PathBuf, String> {
    resolve_kimcli_binary_with(
        std::env::var("CODEX_BIN").ok(),
        crate::config::kim_home(),
        crate::agentic::which,
        std::env::current_exe().ok(),
    )
}

#[cfg(test)]
mod tests {
    use super::resolve_kimcli_binary_with;
    use std::path::PathBuf;

    #[test]
    fn resolve_kimcli_binary_prefers_codex_bin_env_over_everything() {
        let resolved = resolve_kimcli_binary_with(
            Some("/explicit/codex-bin".to_string()),
            Some(PathBuf::from("/home/whoever")),
            |_| Some(PathBuf::from("/usr/bin/kimcli")),
            None,
        )
        .unwrap();
        assert_eq!(resolved, PathBuf::from("/explicit/codex-bin"));
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
            None,
        )
        .unwrap();
        assert_eq!(resolved, kimcli_path);
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
            resolve_kimcli_binary_with(None, Some(dir.path().to_path_buf()), |_| None, None)
                .unwrap();
        assert_eq!(resolved, kimcli_path);
    }

    #[test]
    fn resolve_kimcli_binary_falls_back_to_path_kimcli() {
        let dir = tempfile::tempdir().unwrap(); // no .kim/bin/kimcli inside
        let resolved = resolve_kimcli_binary_with(
            None,
            Some(dir.path().to_path_buf()),
            |name| (name == "kimcli").then(|| PathBuf::from("/usr/local/bin/kimcli")),
            None,
        )
        .unwrap();
        assert_eq!(resolved, PathBuf::from("/usr/local/bin/kimcli"));
    }

    #[test]
    fn resolve_kimcli_binary_does_not_fall_back_to_codex_on_path() {
        // A `codex` binary on PATH (e.g. a dev machine with @openai/codex
        // installed via npm) must NOT be treated as a kimcli stand-in by the
        // launcher — kimcli is a standalone product, and silently running
        // upstream codex here would mask real kimcli bugs (#61). Only
        // `kimcli` on PATH resolves; `codex` is ignored entirely.
        let dir = tempfile::tempdir().unwrap();
        let err = resolve_kimcli_binary_with(
            None,
            Some(dir.path().to_path_buf()),
            |name| (name == "codex").then(|| PathBuf::from("/usr/local/bin/codex")),
            None,
        )
        .unwrap_err();
        assert!(err.contains("install_kimcli.sh"), "got: {err}");
    }

    #[test]
    fn resolve_kimcli_binary_none_found_is_an_actionable_error() {
        let dir = tempfile::tempdir().unwrap();
        let err = resolve_kimcli_binary_with(None, Some(dir.path().to_path_buf()), |_| None, None)
            .unwrap_err();
        assert!(err.contains("install_kimcli.sh"), "got: {err}");
        assert!(err.contains("CODEX_BIN"), "got: {err}");
    }

    #[test]
    fn resolve_kimcli_binary_nothing_resolves_anywhere_gives_install_guidance() {
        // No $CODEX_BIN, no ~/.kim/bin/kimcli, nothing on PATH at all
        // (including no `codex`) — must fail with actionable guidance,
        // not silently succeed via some fallback.
        let dir = tempfile::tempdir().unwrap(); // no .kim/bin/kimcli inside
        let err = resolve_kimcli_binary_with(
            None,
            Some(dir.path().to_path_buf()),
            |_name: &str| None,
            None,
        )
        .unwrap_err();
        assert!(err.contains("install_kimcli.sh"), "got: {err}");
        assert!(err.contains("CODEX_BIN"), "got: {err}");
        assert!(err.contains("kimcli"), "got: {err}");
    }

    // ── FIX 2 (#61 review): self-spawn guard ───────────────────────────────

    #[test]
    fn resolve_kimcli_binary_rejects_path_kimcli_that_is_actually_ourselves() {
        // Reproduces the exact bug: `cargo install --path cli` puts both
        // `kim` and `kimcli` (the launcher) on PATH. With no
        // ~/.kim/bin/kimcli installed, a naive PATH lookup for "kimcli"
        // resolves the LAUNCHER binary itself — spawning it would feed it
        // `-c key=value` argv it doesn't understand
        // (`parse_tui_flags`'s `unknown option` error, exit 2). The
        // self-spawn guard must reject that candidate and fall through to
        // the actionable install error instead of a self-referential spawn.
        let dir = tempfile::tempdir().unwrap();
        let self_bin = dir.path().join("kimcli");
        std::fs::write(&self_bin, b"stub launcher binary").unwrap();
        let no_kim_home = dir.path().join("no-such-home");

        let err = resolve_kimcli_binary_with(
            None,
            Some(no_kim_home),
            |name| (name == "kimcli").then(|| self_bin.clone()),
            Some(self_bin.clone()),
        )
        .unwrap_err();
        assert!(err.contains("install_kimcli.sh"), "got: {err}");
    }

    #[test]
    fn resolve_kimcli_binary_rejects_self_path_via_kim_home_too() {
        // Same guard applies to the ~/.kim/bin/kimcli stage — a corrupted
        // install that symlinked/copied the launcher there must not be
        // treated as the real TUI binary either.
        let dir = tempfile::tempdir().unwrap();
        let bin_dir = dir.path().join(".kim").join("bin");
        std::fs::create_dir_all(&bin_dir).unwrap();
        let bin_name = if cfg!(windows) {
            "kimcli.exe"
        } else {
            "kimcli"
        };
        let self_bin = bin_dir.join(bin_name);
        std::fs::write(&self_bin, b"stub launcher binary").unwrap();

        let err = resolve_kimcli_binary_with(
            None,
            Some(dir.path().to_path_buf()),
            |_| None,
            Some(self_bin.clone()),
        )
        .unwrap_err();
        assert!(err.contains("install_kimcli.sh"), "got: {err}");
    }

    #[test]
    fn resolve_kimcli_binary_still_finds_a_distinct_kim_home_binary() {
        // The self-spawn guard must not over-reject: a genuinely different
        // binary at ~/.kim/bin/kimcli (the normal, correct install layout)
        // still resolves even though `current_exe` points elsewhere.
        let dir = tempfile::tempdir().unwrap();
        let bin_dir = dir.path().join(".kim").join("bin");
        std::fs::create_dir_all(&bin_dir).unwrap();
        let bin_name = if cfg!(windows) {
            "kimcli.exe"
        } else {
            "kimcli"
        };
        let kimcli_path = bin_dir.join(bin_name);
        std::fs::write(&kimcli_path, b"the real rebranded codex-cli binary").unwrap();

        let self_bin = dir.path().join("kim"); // a distinct `kim` binary
        std::fs::write(&self_bin, b"the kim launcher").unwrap();

        let resolved = resolve_kimcli_binary_with(
            None,
            Some(dir.path().to_path_buf()),
            |_| None,
            Some(self_bin),
        )
        .unwrap();
        assert_eq!(resolved, kimcli_path);
    }

    #[test]
    fn resolve_kimcli_binary_rejects_explicit_codex_bin_pointing_at_self_but_keeps_resolving() {
        // Even an explicit $CODEX_BIN override pointing at ourselves is
        // wrong (it would still self-spawn) — reject it and fall through
        // rather than trusting it blindly, but a legitimate kim_home
        // binary underneath must still win.
        let dir = tempfile::tempdir().unwrap();
        let bin_dir = dir.path().join(".kim").join("bin");
        std::fs::create_dir_all(&bin_dir).unwrap();
        let bin_name = if cfg!(windows) {
            "kimcli.exe"
        } else {
            "kimcli"
        };
        let kimcli_path = bin_dir.join(bin_name);
        std::fs::write(&kimcli_path, b"the real rebranded codex-cli binary").unwrap();

        let self_bin = dir.path().join("kimcli-launcher");
        std::fs::write(&self_bin, b"self").unwrap();

        let resolved = resolve_kimcli_binary_with(
            Some(self_bin.to_string_lossy().to_string()),
            Some(dir.path().to_path_buf()),
            |_| None,
            Some(self_bin.clone()),
        )
        .unwrap();
        assert_eq!(resolved, kimcli_path);
    }
}

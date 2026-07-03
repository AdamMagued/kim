//! GUI-launch PATH repair (issue #23).
//!
//! macOS (and some Linux desktop) launches hand the app launchd's minimal
//! PATH (`/usr/bin:/bin:/usr/sbin:/sbin`), so `which ollama`, `ollama ps`,
//! `ollama pull`, and every other shell-out that worked in `npm run tauri dev`
//! (which inherits the terminal's PATH) silently fails in the packaged .app.
//!
//! `fix_gui_path()` asks the user's login shell for its PATH (non-interactive
//! login shell, so `.zprofile`/`.profile` — where Homebrew/ollama installers
//! put their exports — are read without risking an interactive-rc hang) and
//! appends any missing entries, plus a hardcoded set of well-known install
//! dirs as a final fallback. Existing PATH entries keep priority; we only
//! append, never reorder, so system binaries still win lookups.

#[cfg(unix)]
pub(crate) fn fix_gui_path() {
    let current = std::env::var("PATH").unwrap_or_default();
    let mut entries: Vec<String> = std::env::split_paths(&current)
        .map(|p| p.to_string_lossy().into_owned())
        .filter(|p| !p.is_empty())
        .collect();

    let push_missing = |candidate: &str, entries: &mut Vec<String>| {
        if !candidate.is_empty() && !entries.iter().any(|e| e == candidate) {
            entries.push(candidate.to_string());
        }
    };

    if let Some(shell_path) = login_shell_path() {
        for part in shell_path.split(':') {
            push_missing(part, &mut entries);
        }
    }

    // Well-known install locations, in case the login shell probe failed
    // (unusual shells, broken profiles). The ollama.com installer links into
    // /usr/local/bin; Homebrew on Apple Silicon uses /opt/homebrew.
    let mut fallbacks = vec![
        "/usr/local/bin".to_string(),
        "/opt/homebrew/bin".to_string(),
        "/opt/homebrew/sbin".to_string(),
    ];
    if let Some(home) = dirs::home_dir() {
        fallbacks.push(home.join(".local/bin").to_string_lossy().into_owned());
    }
    for dir in &fallbacks {
        push_missing(dir, &mut entries);
    }

    let merged = entries.join(":");
    if merged != current {
        std::env::set_var("PATH", &merged);
    }
}

#[cfg(not(unix))]
pub(crate) fn fix_gui_path() {}

/// Ask the user's login shell for its PATH. Runs `$SHELL -lc 'printf %s "$PATH"'`
/// with a hard 2-second kill so a hanging profile script can never stall app
/// startup. Returns None on any failure — callers fall back to the hardcoded
/// directory list.
#[cfg(unix)]
fn login_shell_path() -> Option<String> {
    use std::time::{Duration, Instant};

    let shell = std::env::var("SHELL")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| "/bin/sh".to_string());

    let mut child = std::process::Command::new(&shell)
        .args(["-lc", r#"printf '%s' "$PATH""#])
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .spawn()
        .ok()?;

    // Poor-man's timeout: poll try_wait, kill after 2s. std::process has no
    // built-in timeout and pulling in a crate for one call isn't worth it.
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                if !status.success() {
                    return None;
                }
                break;
            }
            Ok(None) => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    return None;
                }
                std::thread::sleep(Duration::from_millis(25));
            }
            Err(_) => return None,
        }
    }

    let mut out = String::new();
    use std::io::Read as _;
    child.stdout.take()?.read_to_string(&mut out).ok()?;
    let trimmed = out.trim();
    // Sanity: a real PATH has at least one absolute entry.
    if trimmed.contains('/') {
        Some(trimmed.to_string())
    } else {
        None
    }
}

#[cfg(all(test, unix))]
mod tests {
    // fix_gui_path mutates process-global env, so tests assert the pure parts
    // via a scoped run and only check the invariants that are safe to observe.

    #[test]
    fn fix_gui_path_appends_known_dirs_and_keeps_existing_order() {
        // Snapshot, run, restore.
        let original = std::env::var("PATH").unwrap_or_default();
        super::fix_gui_path();
        let fixed = std::env::var("PATH").unwrap_or_default();
        std::env::set_var("PATH", &original);

        // Existing entries keep their positions (prefix preserved).
        assert!(
            fixed.starts_with(&original) || original.is_empty(),
            "existing PATH must remain a prefix; got {fixed}"
        );
        // Well-known dirs are present afterwards.
        assert!(fixed.split(':').any(|p| p == "/usr/local/bin"));
        assert!(fixed.split(':').any(|p| p == "/opt/homebrew/bin"));
        // The APPENDED segment introduces no duplicates — neither of an
        // existing entry nor among itself. (The pre-existing PATH may already
        // contain duplicates; we preserve it verbatim and never dedupe it.)
        let original_entries: Vec<&str> =
            original.split(':').filter(|p| !p.is_empty()).collect();
        let fixed_entries: Vec<&str> = fixed.split(':').filter(|p| !p.is_empty()).collect();
        let appended = &fixed_entries[original_entries.len()..];
        let mut seen = std::collections::HashSet::new();
        for p in appended {
            assert!(
                !original_entries.contains(p),
                "appended entry duplicates an existing one: {p}"
            );
            assert!(seen.insert(p.to_string()), "appended entry repeated: {p}");
        }
    }
}

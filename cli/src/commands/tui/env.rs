//! The kimcli child's environment allowlist. Split out of the former
//! single-file `commands/tui.rs` — pure relocation, no behavior changes.

/// Build the kimcli child's environment from an explicit `(key, value)`
/// iterator (the real parent env, or a fixture in tests). Mirrors
/// `orchestrator/codex_bridge_service.py`'s `_run_exec_task` minimal env
/// (lines ~772-805): PATH (falling back to `Path`), HOME (falling back to
/// USERPROFILE on Windows), USER (falling back to USERNAME), TMPDIR
/// (falling back to TEMP/TMP), LANG, every `LC_*` locale var, TERM,
/// COLORTERM, TERMINFO — plus, mirroring
/// `mcp_server/policy_platform._with_windows_env_aliases(...,
/// include_runtime=True)` (which `_run_exec_task` applies whenever
/// `sys.platform == "win32"`), the Windows runtime vars SYSTEMROOT,
/// COMSPEC, USERPROFILE, TEMP, TMP, PATHEXT are forwarded *verbatim* under
/// their own native names, in addition to (not instead of) the HOME/TMPDIR
/// aliases derived from the same source vars — so e.g. a Windows parent's
/// `USERPROFILE` ends up in the child as both `HOME` and `USERPROFILE`,
/// exactly like the Python twin. Without SYSTEMROOT the child breaks
/// winsock/DNS resolution; without COMSPEC/PATHEXT it breaks shelling out;
/// without the PATH `Path`-casing fallback, combined with the caller's
/// `Command::env_clear()` (mod.rs), the child gets NO PATH at all on a
/// real Windows host. The well-known alternate castings Windows actually
/// uses (`Path`, `SystemRoot`, `ComSpec`) are matched explicitly per key —
/// USERPROFILE/USERNAME/TEMP/TMP/PATHEXT are already all-caps on a real
/// Windows host. These extra keys are never set by non-Windows hosts, so
/// the pass is a no-op there. Deliberately does NOT forward CODEX_HOME or
/// OPENAI_BASE_URL — those are set explicitly by the caller per-route (or
/// left unset so kimcli/codex uses its own home).
pub(crate) fn build_child_env_from<I>(parent_env: I) -> Vec<(String, String)>
where
    I: IntoIterator<Item = (String, String)>,
{
    let map: std::collections::HashMap<String, String> = parent_env.into_iter().collect();
    let mut out = Vec::new();

    if let Some(home) = map.get("HOME").or_else(|| map.get("USERPROFILE")) {
        out.push(("HOME".to_string(), home.clone()));
    }
    if let Some(user) = map.get("USER").or_else(|| map.get("USERNAME")) {
        out.push(("USER".to_string(), user.clone()));
    }
    if let Some(tmp) = map
        .get("TMPDIR")
        .or_else(|| map.get("TEMP"))
        .or_else(|| map.get("TMP"))
    {
        out.push(("TMPDIR".to_string(), tmp.clone()));
    }
    // PATH: Windows conventionally cases this `Path` (matches
    // `os.environ.get("PATH") or os.environ.get("Path", "")` in
    // `_run_exec_task`'s unconditional top-level env construction).
    if let Some(path) = map.get("PATH").or_else(|| map.get("Path")) {
        out.push(("PATH".to_string(), path.clone()));
    }
    for key in ["LANG", "TERM", "COLORTERM", "TERMINFO"] {
        if let Some(value) = map.get(key) {
            out.push((key.to_string(), value.clone()));
        }
    }
    let mut lc_keys: Vec<&String> = map.keys().filter(|k| k.starts_with("LC_")).collect();
    lc_keys.sort();
    for key in lc_keys {
        out.push((key.clone(), map[key].clone()));
    }

    // Windows runtime vars: forwarded verbatim, in addition to the
    // HOME/TMPDIR aliases above, matching
    // `_with_windows_env_aliases(..., include_runtime=True)` exactly (see
    // doc comment above).
    for (canonical, aliases) in [
        ("SYSTEMROOT", &["SYSTEMROOT", "SystemRoot"][..]),
        ("COMSPEC", &["COMSPEC", "ComSpec"][..]),
        ("USERPROFILE", &["USERPROFILE"][..]),
        ("TEMP", &["TEMP"][..]),
        ("TMP", &["TMP"][..]),
        ("PATHEXT", &["PATHEXT"][..]),
    ] {
        if let Some(value) = aliases.iter().find_map(|alias| map.get(*alias)) {
            out.push((canonical.to_string(), value.clone()));
        }
    }

    out
}

pub(crate) fn build_child_env() -> Vec<(String, String)> {
    build_child_env_from(std::env::vars())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env_pairs(pairs: &[(&str, &str)]) -> Vec<(String, String)> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    #[test]
    fn build_child_env_keeps_the_allowlist_and_drops_everything_else() {
        let env = build_child_env_from(env_pairs(&[
            ("PATH", "/usr/bin"),
            ("HOME", "/home/kim"),
            ("USER", "kim"),
            ("TMPDIR", "/tmp"),
            ("LANG", "en_US.UTF-8"),
            ("TERM", "xterm-256color"),
            ("COLORTERM", "truecolor"),
            ("TERMINFO", "/usr/share/terminfo"),
            ("LC_ALL", "C"),
            ("LC_CTYPE", "en_US.UTF-8"),
            // Must be dropped:
            ("CODEX_HOME", "/should/not/leak"),
            ("OPENAI_BASE_URL", "http://should-not-leak"),
            ("SECRET_TOKEN", "leaked-if-present"),
        ]));
        let map: std::collections::HashMap<_, _> = env.into_iter().collect();

        assert_eq!(map.get("TERM").map(String::as_str), Some("xterm-256color"));
        assert_eq!(map.get("PATH").map(String::as_str), Some("/usr/bin"));
        assert_eq!(map.get("HOME").map(String::as_str), Some("/home/kim"));
        assert_eq!(map.get("LC_ALL").map(String::as_str), Some("C"));
        assert_eq!(map.get("LC_CTYPE").map(String::as_str), Some("en_US.UTF-8"));

        assert!(!map.contains_key("CODEX_HOME"), "CODEX_HOME must not leak");
        assert!(
            !map.contains_key("OPENAI_BASE_URL"),
            "OPENAI_BASE_URL must not leak"
        );
        assert!(
            !map.contains_key("SECRET_TOKEN"),
            "arbitrary parent env vars must not leak"
        );
    }

    #[test]
    fn build_child_env_collapses_windows_home_and_user_aliases() {
        let env = build_child_env_from(env_pairs(&[
            ("USERPROFILE", r"C:\Users\kim"),
            ("USERNAME", "kim"),
            ("TEMP", r"C:\Temp"),
        ]));
        let map: std::collections::HashMap<_, _> = env.into_iter().collect();
        assert_eq!(map.get("HOME").map(String::as_str), Some(r"C:\Users\kim"));
        assert_eq!(map.get("USER").map(String::as_str), Some("kim"));
        assert_eq!(map.get("TMPDIR").map(String::as_str), Some(r"C:\Temp"));
        // USERPROFILE/TEMP are Windows runtime vars: forwarded verbatim
        // under their own native names *in addition to* the HOME/TMPDIR
        // aliases above — matches
        // `_with_windows_env_aliases(..., include_runtime=True)`, which
        // double-maps the same source var.
        assert_eq!(
            map.get("USERPROFILE").map(String::as_str),
            Some(r"C:\Users\kim")
        );
        assert_eq!(map.get("TEMP").map(String::as_str), Some(r"C:\Temp"));
        // USERNAME has no native-name passthrough (it's not in
        // `_WINDOWS_RUNTIME_ENV_KEYS`) — only its HOME/USER alias survives.
        assert!(!map.contains_key("USERNAME"));
    }

    #[test]
    fn build_child_env_native_home_wins_over_userprofile() {
        let env = build_child_env_from(env_pairs(&[
            ("HOME", "/home/kim"),
            ("USERPROFILE", r"C:\Users\kim"),
        ]));
        let map: std::collections::HashMap<_, _> = env.into_iter().collect();
        assert_eq!(map.get("HOME").map(String::as_str), Some("/home/kim"));
    }

    /// FIX 1 (#61 review): a real Windows host commonly cases PATH as
    /// `Path` and SYSTEMROOT as `SystemRoot` (COMSPEC as `ComSpec`). Before
    /// this fix, `build_child_env_from` only matched the exact-uppercase
    /// `PATH` key, so on Windows the child got no PATH at all (combined
    /// with the caller's `Command::env_clear()`), and never carried
    /// SYSTEMROOT/COMSPEC/PATHEXT through, breaking winsock/DNS and
    /// shell-out in the child. This proves the conventionally-cased
    /// fixtures survive with their canonical (upper-case) names.
    #[test]
    fn build_child_env_forwards_windows_runtime_vars_with_conventional_casing() {
        let env = build_child_env_from(env_pairs(&[
            ("Path", r"C:\Windows\System32;C:\Windows"),
            ("SystemRoot", r"C:\Windows"),
            ("ComSpec", r"C:\Windows\System32\cmd.exe"),
            ("USERPROFILE", r"C:\Users\kim"),
            ("TEMP", r"C:\Users\kim\AppData\Local\Temp"),
            ("TMP", r"C:\Users\kim\AppData\Local\Temp"),
            ("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
        ]));
        let map: std::collections::HashMap<_, _> = env.into_iter().collect();

        assert_eq!(
            map.get("PATH").map(String::as_str),
            Some(r"C:\Windows\System32;C:\Windows"),
            "PATH must survive even when the parent env cases it `Path`"
        );
        assert_eq!(
            map.get("SYSTEMROOT").map(String::as_str),
            Some(r"C:\Windows"),
            "SYSTEMROOT must survive even when the parent env cases it `SystemRoot`"
        );
        assert_eq!(
            map.get("COMSPEC").map(String::as_str),
            Some(r"C:\Windows\System32\cmd.exe")
        );
        assert_eq!(
            map.get("USERPROFILE").map(String::as_str),
            Some(r"C:\Users\kim")
        );
        assert_eq!(
            map.get("TEMP").map(String::as_str),
            Some(r"C:\Users\kim\AppData\Local\Temp")
        );
        assert_eq!(
            map.get("TMP").map(String::as_str),
            Some(r"C:\Users\kim\AppData\Local\Temp")
        );
        assert_eq!(
            map.get("PATHEXT").map(String::as_str),
            Some(".COM;.EXE;.BAT;.CMD")
        );
    }
}

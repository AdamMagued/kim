//! The kimcli child's environment allowlist. Split out of the former
//! single-file `commands/tui.rs` — pure relocation, no behavior changes.

/// Build the kimcli child's environment from an explicit `(key, value)`
/// iterator (the real parent env, or a fixture in tests). Mirrors
/// `orchestrator/codex_bridge_service.py`'s `_run_exec_task` minimal env
/// (lines ~772-805): PATH, HOME (falling back to USERPROFILE on Windows),
/// USER (falling back to USERNAME), TMPDIR (falling back to TEMP/TMP), LANG,
/// every `LC_*` locale var, TERM, COLORTERM, TERMINFO. Deliberately does NOT
/// forward CODEX_HOME or OPENAI_BASE_URL — those are set explicitly by the
/// caller per-route (or left unset so kimcli/codex uses its own home).
pub(crate) fn build_child_env_from<I>(parent_env: I) -> Vec<(String, String)>
where
    I: IntoIterator<Item = (String, String)>,
{
    let mut map: std::collections::HashMap<String, String> = parent_env.into_iter().collect();
    let mut out = Vec::new();

    if let Some(home) = map.remove("HOME").or_else(|| map.remove("USERPROFILE")) {
        out.push(("HOME".to_string(), home));
    }
    if let Some(user) = map.remove("USER").or_else(|| map.remove("USERNAME")) {
        out.push(("USER".to_string(), user));
    }
    if let Some(tmp) = map
        .remove("TMPDIR")
        .or_else(|| map.remove("TEMP"))
        .or_else(|| map.remove("TMP"))
    {
        out.push(("TMPDIR".to_string(), tmp));
    }
    for key in ["PATH", "LANG", "TERM", "COLORTERM", "TERMINFO"] {
        if let Some(value) = map.remove(key) {
            out.push((key.to_string(), value));
        }
    }
    let mut lc_keys: Vec<String> = map
        .keys()
        .filter(|k| k.starts_with("LC_"))
        .cloned()
        .collect();
    lc_keys.sort();
    for key in lc_keys {
        if let Some(value) = map.remove(&key) {
            out.push((key, value));
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
        // The Windows-native names themselves are not forwarded verbatim.
        assert!(!map.contains_key("USERPROFILE"));
        assert!(!map.contains_key("USERNAME"));
        assert!(!map.contains_key("TEMP"));
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
}

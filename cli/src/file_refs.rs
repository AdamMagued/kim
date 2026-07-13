use std::path::PathBuf;

pub(crate) fn prompt_with_file_references(input: &str) -> String {
    let file_paths = prompt_file_references(input);
    if file_paths.is_empty() {
        return input.to_string();
    }
    let mut prompt = input.trim().to_string();
    prompt.push_str("\n\nReferenced local files Kim may access:");
    for path in file_paths {
        prompt.push_str("\n- ");
        prompt.push_str(&path.display().to_string());
    }
    prompt.push_str("\n\nUse these file paths directly when reading or inspecting attachments.");
    prompt
}

fn prompt_file_references(input: &str) -> Vec<PathBuf> {
    let mut paths = split_shellish_tokens(input)
        .into_iter()
        .filter_map(|token| normalize_existing_path(&token))
        .collect::<Vec<_>>();
    paths.sort();
    paths.dedup();
    paths
}

fn normalize_existing_path(token: &str) -> Option<PathBuf> {
    let trimmed = token
        .trim()
        .trim_matches(|ch| matches!(ch, '\'' | '"' | '`' | ',' | ';'));
    if trimmed.is_empty() {
        return None;
    }
    // A19: don't match innocent words. Require a path-ish token (a separator, a
    // dot, or ~) and never treat a bare "." / ".." as a reference.
    if trimmed == "." || trimmed == ".." {
        return None;
    }
    let path_ish = trimmed.contains('/')
        || trimmed.contains('\\')
        || trimmed.contains('.')
        || trimmed.starts_with('~');
    if !path_ish {
        return None;
    }
    let expanded = if trimmed == "~" {
        crate::config::kim_home()?
    } else if let Some(rest) = trimmed.strip_prefix("~/") {
        crate::config::kim_home()?.join(rest)
    } else {
        PathBuf::from(trimmed)
    };
    let candidate = if expanded.is_absolute() {
        expanded
    } else {
        std::env::current_dir().ok()?.join(expanded)
    };
    if !candidate.exists() {
        return None;
    }
    let canonical = std::fs::canonicalize(&candidate).ok()?;
    // A19: never reference the current working directory itself.
    if let Ok(cwd) = std::env::current_dir() {
        if std::fs::canonicalize(&cwd).ok().as_deref() == Some(canonical.as_path()) {
            return None;
        }
    }
    Some(canonical)
}

pub(crate) fn split_shellish_tokens(input: &str) -> Vec<String> {
    let mut tokens = Vec::new();
    let mut current = String::new();
    let mut quote = None;
    let mut chars = input.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch == '\\' {
            if chars
                .peek()
                .is_some_and(|next| next.is_whitespace() || matches!(*next, '\'' | '"'))
            {
                current.push(chars.next().expect("peeked character must exist"));
            } else {
                // Preserve Windows path separators (for example C:\\Users).
                current.push(ch);
            }
            continue;
        }
        if quote == Some(ch) {
            quote = None;
            continue;
        }
        if quote.is_none() && matches!(ch, '\'' | '"') {
            quote = Some(ch);
            continue;
        }
        if quote.is_none() && ch.is_whitespace() {
            if !current.is_empty() {
                tokens.push(std::mem::take(&mut current));
            }
            continue;
        }
        current.push(ch);
    }
    if !current.is_empty() {
        tokens.push(current);
    }
    tokens
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

    use super::{prompt_file_references, prompt_with_file_references, split_shellish_tokens};

    // ── A19: file-reference detector ────────────────────────────────────────

    #[test]
    fn file_refs_ignore_bare_dot_and_plain_words() {
        assert!(prompt_file_references("what is .").is_empty());
        assert!(prompt_file_references("tell me about cargo").is_empty());
    }

    #[test]
    fn file_refs_match_pathish_existing_file() {
        let path = std::env::temp_dir().join(format!(
            "kim-ref-{}.txt",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::write(&path, "x").unwrap();
        // Use forward slashes — split_shellish_tokens treats `\` as an escape, so
        // a Windows backslash path would be mangled (a real cross-platform gap in
        // the tokenizer, separate from A19's matching gate).
        let token = path.to_string_lossy().replace('\\', "/");
        let refs = prompt_file_references(&format!("inspect {token}"));
        let _ = fs::remove_file(&path);
        assert!(refs
            .iter()
            .any(|p| p.to_string_lossy().contains("kim-ref-")));
    }

    #[test]
    fn splits_dragged_paths_with_escaped_spaces() {
        let tokens = split_shellish_tokens(r#"please inspect /tmp/my\ file.png "and this.txt""#);
        assert_eq!(
            tokens,
            vec!["please", "inspect", "/tmp/my file.png", "and this.txt"]
        );
    }

    #[test]
    fn prompt_adds_existing_file_references() {
        let path = std::env::temp_dir().join(format!(
            "kim-cli-attach-{}.txt",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("clock should be after epoch")
                .as_nanos()
        ));
        fs::write(&path, "hello").expect("fixture should write");
        let prompt = prompt_with_file_references(&format!("read {}", path.display()));
        let _ = fs::remove_file(&path);
        assert!(prompt.contains("Referenced local files Kim may access:"));
        assert!(prompt.contains("kim-cli-attach-"));
    }

    #[test]
    fn ignores_missing_file_references() {
        let paths = prompt_file_references("/definitely/not/a/kim/file.png");
        assert!(paths.is_empty());
    }
}

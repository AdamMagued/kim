//! P7: tiny terminal Markdown renderer (no dependency — full control over ANSI,
//! keeps the binary lean). Handles headings, **bold**, `inline code`, and fenced
//! code blocks with a dim left border.

const RESET: &str = "\x1b[0m";
const BOLD: &str = "\x1b[1m";
const DIM: &str = "\x1b[2m";
const REVERSE: &str = "\x1b[7m";

/// Render Markdown to an ANSI-decorated string for the terminal.
///
/// #8: both call sites (agentic.rs, bridge.rs) pass the WHOLE final answer
/// text in one call — never a partial streaming chunk — so a fence line only
/// gets to toggle code-mode rendering when it has a matching partner
/// somewhere else in this same `input`. An odd, unterminated trailing fence
/// (a cut-off answer, or a stray ``` typed as normal text) previously
/// flipped `in_code` on for good, recoloring every line after it — including
/// content that was never inside a code block — with no closing border ever
/// appearing. Pre-scan fence line indices and drop a trailing unpaired one
/// from the toggle set so it renders as a literal line instead.
pub fn render_markdown(input: &str) -> String {
    let lines: Vec<&str> = input.split('\n').collect();
    let mut fence_lines: Vec<usize> = lines
        .iter()
        .enumerate()
        .filter(|(_, line)| line.trim_start().starts_with("```"))
        .map(|(index, _)| index)
        .collect();
    if fence_lines.len() % 2 == 1 {
        // Unbalanced: the last fence in the text never closes. Don't let it
        // toggle code-mode at all — treat it as ordinary text instead.
        fence_lines.pop();
    }
    let toggles: std::collections::HashSet<usize> = fence_lines.into_iter().collect();

    let mut out: Vec<String> = Vec::new();
    let mut in_code = false;
    for (index, line) in lines.iter().enumerate() {
        if toggles.contains(&index) {
            // Toggle fenced code block.
            in_code = !in_code;
            // Render the fence itself as a faint rule, dropping the language tag.
            out.push(format!("{DIM}┄┄┄{RESET}"));
            continue;
        }
        if in_code {
            // Dim left border + verbatim content (no inline parsing inside code).
            out.push(format!("{DIM}│{RESET} {line}"));
            continue;
        }
        // Headings: #, ##, ### → bold (strip the leading hashes).
        let trimmed = line.trim_start();
        if let Some(rest) = heading_text(trimmed) {
            out.push(format!("{BOLD}{}{RESET}", render_inline(rest)));
            continue;
        }
        out.push(render_inline(line));
    }
    out.join("\n")
}

/// If `line` is an ATX heading (`#`..`######` then a space), return the text.
fn heading_text(line: &str) -> Option<&str> {
    let hashes = line.chars().take_while(|c| *c == '#').count();
    if (1..=6).contains(&hashes) && line[hashes..].starts_with(' ') {
        Some(line[hashes..].trim_start())
    } else {
        None
    }
}

/// Render inline spans: **bold** and `code`. Single-pass, left to right.
pub fn render_inline(line: &str) -> String {
    let bytes = line.as_bytes();
    let mut out = String::with_capacity(line.len());
    let mut i = 0;
    while i < bytes.len() {
        if line[i..].starts_with("**") {
            if let Some(end) = line[i + 2..].find("**") {
                let inner = &line[i + 2..i + 2 + end];
                out.push_str(BOLD);
                out.push_str(inner);
                out.push_str(RESET);
                i = i + 2 + end + 2;
                continue;
            }
        }
        if bytes[i] == b'`' {
            if let Some(end) = line[i + 1..].find('`') {
                let inner = &line[i + 1..i + 1 + end];
                out.push_str(REVERSE);
                out.push(' ');
                out.push_str(inner);
                out.push(' ');
                out.push_str(RESET);
                i = i + 1 + end + 1;
                continue;
            }
        }
        // Default: copy one UTF-8 char.
        let ch_len = utf8_len(bytes[i]);
        out.push_str(&line[i..i + ch_len]);
        i += ch_len;
    }
    out
}

fn utf8_len(first: u8) -> usize {
    match first {
        b if b < 0x80 => 1,
        b if b >> 5 == 0b110 => 2,
        b if b >> 4 == 0b1110 => 3,
        _ => 4,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn heading_becomes_bold() {
        let out = render_markdown("# Title");
        assert!(out.contains(BOLD));
        assert!(out.contains("Title"));
        assert!(!out.contains("# Title")); // hashes stripped
    }

    #[test]
    fn inline_bold_and_code() {
        let out = render_inline("a **b** `c`");
        assert!(out.contains(&format!("{BOLD}b{RESET}")));
        assert!(out.contains(REVERSE));
        assert!(out.contains("c"));
    }

    #[test]
    fn fenced_code_block_gets_border() {
        let md = "```rust\nlet x = 1;\n```";
        let out = render_markdown(md);
        assert!(out.contains("│")); // border line
        assert!(out.contains("let x = 1;"));
        // No inline parsing inside code: backticks-free content stays verbatim.
    }

    // ── #8: unbalanced code fence must not recolor the rest of the message ──

    #[test]
    fn unterminated_fence_does_not_recolor_trailing_lines() {
        // Only ONE fence — it never closes (e.g. a cut-off streamed answer).
        let md = "before\n```rust\nlet x = 1;\nafter line one\nafter line two";
        let out = render_markdown(md);
        // The unmatched fence must not have entered code mode: lines after
        // it must render as plain text, not with the dim code-border prefix.
        assert!(
            !out.contains(&format!("{DIM}│{RESET} after line one")),
            "text after an unbalanced fence must not be rendered as code: {out}"
        );
        assert!(
            !out.contains(&format!("{DIM}│{RESET} after line two")),
            "text after an unbalanced fence must not be rendered as code: {out}"
        );
        assert!(out.contains("after line one"));
        assert!(out.contains("after line two"));
    }

    #[test]
    fn balanced_fences_still_render_as_code() {
        let md = "before\n```\ncode line\n```\nafter";
        let out = render_markdown(md);
        assert!(
            out.contains(&format!("{DIM}│{RESET} code line")),
            "a properly closed fence must still render its content as code: {out}"
        );
        assert!(out.contains("after"));
    }

    #[test]
    fn two_separate_balanced_fence_blocks_both_render_as_code() {
        let md = "```\nfirst\n```\nplain text between\n```\nsecond\n```";
        let out = render_markdown(md);
        assert!(out.contains(&format!("{DIM}│{RESET} first")));
        assert!(out.contains(&format!("{DIM}│{RESET} second")));
        assert!(
            !out.contains(&format!("{DIM}│{RESET} plain text between")),
            "text between two closed fence pairs must not be treated as code: {out}"
        );
    }

    #[test]
    fn unterminated_bold_is_literal() {
        let out = render_inline("**oops");
        assert_eq!(out, "**oops");
    }

    #[test]
    fn plain_text_unchanged_except_copy() {
        assert_eq!(render_inline("hello world"), "hello world");
    }
}

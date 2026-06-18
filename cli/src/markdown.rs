//! P7: tiny terminal Markdown renderer (no dependency — full control over ANSI,
//! keeps the binary lean). Handles headings, **bold**, `inline code`, and fenced
//! code blocks with a dim left border.

const RESET: &str = "\x1b[0m";
const BOLD: &str = "\x1b[1m";
const DIM: &str = "\x1b[2m";
const REVERSE: &str = "\x1b[7m";

/// Render Markdown to an ANSI-decorated string for the terminal.
pub fn render_markdown(input: &str) -> String {
    let mut out: Vec<String> = Vec::new();
    let mut in_code = false;
    for raw in input.split('\n') {
        let line = raw;
        if line.trim_start().starts_with("```") {
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

use std::time::Duration;

use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, List, ListItem, ListState, Paragraph};
use ratatui::Frame;

use crate::provider::PROVIDERS;
use crate::theme::Theme;
use crate::{thinking, App, AppMode, MessageRole, ViewState};

pub fn draw(frame: &mut Frame<'_>, app: &App) {
    let theme = Theme::for_name(app.config.theme);
    let area = frame.area();
    frame.render_widget(Clear, area);

    // In compact inline mode (e.g. sync_inline_scrollback), the viewport is shrunk
    // to 3 rows. We only have space to draw the input box.
    if area.height <= 3 {
        draw_input(frame, app, area, theme);
        return;
    }

    let root = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(3),
            Constraint::Min(8),
            Constraint::Length(3),
            Constraint::Length(1),
        ])
        .split(area);

    draw_header(frame, app, root[0], theme);
    draw_body(frame, app, root[1], theme);
    draw_input(frame, app, root[2], theme);
    draw_slash_palette(frame, app, root[1], theme);
    draw_model_picker(frame, app, root[1], theme);
    draw_provider_picker(frame, app, root[1], theme);
    draw_status(frame, app, root[3], theme);
}

fn draw_header(frame: &mut Frame<'_>, app: &App, area: Rect, theme: Theme) {
    let title = Line::from(vec![
        Span::styled(
            " kim ",
            Style::default()
                .fg(theme.accent)
                .add_modifier(Modifier::BOLD),
        ),
        Span::styled("terminal", Style::default().fg(theme.text_dim)),
        Span::styled(" v1.192", Style::default().fg(theme.text_dimmer)),
        Span::raw("  "),
        Span::styled(
            format!("[{}]", app.mode.label()),
            Style::default()
                .fg(theme.accent_ink)
                .bg(theme.accent)
                .add_modifier(Modifier::BOLD),
        ),
        Span::raw("  "),
        Span::styled(
            app.config.provider.as_str(),
            Style::default()
                .fg(if app.provider_ready { theme.success } else { theme.danger })
                .add_modifier(Modifier::BOLD),
        ),
        Span::styled(
            if app.provider_ready { " ✓" } else { " ✗" },
            Style::default().fg(if app.provider_ready { theme.success } else { theme.danger }),
        ),
        Span::styled(" / ", Style::default().fg(theme.text_dimmer)),
        Span::styled(
            app.config.model.as_str(),
            Style::default().fg(theme.text_dim),
        ),
        Span::raw("  "),
        Span::styled(
            if app.bridge_connected {
                "[bridge]"
            } else {
                "[direct]"
            },
            Style::default().fg(if app.bridge_connected {
                theme.success
            } else {
                theme.text_dimmer
            }),
        ),
    ]);
    let paragraph = Paragraph::new(title).block(
        Block::default()
            .borders(Borders::ALL)
            .border_style(Style::default().fg(theme.border))
            .style(Style::default().bg(theme.panel)),
    );
    frame.render_widget(paragraph, area);
}

fn draw_body(frame: &mut Frame<'_>, app: &App, area: Rect, theme: Theme) {
    if app.view == ViewState::SessionMenu {
        draw_session_browser(frame, app, area, theme);
        return;
    }
    if app.busy && !app.trace.is_empty() {
        let elapsed = app.thinking_start.map_or(Duration::ZERO, |t| t.elapsed());
        // Only show the thinking panel when the terminal is tall enough that it
        // doesn't crowd the chat area.  Keep a minimum of 6 chat lines visible.
        if area.height >= 16 {
            // Fixed 8-row thinking panel — no dynamic resize so the chat area
            // doesn't jump in size every time a new trace item appears.
            let panel_h: u16 = 8;
            let chat_h = area.height.saturating_sub(panel_h);
            if chat_h >= 6 {
                let chunks = Layout::default()
                    .direction(Direction::Vertical)
                    .constraints([Constraint::Length(chat_h), Constraint::Length(panel_h)])
                    .split(area);
                draw_chat(frame, app, chunks[0], theme);
                thinking::draw_thinking_panel(frame, chunks[1], &app.trace, elapsed, theme);
                return;
            }
        }
    }
    draw_chat(frame, app, area, theme);
}

fn draw_session_browser(frame: &mut Frame<'_>, app: &App, area: Rect, theme: Theme) {
    let mut items = Vec::new();
    let new_label = match app.mode {
        AppMode::Chat => "New Kim chat",
        AppMode::Code => "New code chat in this folder",
    };
    items.push(session_row(new_label, "start fresh", app.selected_session == 0, theme));
    for (index, session) in app.sessions.iter().take(40).enumerate() {
        items.push(session_row(
            &session.label,
            &session.preview,
            app.selected_session == index + 1,
            theme,
        ));
    }
    if app.sessions.is_empty() {
        items.push(ListItem::new(Line::styled(
            if app.mode == AppMode::Code {
                "No saved code conversations in this folder yet."
            } else {
                "No saved Kim chats found yet."
            },
            Style::default().fg(theme.text_dimmer),
        )));
    }

    // Mode indicator in title: active mode is accented, inactive is dimmed.
    let (chat_style, code_style) = if app.mode == AppMode::Chat {
        (
            Style::default().fg(theme.accent).add_modifier(Modifier::BOLD),
            Style::default().fg(theme.text_dimmer),
        )
    } else {
        (
            Style::default().fg(theme.text_dimmer),
            Style::default().fg(theme.accent).add_modifier(Modifier::BOLD),
        )
    };
    let title = Line::from(vec![
        Span::raw(" "),
        Span::styled("Chat", chat_style),
        Span::styled("  /  ", Style::default().fg(theme.text_dimmer)),
        Span::styled("Code", code_style),
        Span::styled("  ·  Tab to switch  ", Style::default().fg(theme.text_dimmer)),
    ]);

    let list = List::new(items).block(
        Block::default()
            .title(title)
            .borders(Borders::ALL)
            .border_style(Style::default().fg(theme.border))
            .style(Style::default().bg(theme.bg)),
    );
    let mut list_state = ListState::default();
    list_state.select(Some(app.selected_session));
    frame.render_stateful_widget(list, area, &mut list_state);
}

fn session_row<'a>(title: &'a str, preview: &'a str, selected: bool, theme: Theme) -> ListItem<'a> {
    let style = if selected {
        Style::default()
            .fg(theme.accent_ink)
            .bg(theme.accent)
            .add_modifier(Modifier::BOLD)
    } else {
        Style::default().fg(theme.text)
    };
    let marker = if selected { "›" } else { " " };
    ListItem::new(vec![
        Line::styled(format!("{marker} {title}"), style),
        Line::styled(
            format!("  {preview}"),
            Style::default().fg(theme.text_dimmer),
        ),
    ])
}

/// Build a flat list of pre-wrapped plain-text rows for the chat area.
///
/// Each element corresponds to exactly one terminal row at the given `total_width`
/// (which should be the full widget width; the inner text width is derived by
/// subtracting the 2 border columns internally).
///
/// This function is `pub` so that `main.rs` can call it from
/// `sync_inline_scrollback` to calculate how many rows overflow the viewport
/// and need to be committed to the terminal scrollback buffer.
pub fn chat_visual_rows(app: &App, total_width: u16) -> Vec<String> {
    let inner_width = total_width.saturating_sub(2) as usize;
    build_chat_rows(app, inner_width, false)
}

/// Core pre-wrap engine shared by both the styled draw path and the plain-text
/// scrollback measurement path.
///
/// * `inner_width` — usable columns inside the border (widget width - 2).
/// * `include_busy_spinner` — when true, appends the " generating…" row while
///   `app.busy` is true.
fn build_chat_rows(app: &App, inner_width: usize, include_busy_spinner: bool) -> Vec<String> {
    let mut rows: Vec<String> = Vec::new();

    for message in app.visible_messages() {
        let label = match message.role {
            MessageRole::User      => "you",
            MessageRole::Assistant => "kim",
            MessageRole::System    => "note",
            MessageRole::Error     => "error",
            MessageRole::Reasoning => "think",
        };

        // Skip empty assistant placeholder while streaming.
        if message.role == MessageRole::Assistant && message.content.trim().is_empty() && app.busy {
            continue;
        }

        let label_str  = format!("{label} ");
        let label_w    = label_str.chars().count();
        let indent_str = " ".repeat(label_w);
        let first_text_w = if inner_width > label_w { inner_width - label_w } else { 1 };
        let cont_text_w  = if inner_width > label_w  { inner_width - label_w } else { 1 };

        let cleaned = clean_for_display(&message.content);
        let mut is_first_line_of_message = true;

        for text_line in cleaned.lines() {
            let avail = if is_first_line_of_message { first_text_w } else { cont_text_w };
            let mut remaining = text_line;
            let mut first_chunk = true;
            loop {
                if remaining.is_empty() && !first_chunk { break; }
                let take = if inner_width == 0 { remaining.len() }
                           else { remaining.chars().count().min(avail) };
                let byte_end = remaining
                    .char_indices()
                    .nth(take)
                    .map_or(remaining.len(), |(i, _)| i);
                let chunk = &remaining[..byte_end];
                remaining  = &remaining[byte_end..];

                if is_first_line_of_message && first_chunk {
                    rows.push(format!("{label_str}{chunk}"));
                } else {
                    rows.push(format!("{indent_str}{chunk}"));
                }
                first_chunk = false;
                if remaining.is_empty() { break; }
                is_first_line_of_message = false;
            }
            is_first_line_of_message = false;
        }

        if cleaned.is_empty() || cleaned.lines().next().is_none() {
            rows.push(label_str.clone());
        }
        rows.push(String::new()); // blank separator
    }

    if app.messages.is_empty() {
        let hint = if app.view == ViewState::SessionMenu {
            "Message box accepts slash commands here. Open New chat before sending prompts."
        } else if app.mode == AppMode::Code {
            "Kim Code · coding agent mode."
        } else {
            "Kim Chat · type a message, or /help."
        };
        rows.push(hint.to_string());
    }

    if include_busy_spinner && app.busy {
        rows.push("  generating…".to_string());
    }

    rows
}

fn draw_chat(frame: &mut Frame<'_>, app: &App, area: Rect, theme: Theme) {
    let visible_height = area.height.saturating_sub(2);
    let inner_width    = area.width.saturating_sub(2) as usize;

    // Use the shared pre-wrap engine to get exact row strings, then convert to
    // styled Lines.  One row = one terminal line = exact scroll math.
    let plain_rows = build_chat_rows(app, inner_width, true);

    // Re-build styled lines from the same data so we keep colour.
    let mut lines: Vec<Line<'static>> = Vec::new();
    for message in app.visible_messages() {
        let (label, color) = match message.role {
            MessageRole::User      => ("you",   theme.accent),
            MessageRole::Assistant => ("kim",   theme.text),
            MessageRole::System    => ("note",  theme.text_dim),
            MessageRole::Error     => ("error", theme.danger),
            MessageRole::Reasoning => ("think", theme.text_dim),
        };

        if message.role == MessageRole::Assistant && message.content.trim().is_empty() && app.busy {
            continue;
        }

        let label_str  = format!("{label} ");
        let label_w    = label_str.chars().count();
        let indent_str = " ".repeat(label_w);
        let first_text_w = if inner_width > label_w { inner_width - label_w } else { 1 };
        let cont_text_w  = if inner_width > label_w  { inner_width - label_w } else { 1 };

        let cleaned = clean_for_display(&message.content);
        let mut is_first_line_of_message = true;

        for text_line in cleaned.lines() {
            let avail = if is_first_line_of_message { first_text_w } else { cont_text_w };
            let mut remaining = text_line;
            let mut first_chunk = true;
            loop {
                if remaining.is_empty() && !first_chunk { break; }
                let take = if inner_width == 0 { remaining.len() }
                           else { remaining.chars().count().min(avail) };
                let byte_end = remaining
                    .char_indices()
                    .nth(take)
                    .map_or(remaining.len(), |(i, _)| i);
                let chunk = &remaining[..byte_end];
                remaining  = &remaining[byte_end..];

                if is_first_line_of_message && first_chunk {
                    lines.push(Line::from(vec![
                        Span::styled(label_str.clone(),
                            Style::default().fg(color).add_modifier(Modifier::BOLD)),
                        Span::styled(chunk.to_string(),
                            Style::default().fg(theme.text)),
                    ]));
                } else {
                    lines.push(Line::styled(
                        format!("{indent_str}{chunk}"),
                        Style::default().fg(theme.text),
                    ));
                }
                first_chunk = false;
                if remaining.is_empty() { break; }
                is_first_line_of_message = false;
            }
            is_first_line_of_message = false;
        }

        if cleaned.is_empty() || cleaned.lines().next().is_none() {
            lines.push(Line::from(vec![Span::styled(
                label_str.clone(),
                Style::default().fg(color).add_modifier(Modifier::BOLD),
            )]));
        }
        lines.push(Line::raw(""));
    }

    if app.messages.is_empty() {
        let hint = if app.view == ViewState::SessionMenu {
            "Message box accepts slash commands here. Open New chat before sending prompts."
        } else if app.mode == AppMode::Code {
            "Kim Code · coding agent mode. Ask about code, bugs, refactors, or drop a file path."
        } else {
            "Kim Chat · type a message, or /help. Esc returns to the chat list."
        };
        lines.push(Line::styled(hint.to_string(), Style::default().fg(theme.text_dim)));
    }

    if app.busy {
        let elapsed = app.thinking_start.map_or(Duration::ZERO, |t| t.elapsed());
        lines.push(Line::from(vec![
            thinking::spinner_span(elapsed, thinking::SpinnerStyle::Braille, theme.accent),
            Span::styled(
                "  generating…".to_string(),
                Style::default().fg(theme.accent),
            ),
        ]));
    }

    // Exact scroll math: plain_rows.len() == number of terminal rows rendered.
    let total_lines  = plain_rows.len() as u16;
    let max_scroll   = total_lines.saturating_sub(visible_height);
    app.last_max_scroll.set(max_scroll);

    let effective_scroll = if app.follow {
        max_scroll
    } else {
        app.scroll.min(max_scroll)
    };

    let scrolled_up = !app.follow && effective_scroll < max_scroll;
    let chat_title  = if app.view == ViewState::SessionMenu {
        " Chat List ".to_string()
    } else if scrolled_up {
        " Chat  ↓ scroll to latest ".to_string()
    } else {
        " Chat ".to_string()
    };

    let chat = Paragraph::new(lines)
        .scroll((effective_scroll, 0))
        .block(
            Block::default()
                .title(chat_title)
                .borders(Borders::ALL)
                .border_style(Style::default().fg(theme.border))
                .style(Style::default().bg(theme.bg)),
        );
    frame.render_widget(chat, area);
}

fn draw_model_picker(frame: &mut Frame<'_>, app: &App, body_area: Rect, theme: Theme) {
    if !app.model_picker_open {
        return;
    }
    let width = 44.min(body_area.width.saturating_sub(4));
    let height = u16::try_from(app.model_options.len())
        .unwrap_or(u16::MAX)
        .saturating_add(3)
        .min(14);
    if width < 24 || height < 4 {
        return;
    }
    let area = Rect {
        x: body_area.x + (body_area.width.saturating_sub(width)) / 2,
        y: body_area.y + (body_area.height.saturating_sub(height)) / 2,
        width,
        height,
    };
    frame.render_widget(Clear, area);
    let selected_idx = app
        .selected_model
        .min(app.model_options.len().saturating_sub(1));
    let items = app
        .model_options
        .iter()
        .enumerate()
        .map(|(index, model)| {
            let selected = index == selected_idx;
            let style = if selected {
                Style::default()
                    .fg(theme.accent_ink)
                    .bg(theme.accent)
                    .add_modifier(Modifier::BOLD)
            } else if model == &app.config.model {
                Style::default().fg(theme.success)
            } else {
                Style::default().fg(theme.text)
            };
            let marker = if selected { "› " } else { "  " };
            ListItem::new(Line::styled(format!("{marker}{model}"), style))
        })
        .collect::<Vec<_>>();
    let list = List::new(items).block(
        Block::default()
            .title(" choose model ")
            .borders(Borders::ALL)
            .border_style(Style::default().fg(theme.border))
            .style(Style::default().bg(theme.panel)),
    );
    let mut list_state = ListState::default();
    list_state.select(Some(selected_idx));
    frame.render_stateful_widget(list, area, &mut list_state);
}

fn draw_input(frame: &mut Frame<'_>, app: &App, area: Rect, theme: Theme) {
    let (title, border_color) = if app.secure_input {
        let t = format!(" {} API key · Enter to submit · Esc to cancel ", app.secure_input_provider);
        (t, theme.accent)
    } else if app.view == ViewState::SessionMenu {
        (" / command ".to_string(), theme.border)
    } else {
        (" Message ".to_string(), theme.border)
    };

    // Compute the inner width available for text (minus 2 for the left/right borders).
    let inner_w = area.width.saturating_sub(2) as usize;

    // Build what we actually render: show only the tail of the input that fits
    // inside the box, so the cursor is always visible at the right edge.
    let display_text: String = if app.secure_input {
        let masked: String = "•".repeat(app.input.chars().count());
        if inner_w == 0 || masked.chars().count() <= inner_w {
            masked
        } else {
            masked.chars().rev().take(inner_w).collect::<String>().chars().rev().collect()
        }
    } else {
        let chars: Vec<char> = app.input.chars().collect();
        if inner_w == 0 || chars.len() <= inner_w {
            app.input.clone()
        } else {
            // Show the rightmost `inner_w` chars so the cursor stays visible.
            chars[chars.len() - inner_w..].iter().collect()
        }
    };

    let input = Paragraph::new(display_text.as_str())
        .style(Style::default().fg(theme.text).bg(theme.panel_alt))
        .block(
            Block::default()
                .title(title)
                .borders(Borders::ALL)
                .border_style(Style::default().fg(border_color)),
        );
    frame.render_widget(input, area);

    // Place cursor at the end of the visible display text.
    let visible_len = display_text.chars().count() as u16;
    let cursor_x = area.x.saturating_add(1).saturating_add(visible_len);
    let cursor_y = area.y.saturating_add(1);
    frame.set_cursor_position((cursor_x.min(area.right().saturating_sub(2)), cursor_y));
}

fn draw_provider_picker(frame: &mut Frame<'_>, app: &App, body_area: Rect, theme: Theme) {
    if !app.provider_picker_open {
        return;
    }
    let width = 30.min(body_area.width.saturating_sub(4));
    let height = (PROVIDERS.len() as u16).saturating_add(2).min(12);
    if width < 18 || height < 4 {
        return;
    }
    let area = Rect {
        x: body_area.x + (body_area.width.saturating_sub(width)) / 2,
        y: body_area.y + (body_area.height.saturating_sub(height)) / 2,
        width,
        height,
    };
    frame.render_widget(Clear, area);
    let selected_idx = app.selected_provider.min(PROVIDERS.len().saturating_sub(1));
    let items = PROVIDERS
        .iter()
        .enumerate()
        .map(|(index, p)| {
            let is_cursor = index == selected_idx;
            let is_active = p.name == app.config.provider;
            let style = if is_cursor {
                Style::default()
                    .fg(theme.accent_ink)
                    .bg(theme.accent)
                    .add_modifier(Modifier::BOLD)
            } else if is_active {
                Style::default().fg(theme.success)
            } else {
                Style::default().fg(theme.text)
            };
            let marker = if is_cursor { "› " } else if is_active { "✓ " } else { "  " };
            ListItem::new(Line::styled(format!("{marker}{}", p.name), style))
        })
        .collect::<Vec<_>>();
    let list = List::new(items).block(
        Block::default()
            .title(" choose provider ")
            .borders(Borders::ALL)
            .border_style(Style::default().fg(theme.border))
            .style(Style::default().bg(theme.panel)),
    );
    let mut list_state = ListState::default();
    list_state.select(Some(selected_idx));
    frame.render_stateful_widget(list, area, &mut list_state);
}

fn draw_slash_palette(frame: &mut Frame<'_>, app: &App, body_area: Rect, theme: Theme) {
    let matches = app.slash_matches();
    if matches.is_empty() {
        return;
    }
    let width = 34.min(body_area.width.saturating_sub(4));
    let height = u16::try_from(matches.len())
        .unwrap_or(u16::MAX)
        .saturating_add(2)
        .min(10);
    if width < 18 || height < 3 {
        return;
    }
    let area = Rect {
        x: body_area.right().saturating_sub(width + 2),
        y: body_area.bottom().saturating_sub(height + 1),
        width,
        height,
    };
    frame.render_widget(Clear, area);
    let selected_idx = app.slash_selected.min(matches.len().saturating_sub(1));
    let items = matches
        .iter()
        .enumerate()
        .map(|(index, command)| {
            let selected = index == selected_idx;
            let style = if selected {
                Style::default()
                    .fg(theme.accent_ink)
                    .bg(theme.accent)
                    .add_modifier(Modifier::BOLD)
            } else {
                Style::default().fg(theme.text)
            };
            let marker = if selected { "› " } else { "  " };
            ListItem::new(Line::styled(format!("{marker}{command}"), style))
        })
        .collect::<Vec<_>>();
    let list = List::new(items).block(
        Block::default()
            .title(" slash commands ")
            .borders(Borders::ALL)
            .border_style(Style::default().fg(theme.border))
            .style(Style::default().bg(theme.panel)),
    );
    let mut list_state = ListState::default();
    list_state.select(Some(selected_idx));
    frame.render_stateful_widget(list, area, &mut list_state);
}

fn clean_for_display(text: &str) -> String {
    let mut output: Vec<String> = Vec::new();
    let mut in_code_block = false;
    for line in text.lines() {
        let trimmed_start = line.trim_start();
        if trimmed_start.starts_with("```") {
            in_code_block = !in_code_block;
            // Show the language tag if present, e.g. "  ── rust"
            let lang = trimmed_start.trim_start_matches('`').trim();
            if lang.is_empty() {
                output.push("  ─────".to_string());
            } else {
                output.push(format!("  ── {lang}"));
            }
            continue;
        }
        if in_code_block {
            // Preserve code lines exactly — no markdown stripping.
            output.push(format!("  {line}"));
            continue;
        }
        // Only strip `#` when it looks like a markdown header (# followed by space).
        // This avoids breaking #include, #!/usr/bin/env, etc.
        let line = if trimmed_start.starts_with("# ") || trimmed_start.starts_with("## ") ||
                      trimmed_start.starts_with("### ") || trimmed_start == "#" {
            trimmed_start.trim_start_matches('#').trim_start()
        } else {
            trimmed_start
        };
        // Strip `> ` blockquote prefix.
        let line = line.strip_prefix("> ").unwrap_or(line);
        output.push(strip_inline_md(line));
    }
    output.join("\n")
}

fn strip_inline_md(text: &str) -> String {
    // Only strip paired markdown emphasis/code markers.
    // A marker char is only removed when it appears in a balanced pair that
    // wraps non-whitespace content: **bold**, *em*, `code`, ***both***.
    // Unpaired `*` (e.g. "2 * 3", "C* libs") and unpaired backticks are kept.
    let mut result = String::with_capacity(text.len());
    let chars: Vec<char> = text.chars().collect();
    let len = chars.len();
    let mut i = 0;
    while i < len {
        let ch = chars[i];
        if ch == '*' || ch == '_' || ch == '`' {
            // Count the run of this marker.
            let start = i;
            while i < len && chars[i] == ch {
                i += 1;
            }
            let marker_len = i - start;
            // Search for a matching closing run of the same length.
            let mut j = i;
            let mut found_close = false;
            while j + marker_len <= len {
                // Find next occurrence of ch.
                if chars[j] == ch {
                    // Check if run length matches.
                    let run_start = j;
                    let mut k = j;
                    while k < len && chars[k] == ch {
                        k += 1;
                    }
                    if k - run_start == marker_len {
                        // Balanced pair found.  Emit the inner content stripped.
                        let inner: String = chars[i..run_start].iter().collect();
                        // Recursively strip nested markers in the inner text.
                        result.push_str(&strip_inline_md(&inner));
                        i = k;
                        found_close = true;
                        break;
                    } else {
                        j = k;
                    }
                } else {
                    j += 1;
                }
            }
            if !found_close {
                // No balanced closing marker — emit the marker chars as-is.
                for _ in 0..marker_len {
                    result.push(ch);
                }
                // i is already past the opening run; do NOT re-consume.
            }
        } else {
            result.push(ch);
            i += 1;
        }
    }
    result
}

fn draw_status(frame: &mut Frame<'_>, app: &App, area: Rect, theme: Theme) {
    let current_status = if app.busy {
        let elapsed = app.thinking_start.map_or(Duration::ZERO, |t| t.elapsed());
        format!("{} ({})", app.status, format_elapsed(elapsed))
    } else {
        app.status.clone()
    };
    // Keep the hint short so it fits on narrow terminals without wrapping.
    let hint = if app.view == ViewState::SessionMenu {
        "Tab: Chat/Code  Enter: open  /login /help"
    } else {
        "Esc: list  Ctrl-C ×2: exit  /help"
    };
    let status = format!("  {}  ·  {}", hint, current_status);
    frame.render_widget(
        Paragraph::new(status).style(Style::default().fg(theme.text_dim).bg(theme.status)),
        area,
    );
}

fn format_elapsed(duration: Duration) -> String {
    let secs = duration.as_secs();
    if secs < 60 {
        format!("{secs}s")
    } else {
        format!("{}m {:02}s", secs / 60, secs % 60)
    }
}

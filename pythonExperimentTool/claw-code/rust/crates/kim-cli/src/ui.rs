use std::time::Duration;

use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, List, ListItem, ListState, Paragraph, Wrap};
use ratatui::Frame;

use crate::theme::Theme;
use crate::{thinking, App, AppMode, MessageRole, ViewState};

pub fn draw(frame: &mut Frame<'_>, app: &App) {
    let theme = Theme::for_name(app.config.theme);
    let area = frame.area();
    frame.render_widget(Clear, area);
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
            Style::default().fg(theme.text),
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
    if app.busy {
        let elapsed = app.thinking_start.map_or(Duration::ZERO, |t| t.elapsed());
        // Dock thinking panel at the bottom so streaming text is always visible above.
        // It needs enough room for border + padding + header/body/keybar; below that it
        // looks broken, so fall back to the inline spinner.
        if area.height >= 12 {
            let trace_rows = (app.trace.len() as u16).min(6);
            let max_panel_h = (area.height / 2).max(8).min(area.height.saturating_sub(4));
            let panel_h = (trace_rows + 7).clamp(8, max_panel_h);
            let chunks = Layout::default()
                .direction(Direction::Vertical)
                .constraints([Constraint::Min(4), Constraint::Length(panel_h)])
                .split(area);
            draw_chat(frame, app, chunks[0], theme);
            thinking::draw_thinking_panel(frame, chunks[1], &app.trace, elapsed, theme);
            return;
        }
    }
    draw_chat(frame, app, area, theme);
}

#[allow(clippy::too_many_lines)]
fn draw_session_browser(frame: &mut Frame<'_>, app: &App, area: Rect, theme: Theme) {
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(5), Constraint::Min(8)])
        .split(area);
    let mode_line = Line::from(vec![
        Span::styled(
            if app.mode == AppMode::Chat {
                "  Chat  "
            } else {
                "  chat  "
            },
            if app.mode == AppMode::Chat {
                Style::default()
                    .fg(theme.accent_ink)
                    .bg(theme.accent)
                    .add_modifier(Modifier::BOLD)
            } else {
                Style::default().fg(theme.text_dim)
            },
        ),
        Span::raw("  "),
        Span::styled(
            if app.mode == AppMode::Code {
                "  Code  "
            } else {
                "  code  "
            },
            if app.mode == AppMode::Code {
                Style::default()
                    .fg(theme.accent_ink)
                    .bg(theme.accent)
                    .add_modifier(Modifier::BOLD)
            } else {
                Style::default().fg(theme.text_dim)
            },
        ),
        Span::raw("  "),
        Span::styled(
            "Press Tab to switch Chat/Code · ↑/↓ selects · Enter opens · type /commands below",
            Style::default().fg(theme.text_dimmer),
        ),
    ]);
    let scope = if app.mode == AppMode::Code {
        "Code shows conversations for this folder/project. New code chat starts here."
    } else {
        "Chat shows your Kim conversations. New Kim chat starts a general assistant thread."
    };
    frame.render_widget(
        Paragraph::new(vec![
            mode_line,
            Line::raw(""),
            Line::styled(scope, Style::default().fg(theme.text_dim)),
        ])
        .block(
            Block::default()
                .title(" Choose Mode ")
                .borders(Borders::ALL)
                .border_style(Style::default().fg(theme.border))
                .style(Style::default().bg(theme.panel)),
        ),
        rows[0],
    );

    let mut items = Vec::new();
    let new_label = match app.mode {
        AppMode::Chat => "New Kim chat",
        AppMode::Code => "New code chat in this folder",
    };
    items.push(session_row(
        new_label,
        "start fresh",
        app.selected_session == 0,
        theme,
    ));
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
    let list = List::new(items).block(
        Block::default()
            .title(if app.mode == AppMode::Code {
                " Code Conversations "
            } else {
                " Kim Chats "
            })
            .borders(Borders::ALL)
            .border_style(Style::default().fg(theme.border))
            .style(Style::default().bg(theme.bg)),
    );
    // Use ListState so ratatui auto-scrolls the viewport when the selection
    // moves beyond the visible area. Each row is 2 lines tall.
    let mut list_state = ListState::default();
    list_state.select(Some(app.selected_session));
    frame.render_stateful_widget(list, rows[1], &mut list_state);
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

fn draw_chat(frame: &mut Frame<'_>, app: &App, area: Rect, theme: Theme) {
    let mut lines = Vec::new();
    lines.push(Line::from(vec![
        Span::styled(
            "Esc ",
            Style::default()
                .fg(theme.accent)
                .add_modifier(Modifier::BOLD),
        ),
        Span::styled("returns to chat list", Style::default().fg(theme.text_dim)),
        Span::styled(" · ", Style::default().fg(theme.text_dimmer)),
        Span::styled(
            "Press Tab to switch Chat/Code from the list",
            Style::default().fg(theme.text_dimmer),
        ),
    ]));
    lines.push(Line::raw(""));
    for message in app.visible_messages() {
        let (label, color) = match message.role {
            MessageRole::User => ("you", theme.accent),
            MessageRole::Assistant => ("kim", theme.text),
            MessageRole::System => ("note", theme.text_dim),
            MessageRole::Error => ("error", theme.danger),
        };
        let cleaned = clean_for_display(&message.content);
        let indent = " ".repeat(label.len() + 1);
        let mut first = true;
        for text_line in cleaned.lines() {
            if first {
                lines.push(Line::from(vec![
                    Span::styled(
                        format!("{label} "),
                        Style::default().fg(color).add_modifier(Modifier::BOLD),
                    ),
                    Span::styled(text_line.to_string(), Style::default().fg(theme.text)),
                ]));
                first = false;
            } else {
                lines.push(Line::styled(
                    format!("{indent}{text_line}"),
                    Style::default().fg(theme.text),
                ));
            }
        }
        if first {
            // empty message — emit the label at minimum
            lines.push(Line::from(vec![Span::styled(
                format!("{label} "),
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
        lines.push(Line::styled(hint, Style::default().fg(theme.text_dim)));
    }
    if app.busy {
        let elapsed = app.thinking_start.map_or(Duration::ZERO, |t| t.elapsed());
        lines.push(Line::from(vec![
            thinking::spinner_span(elapsed, thinking::SpinnerStyle::Braille, theme.accent),
            ratatui::text::Span::styled(
                "  thinking…",
                ratatui::style::Style::default().fg(theme.accent),
            ),
        ]));
    }
    // Compute scroll. Publish max_scroll so the event loop can reason about
    // when to re-engage follow-mode without needing access to the terminal size.
    let visible_height = area.height.saturating_sub(2);
    let max_scroll = (lines.len() as u16).saturating_sub(visible_height);
    app.last_max_scroll.set(max_scroll);
    let effective_scroll = if app.follow {
        max_scroll
    } else {
        app.scroll.min(max_scroll)
    };
    let scrolled_up = !app.follow && effective_scroll < max_scroll;
    let chat_title = if app.view == ViewState::SessionMenu {
        " Chat List ".to_string()
    } else if scrolled_up {
        " Chat · ↓ scroll to bottom · Esc returns to list ".to_string()
    } else {
        " Chat · Esc returns to chat list ".to_string()
    };
    let chat = Paragraph::new(lines)
        .wrap(Wrap { trim: false })
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
    let items = app
        .model_options
        .iter()
        .take(11)
        .enumerate()
        .map(|(index, model)| {
            let selected = index
                == app
                    .selected_model
                    .min(app.model_options.len().saturating_sub(1));
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
    frame.render_widget(list, area);
}

fn draw_input(frame: &mut Frame<'_>, app: &App, area: Rect, theme: Theme) {
    let title = if app.view == ViewState::SessionMenu {
        " Slash command / open a chat first "
    } else {
        " Message · drag/drop PNG or file to paste path "
    };
    let input = Paragraph::new(app.input.as_str())
        .style(Style::default().fg(theme.text).bg(theme.panel_alt))
        .block(
            Block::default()
                .title(title)
                .borders(Borders::ALL)
                .border_style(Style::default().fg(theme.border)),
        );
    frame.render_widget(input, area);
    let input_width = u16::try_from(app.input.len()).unwrap_or(u16::MAX);
    let cursor_x = area.x.saturating_add(1).saturating_add(input_width);
    let cursor_y = area.y.saturating_add(1);
    frame.set_cursor_position((cursor_x.min(area.right().saturating_sub(2)), cursor_y));
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
    let items = matches
        .iter()
        .take(8)
        .enumerate()
        .map(|(index, command)| {
            let selected = index == app.slash_selected.min(matches.len().saturating_sub(1));
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
    frame.render_widget(list, area);
}

fn clean_for_display(text: &str) -> String {
    let mut output: Vec<String> = Vec::new();
    let mut in_code_block = false;
    for line in text.lines() {
        let trimmed_start = line.trim_start();
        if trimmed_start.starts_with("```") {
            in_code_block = !in_code_block;
            output.push("  ─────".to_string());
            continue;
        }
        if in_code_block {
            output.push(format!("  {line}"));
            continue;
        }
        // Strip leading # header markers and > blockquote markers
        let line = trimmed_start.trim_start_matches('#').trim_start();
        let line = line.strip_prefix("> ").unwrap_or(line);
        output.push(strip_inline_md(line));
    }
    output.join("\n")
}

fn strip_inline_md(text: &str) -> String {
    let mut result = String::with_capacity(text.len());
    let mut chars = text.chars().peekable();
    while let Some(ch) = chars.next() {
        match ch {
            '*' | '`' => {
                // consume all consecutive identical marker chars, emit nothing
                while chars.peek() == Some(&ch) {
                    chars.next();
                }
            }
            _ => result.push(ch),
        }
    }
    result
}

fn draw_status(frame: &mut Frame<'_>, app: &App, area: Rect, theme: Theme) {
    let current_status = if app.busy {
        let elapsed = app.thinking_start.map_or(Duration::ZERO, |t| t.elapsed());
        format!("{} · worked for {}", app.status, format_elapsed(elapsed))
    } else {
        app.status.clone()
    };
    let status = if app.view == ViewState::SessionMenu {
        format!(
            "  Enter opens selected · Press Tab to switch Chat/Code · /login /model /help · {}",
            current_status
        )
    } else {
        format!(
            "  Esc returns to chat list · type /sessions for chat list · Ctrl-C twice exits · {}",
            current_status
        )
    };
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

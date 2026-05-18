use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, List, ListItem, Paragraph, Wrap};
use ratatui::Frame;

use crate::theme::Theme;
use crate::{App, AppMode, MessageRole, ViewState};

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
        Span::styled(
            " v1.192",
            Style::default().fg(theme.text_dimmer),
        ),
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
    frame.render_widget(list, rows[1]);
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
        lines.push(Line::from(vec![
            Span::styled(
                format!("{label} "),
                Style::default().fg(color).add_modifier(Modifier::BOLD),
            ),
            Span::styled(message.content.as_str(), Style::default().fg(theme.text)),
        ]));
        lines.push(Line::raw(""));
    }
    if app.messages.is_empty() {
        lines.push(Line::styled(
            if app.view == ViewState::SessionMenu {
                "Message box accepts slash commands here. Open New chat before sending prompts."
            } else {
                "Type a message, or /help. Esc returns to the chat list."
            },
            Style::default().fg(theme.text_dim),
        ));
    }
    if app.busy {
        lines.push(Line::styled(
            "kim is thinking…",
            Style::default().fg(theme.accent),
        ));
    }
    let chat = Paragraph::new(lines)
        .wrap(Wrap { trim: false })
        .scroll((app.scroll, 0))
        .block(
            Block::default()
                .title(if app.view == ViewState::SessionMenu {
                    " Chat List "
                } else {
                    " Chat · Esc returns to chat list "
                })
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

fn draw_status(frame: &mut Frame<'_>, app: &App, area: Rect, theme: Theme) {
    let status = if app.view == ViewState::SessionMenu {
        format!(
            "  Enter opens selected · Press Tab to switch Chat/Code · /login /model /help · {}",
            app.status
        )
    } else {
        format!(
            "  Esc returns to chat list · type /sessions for chat list · Ctrl-C twice exits · {}",
            app.status
        )
    };
    frame.render_widget(
        Paragraph::new(status).style(Style::default().fg(theme.text_dim).bg(theme.status)),
        area,
    );
}

use std::io::{self, stdout, Write};

use crossterm::cursor::{MoveToColumn, MoveUp};
use crossterm::event::{self, Event, KeyCode, KeyEventKind};
use crossterm::execute;
use crossterm::terminal::{disable_raw_mode, enable_raw_mode, Clear, ClearType};

use crate::sessions::load_session_messages;
use crate::{
    kim_accent_color, paint_bold, paint_dim, print_message, print_model_options, print_note,
    print_recent_transcript, App, MessageRole, UiMessage, ViewState,
};

struct RawModeGuard;

impl RawModeGuard {
    fn enter() -> io::Result<Self> {
        enable_raw_mode()?;
        Ok(Self)
    }
}

impl Drop for RawModeGuard {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
    }
}

pub(super) fn choose_model_interactively(
    app: &mut App,
    options: &[String],
) -> Result<(), Box<dyn std::error::Error>> {
    if options.is_empty() {
        print_model_options(&app.config.model, options);
        return Ok(());
    }

    let mut selected = options
        .iter()
        .position(|model| model == &app.config.model)
        .unwrap_or(0);
    let mut out = stdout();
    let _raw_mode = RawModeGuard::enter()?;
    let mut rendered_lines = render_model_picker(&mut out, options, selected, &app.config.model)?;

    loop {
        match event::read()? {
            Event::Key(key) if matches!(key.kind, KeyEventKind::Press | KeyEventKind::Repeat) => {
                match key.code {
                    KeyCode::Up => {
                        selected = selected.saturating_sub(1);
                        rendered_lines = rerender_model_picker(
                            &mut out,
                            rendered_lines,
                            options,
                            selected,
                            &app.config.model,
                        )?;
                    }
                    KeyCode::Down => {
                        selected = selected
                            .saturating_add(1)
                            .min(options.len().saturating_sub(1));
                        rendered_lines = rerender_model_picker(
                            &mut out,
                            rendered_lines,
                            options,
                            selected,
                            &app.config.model,
                        )?;
                    }
                    KeyCode::Enter => {
                        clear_rendered_lines(&mut out, rendered_lines)?;
                        let model = options[selected].clone();
                        app.config.model = model.clone();
                        let note = match app.config.save() {
                            Ok(()) => format!("model -> {model}"),
                            Err(error) => {
                                format!("model -> {model}\nWarning: config was not saved: {error}")
                            }
                        };
                        drop(_raw_mode);
                        print_note(&note);
                        return Ok(());
                    }
                    KeyCode::Esc | KeyCode::Char('q') => {
                        clear_rendered_lines(&mut out, rendered_lines)?;
                        drop(_raw_mode);
                        print_note("model unchanged");
                        return Ok(());
                    }
                    _ => {}
                }
            }
            _ => {}
        }
    }
}

fn rerender_model_picker(
    out: &mut impl Write,
    rendered_lines: u16,
    options: &[String],
    selected: usize,
    current: &str,
) -> io::Result<u16> {
    clear_rendered_lines(out, rendered_lines)?;
    render_model_picker(out, options, selected, current)
}

fn clear_rendered_lines(out: &mut impl Write, rendered_lines: u16) -> io::Result<()> {
    if rendered_lines > 0 {
        execute!(
            out,
            MoveUp(rendered_lines),
            MoveToColumn(0),
            Clear(ClearType::FromCursorDown)
        )?;
    }
    out.flush()
}

fn raw_writeln(out: &mut impl Write, line: &str) -> io::Result<()> {
    write!(out, "{line}\r\n")
}

fn render_model_picker(
    out: &mut impl Write,
    options: &[String],
    selected: usize,
    current: &str,
) -> io::Result<u16> {
    let max_visible = 12usize;
    let half = max_visible / 2;
    let start = selected
        .saturating_sub(half)
        .min(options.len().saturating_sub(max_visible));
    let end = options.len().min(start + max_visible);
    let mut lines = 0u16;

    raw_writeln(
        out,
        &paint_bold("Choose model (Up/Down, Enter, Esc)", kim_accent_color()),
    )?;
    lines += 1;

    if start > 0 {
        raw_writeln(out, &format!("  {}", paint_dim("...")))?;
        lines += 1;
    }

    for (index, model) in options.iter().enumerate().take(end).skip(start) {
        let pointer = if index == selected { ">" } else { " " };
        let active = if model == current { " current" } else { "" };
        let line = format!("{pointer} {model}{active}");
        if index == selected {
            raw_writeln(out, &paint_bold(&line, kim_accent_color()))?;
        } else {
            raw_writeln(out, &line)?;
        }
        lines += 1;
    }

    if end < options.len() {
        raw_writeln(out, &format!("  {}", paint_dim("...")))?;
        lines += 1;
    }

    raw_writeln(out, &paint_dim("q or Esc cancels"))?;
    lines += 1;
    out.flush()?;
    Ok(lines)
}

pub(super) fn choose_session_interactively(
    app: &mut App,
) -> Result<(), Box<dyn std::error::Error>> {
    app.refresh_sessions();
    if app.sessions.is_empty() {
        print_note("No saved sessions yet. Keep typing to chat here.");
        return Ok(());
    }

    let mut selected = 0usize;
    let mut out = stdout();
    let _raw_mode = RawModeGuard::enter()?;
    let mut rendered_lines = render_session_picker(&mut out, app, selected)?;

    loop {
        match event::read()? {
            Event::Key(key) if matches!(key.kind, KeyEventKind::Press | KeyEventKind::Repeat) => {
                match key.code {
                    KeyCode::Up => {
                        selected = selected.saturating_sub(1);
                        rendered_lines =
                            rerender_session_picker(&mut out, rendered_lines, app, selected)?;
                    }
                    KeyCode::Down => {
                        selected = selected.saturating_add(1).min(app.sessions.len());
                        rendered_lines =
                            rerender_session_picker(&mut out, rendered_lines, app, selected)?;
                    }
                    KeyCode::Enter => {
                        clear_rendered_lines(&mut out, rendered_lines)?;
                        if selected == 0 {
                            drop(_raw_mode);
                            print_note("staying in current chat");
                            return Ok(());
                        }
                        let session = app.sessions[selected - 1].clone();
                        drop(_raw_mode);
                        match load_session_messages(&session.path) {
                            Ok(messages) => {
                                app.messages = messages;
                                app.current_session_id = session.id.clone();
                                app.view = ViewState::InChat;
                                print_note(&format!("opened {}", session.label));
                                print_recent_transcript(app);
                            }
                            Err(error) => print_message(&UiMessage {
                                role: MessageRole::Error,
                                content: error,
                                timestamp_ms: None,
                            }),
                        }
                        return Ok(());
                    }
                    KeyCode::Esc | KeyCode::Char('q') => {
                        clear_rendered_lines(&mut out, rendered_lines)?;
                        drop(_raw_mode);
                        print_note("staying in current chat");
                        return Ok(());
                    }
                    _ => {}
                }
            }
            _ => {}
        }
    }
}

fn rerender_session_picker(
    out: &mut impl Write,
    rendered_lines: u16,
    app: &App,
    selected: usize,
) -> io::Result<u16> {
    clear_rendered_lines(out, rendered_lines)?;
    render_session_picker(out, app, selected)
}

fn render_session_picker(out: &mut impl Write, app: &App, selected: usize) -> io::Result<u16> {
    let max_visible = 12usize;
    let half = max_visible / 2;
    let item_count = app.sessions.len().saturating_add(1);
    let start = selected
        .saturating_sub(half)
        .min(item_count.saturating_sub(max_visible));
    let end = item_count.min(start + max_visible);
    let mut lines = 0u16;

    raw_writeln(
        out,
        &paint_bold("Choose session (Up/Down, Enter, Esc)", kim_accent_color()),
    )?;
    lines += 1;
    raw_writeln(out, &paint_dim("Esc or q keeps you in the current chat."))?;
    lines += 1;

    if start > 0 {
        raw_writeln(out, &format!("  {}", paint_dim("...")))?;
        lines += 1;
    }

    for index in start..end {
        let pointer = if index == selected { ">" } else { " " };
        let line = if index == 0 {
            format!("{pointer} Continue current chat")
        } else {
            let session = &app.sessions[index - 1];
            format!("{pointer} {} ({})", session.label, session.id)
        };
        if index == selected {
            raw_writeln(out, &paint_bold(&line, kim_accent_color()))?;
        } else {
            raw_writeln(out, &line)?;
        }
        lines += 1;
    }

    if end < item_count {
        raw_writeln(out, &format!("  {}", paint_dim("...")))?;
        lines += 1;
    }

    out.flush()?;
    Ok(lines)
}

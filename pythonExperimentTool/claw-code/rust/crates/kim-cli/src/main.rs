mod commands;
mod config;
mod provider;
mod sessions;
mod theme;
mod thinking;
mod ui;

use std::io::{self, stdout};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use commands::{handle_command, CommandOutcome, SUPPORTED_COMMANDS};
use config::KimConfig;
use crossterm::event::{
    self, DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture,
    Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseEventKind,
};
use crossterm::execute;
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use provider::{stream_kim_request, AppEvent, ChatMessage};
use ratatui::backend::CrosstermBackend;
use ratatui::Terminal;
use runtime::{
    compact_session, ContentBlock, ConversationMessage, MessageRole as RuntimeMessageRole,
    Session as RuntimeSession,
};
use sessions::{
    discover_project_sessions, discover_sessions, find_session_by_id, load_session_messages,
    SessionEntry,
};
use thinking::TraceItem;
use tokio::sync::mpsc::UnboundedReceiver;
use tokio::task::JoinHandle;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MessageRole {
    User,
    Assistant,
    System,
    Error,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AppMode {
    Chat,
    Code,
}

impl AppMode {
    #[must_use]
    pub const fn label(self) -> &'static str {
        match self {
            Self::Chat => "chat",
            Self::Code => "code",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ViewState {
    SessionMenu,
    InChat,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UiMessage {
    pub role: MessageRole,
    pub content: String,
}

#[allow(clippy::struct_excessive_bools)]
pub struct App {
    pub config: KimConfig,
    pub messages: Vec<UiMessage>,
    pub input: String,
    pub status: String,
    /// Current scroll offset (lines from top). Only meaningful when `follow = false`.
    pub scroll: u16,
    /// When true, always pin the view to the bottom of content (auto-scroll mode).
    pub follow: bool,
    /// Actual max scroll computed by draw_chat each frame, published via interior mutability.
    pub last_max_scroll: std::cell::Cell<u16>,
    pub busy: bool,
    pub sessions: Vec<SessionEntry>,
    pub selected_session: usize,
    pub current_session_id: String,
    pub slash_selected: usize,
    pub ctrl_c_armed: bool,
    pub mode: AppMode,
    pub model_options: Vec<String>,
    pub selected_model: usize,
    pub model_picker_open: bool,
    pub allow_empty_session_open: bool,
    pub view: ViewState,
    pub bridge_connected: bool,
    // streaming / thinking panel state
    pub trace: Vec<TraceItem>,
    pub thinking_start: Option<Instant>,
    pub event_rx: Option<UnboundedReceiver<AppEvent>>,
    pub task_handle: Option<JoinHandle<()>>,
}

impl App {
    fn new(config: KimConfig, resume_id: Option<&str>) -> Self {
        let mut app = Self {
            current_session_id: new_session_id(),
            config,
            messages: Vec::new(),
            input: String::new(),
            status: "ready".to_string(),
            scroll: 0,
            follow: true,
            last_max_scroll: std::cell::Cell::new(0),
            busy: false,
            sessions: discover_sessions(),
            selected_session: 0,
            slash_selected: 0,
            ctrl_c_armed: false,
            mode: AppMode::Chat,
            model_options: Vec::new(),
            selected_model: 0,
            model_picker_open: false,
            allow_empty_session_open: false,
            view: ViewState::SessionMenu,
            bridge_connected: false,
            trace: Vec::new(),
            thinking_start: None,
            event_rx: None,
            task_handle: None,
        };
        if let Some(resume_id) = resume_id {
            app.resume_session(resume_id);
        }
        app
    }

    fn resume_session(&mut self, session_id: &str) {
        let Some(session) = find_session_by_id(session_id) else {
            self.push(
                MessageRole::Error,
                format!("Could not find session {session_id}."),
            );
            return;
        };
        self.current_session_id.clone_from(&session.id);
        if let Some(index) = self.sessions.iter().position(|e| e.path == session.path) {
            self.selected_session = index;
        }
        match load_session_messages(&session.path) {
            Ok(messages) => {
                self.messages = messages;
                self.view = ViewState::InChat;
                self.scroll = 0;
                self.follow = true;
                self.status = format!("resumed {}", session.label);
            }
            Err(error) => {
                self.view = ViewState::InChat;
                self.push(MessageRole::Error, error);
            }
        }
    }

    #[must_use]
    pub fn slash_matches(&self) -> Vec<&'static str> {
        if !self.input.starts_with('/') {
            return Vec::new();
        }
        let typed = self
            .input
            .split_whitespace()
            .next()
            .unwrap_or(self.input.as_str());
        SUPPORTED_COMMANDS
            .iter()
            .copied()
            .filter(|c| c.starts_with(typed))
            .collect()
    }

    fn selected_slash_match(&self) -> Option<&'static str> {
        let matches = self.slash_matches();
        matches
            .get(self.slash_selected.min(matches.len().saturating_sub(1)))
            .copied()
    }

    fn move_slash_selection(&mut self, delta: isize) {
        let matches = self.slash_matches();
        if matches.is_empty() {
            return;
        }
        let current = isize::try_from(self.slash_selected).unwrap_or(0);
        let max = isize::try_from(matches.len().saturating_sub(1)).unwrap_or(0);
        self.slash_selected =
            usize::try_from(current.saturating_add(delta).clamp(0, max)).unwrap_or(0);
        self.status = format!("selected command {}", matches[self.slash_selected]);
    }

    fn sync_slash_selection(&mut self) {
        let matches = self.slash_matches();
        if self.slash_selected >= matches.len() {
            self.slash_selected = 0;
        }
    }

    fn complete_slash_selection(&mut self) -> bool {
        let Some(command) = self.selected_slash_match() else {
            return false;
        };
        let current_command = self
            .input
            .split_whitespace()
            .next()
            .unwrap_or(self.input.as_str());
        if current_command == command && self.input.trim() != "/" {
            return false;
        }
        self.input = format!("{command} ");
        self.status = format!("selected {command}");
        true
    }

    fn arm_or_exit_with_ctrl_c(&mut self) -> bool {
        if !self.input.is_empty() {
            self.input.clear();
            self.ctrl_c_armed = false;
            self.status = "cleared input".to_string();
            return false;
        }
        if self.ctrl_c_armed {
            return true;
        }
        self.ctrl_c_armed = true;
        self.status = format!(
            "press Ctrl-C again to exit · resume with kim --resume {}",
            self.current_session_id
        );
        false
    }

    fn reset_ctrl_c(&mut self) {
        self.ctrl_c_armed = false;
    }

    fn cancel_in_flight(&mut self) {
        if let Some(handle) = self.task_handle.take() {
            handle.abort();
        }
        self.event_rx = None;
        self.busy = false;
        self.thinking_start = None;
        self.follow = true;
        self.status = "cancelled".to_string();
    }

    fn toggle_mode(&mut self) {
        let next = match self.mode {
            AppMode::Chat => AppMode::Code,
            AppMode::Code => AppMode::Chat,
        };
        self.set_mode(next);
    }

    fn set_mode(&mut self, mode: AppMode) {
        self.mode = mode;
        self.refresh_sessions();
        self.view = ViewState::SessionMenu;
        self.status = format!(
            "kim {} mode · {}",
            self.mode.label(),
            if self.mode == AppMode::Code {
                "sessions are limited to this project"
            } else {
                "showing Kim chat sessions"
            }
        );
    }

    fn open_model_picker(&mut self, options: Vec<String>) {
        self.model_options = options;
        self.selected_model = self
            .model_options
            .iter()
            .position(|m| m == &self.config.model)
            .unwrap_or(0);
        self.model_picker_open = true;
        self.status = "choose model with ↑/↓, Enter confirms, Esc closes".to_string();
    }

    fn move_model_selection(&mut self, delta: isize) {
        if self.model_options.is_empty() {
            return;
        }
        let current = isize::try_from(self.selected_model).unwrap_or(0);
        let max = isize::try_from(self.model_options.len().saturating_sub(1)).unwrap_or(0);
        self.selected_model =
            usize::try_from(current.saturating_add(delta).clamp(0, max)).unwrap_or(0);
        self.status = format!("selected model {}", self.model_options[self.selected_model]);
    }

    fn confirm_model_selection(&mut self) {
        let Some(model) = self.model_options.get(self.selected_model) else {
            self.model_picker_open = false;
            return;
        };
        self.config.model.clone_from(model);
        let message = match self.config.save() {
            Ok(()) => format!("Model set to {}.", self.config.model),
            Err(error) => format!(
                "Model set to {}.\nWarning: config was not saved: {error}",
                self.config.model
            ),
        };
        self.model_picker_open = false;
        self.push(MessageRole::System, message);
        self.status = "ready".to_string();
    }

    fn close_overlays(&mut self) -> bool {
        if self.model_picker_open {
            self.model_picker_open = false;
            self.status = "model picker closed".to_string();
            return true;
        }
        false
    }

    pub fn visible_messages(&self) -> impl Iterator<Item = &UiMessage> {
        self.messages
            .iter()
            .skip(self.messages.len().saturating_sub(120))
    }

    fn push(&mut self, role: MessageRole, content: impl Into<String>) {
        self.messages.push(UiMessage {
            role,
            content: content.into(),
        });
    }

    fn chat_history(&self) -> Vec<ChatMessage> {
        self.messages
            .iter()
            .filter_map(|m| match m.role {
                MessageRole::User => Some(ChatMessage {
                    role: "user".to_string(),
                    content: m.content.clone(),
                }),
                MessageRole::Assistant => Some(ChatMessage {
                    role: "assistant".to_string(),
                    content: m.content.clone(),
                }),
                MessageRole::System | MessageRole::Error => None,
            })
            .rev()
            .take(24)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect()
    }

    fn refresh_sessions(&mut self) {
        self.sessions = match self.mode {
            AppMode::Chat => discover_sessions(),
            AppMode::Code => discover_project_sessions(),
        };
        if self.sessions.is_empty() {
            self.selected_session = 0;
        } else {
            self.selected_session = self.selected_session.min(self.sessions.len());
        }
    }

    fn move_session_selection(&mut self, delta: isize) {
        let item_count = self.sessions.len().saturating_add(1);
        if item_count == 0 {
            return;
        }
        let current = isize::try_from(self.selected_session).unwrap_or(0);
        let max = isize::try_from(item_count.saturating_sub(1)).unwrap_or(0);
        self.selected_session =
            usize::try_from(current.saturating_add(delta).clamp(0, max)).unwrap_or(0);
        self.status = if self.selected_session == 0 {
            "selected New chat".to_string()
        } else {
            format!(
                "selected {}",
                self.sessions[self.selected_session.saturating_sub(1)].label
            )
        };
    }

    fn open_selected_session(&mut self) {
        if self.selected_session == 0 {
            self.start_new_chat();
            return;
        }
        let Some(session) = self.sessions.get(self.selected_session - 1) else {
            self.push(MessageRole::System, "No session selected.");
            return;
        };
        if session.id == self.current_session_id {
            self.view = ViewState::InChat;
            self.status = "already in current session".to_string();
            return;
        }
        match load_session_messages(&session.path) {
            Ok(messages) => {
                self.messages = messages;
                self.view = ViewState::InChat;
                self.scroll = 0;
                self.follow = true;
                self.current_session_id.clone_from(&session.id);
                self.status = format!("opened {}", session.label);
            }
            Err(error) => {
                self.view = ViewState::InChat;
                self.push(MessageRole::Error, error);
                self.status = "could not open session".to_string();
            }
        }
    }

    fn ensure_current_session_listed(&mut self, title: &str) {
        if self
            .sessions
            .iter()
            .any(|s| s.id == self.current_session_id)
        {
            return;
        }
        self.sessions.insert(
            0,
            SessionEntry {
                id: self.current_session_id.clone(),
                label: truncate_for_sidebar(title),
                preview: "current session".to_string(),
                path: PathBuf::new(),
            },
        );
        self.selected_session = 0;
    }

    fn start_new_chat(&mut self) {
        self.current_session_id = new_session_id();
        self.messages.clear();
        self.input.clear();
        self.scroll = 0;
        self.follow = true;
        self.view = ViewState::InChat;
        self.status = format!("new Kim {} chat · type your message", self.mode.label());
    }

    fn show_session_menu(&mut self) {
        self.refresh_sessions();
        self.view = ViewState::SessionMenu;
        self.input.clear();
        self.status = "session menu · choose New chat or a saved session".to_string();
    }

    #[allow(dead_code)]
    fn return_to_session_menu(&mut self) {
        self.show_session_menu();
        self.status =
            "chat list · ↑/↓ select · Enter open · press Tab to switch Chat/Code".to_string();
    }

    fn paste_text(&mut self, text: &str) {
        self.reset_ctrl_c();
        self.allow_empty_session_open = false;
        let file_paths = prompt_file_references(text);
        if !file_paths.is_empty() {
            let paths_text = file_paths
                .iter()
                .map(|p| p.display().to_string())
                .collect::<Vec<_>>()
                .join(" ");
            if !self.input.is_empty() && !self.input.ends_with(char::is_whitespace) {
                self.input.push(' ');
            }
            self.input.push_str(&paths_text);
            self.sync_slash_selection();
            self.status = "attached file path · press Enter to send".to_string();
            return;
        }
        if self.view == ViewState::InChat {
            let safe = text.replace(['\r', '\n'], " ");
            let safe = safe.trim();
            if !safe.is_empty() {
                if !self.input.is_empty() && !self.input.ends_with(char::is_whitespace) {
                    self.input.push(' ');
                }
                self.input.push_str(safe);
                self.sync_slash_selection();
                self.status = "pasted text · press Enter to send".to_string();
                return;
            }
        }
        self.status =
            "paste ignored · open a chat first, or drag/drop a real file path".to_string();
    }
}

/* ===========================================================
entry point
=========================================================== */

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    if args.iter().any(|a| matches!(a.as_str(), "--help" | "-h")) {
        println!("{}", help_text());
        return Ok(());
    }
    if args
        .iter()
        .any(|a| matches!(a.as_str(), "--version" | "-V"))
    {
        println!("kim {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    let resume_id = args
        .windows(2)
        .find_map(|w| (w[0] == "--resume").then_some(w[1].as_str()));

    install_panic_hook();
    let mut terminal = enter_terminal()?;
    let result = run_app(&mut terminal, resume_id).await;
    leave_terminal(&mut terminal)?;
    match result {
        Ok(session_id) => println!("Resume this Kim session with: kim --resume {session_id}"),
        Err(error) => {
            eprintln!("kim error: {error}");
            std::process::exit(1);
        }
    }
    Ok(())
}

/* ===========================================================
main loop — non-blocking so the streaming task can run
=========================================================== */

async fn run_app(
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    resume_id: Option<&str>,
) -> Result<String, Box<dyn std::error::Error>> {
    let mut app = App::new(KimConfig::load(), resume_id);
    app.push(
        MessageRole::System,
        "Kim CLI v1. Pick New chat or a session. /login signs into Ollama; /mode switches chat/code.",
    );

    'main: loop {
        drain_events(&mut app);

        terminal.draw(|frame| ui::draw(frame, &app))?;

        // Non-blocking terminal event drain.
        let mut last_key: Option<(KeyCode, KeyModifiers, KeyEventKind, Instant)> = None;
        while event::poll(Duration::from_millis(0))? {
            match event::read()? {
                Event::Key(key) => {
                    if matches!(key.kind, KeyEventKind::Press | KeyEventKind::Repeat) {
                        let now = Instant::now();
                        let is_dup = last_key.as_ref().is_some_and(|(c, m, k, t)| {
                            *c == key.code
                                && *m == key.modifiers
                                && *k == key.kind
                                && now.duration_since(*t).as_millis() < 20
                        });
                        last_key = Some((key.code, key.modifiers, key.kind, now));
                        if !is_dup && handle_key(key, &mut app, terminal).await? {
                            break 'main;
                        }
                    }
                }
                Event::Paste(text) => app.paste_text(&text),
                Event::Mouse(mouse) => {
                    if app.view == ViewState::InChat {
                        match mouse.kind {
                            MouseEventKind::ScrollUp => {
                                if app.follow {
                                    app.follow = false;
                                    app.scroll = app.last_max_scroll.get();
                                }
                                app.scroll = app.scroll.saturating_sub(3);
                            }
                            MouseEventKind::ScrollDown => {
                                if !app.follow {
                                    app.scroll = app.scroll.saturating_add(3);
                                    if app.scroll >= app.last_max_scroll.get() {
                                        app.follow = true;
                                        app.scroll = 0;
                                    }
                                }
                            }
                            _ => {}
                        }
                    }
                }
                _ => {}
            }
        }

        // Yield to let the streaming task produce more chunks.
        tokio::time::sleep(Duration::from_millis(if app.busy { 33 } else { 60 })).await;
    }
    Ok(app.current_session_id)
}

/// Drain all pending `AppEvent`s from the channel into the App state.
fn drain_events(app: &mut App) {
    loop {
        let event = match app.event_rx.as_mut() {
            None => break,
            Some(rx) => match rx.try_recv() {
                Ok(e) => e,
                Err(_) => break,
            },
        };
        apply_app_event(app, event);
    }
}

fn apply_app_event(app: &mut App, event: AppEvent) {
    match event {
        AppEvent::TextChunk(chunk) => {
            // Append to the last assistant message, or start a new one.
            if let Some(last) = app.messages.last_mut() {
                if last.role == MessageRole::Assistant {
                    last.content.push_str(&chunk);
                    if app.follow {
                        app.scroll = 0;
                    }
                    return;
                }
            }
            app.push(MessageRole::Assistant, chunk);
            if app.follow {
                app.scroll = 0;
            }
        }
        AppEvent::ThoughtChunk(chunk) => {
            // Accumulate into the last Thought trace item, or push a new one.
            match app.trace.last_mut() {
                Some(TraceItem::Thought(ref mut text)) => text.push_str(&chunk),
                _ => app.trace.push(TraceItem::Thought(chunk)),
            }
        }
        AppEvent::ToolEvent { verb, target } => {
            app.trace.push(TraceItem::Tool { verb, target });
        }
        AppEvent::Done(bridge_used) => {
            let elapsed = app
                .thinking_start
                .take()
                .map_or(0.0, |t| t.elapsed().as_secs_f32());
            app.bridge_connected = bridge_used;
            app.busy = false;
            app.task_handle = None;
            app.event_rx = None;
            if matches!(app.messages.last(), Some(message) if message.role == MessageRole::Assistant && message.content.trim().is_empty())
            {
                app.messages.pop();
            }
            if app.follow {
                app.scroll = 0;
            }
            let via = if bridge_used {
                " · via Kim desktop"
            } else {
                ""
            };
            app.status = format!("done in {:.1}s{via}", elapsed);
        }
        AppEvent::Err(error) => {
            app.thinking_start = None;
            app.bridge_connected = false;
            app.busy = false;
            app.task_handle = None;
            app.event_rx = None;
            if matches!(app.messages.last(), Some(message) if message.role == MessageRole::Assistant && message.content.trim().is_empty())
            {
                app.messages.pop();
            }
            app.push(MessageRole::Error, error);
            if app.follow {
                app.scroll = 0;
            }
            app.status = "error".to_string();
        }
    }
}

/* ===========================================================
key handler
=========================================================== */

#[allow(clippy::too_many_lines)]
async fn handle_key(
    key: KeyEvent,
    app: &mut App,
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
) -> Result<bool, Box<dyn std::error::Error>> {
    if app.model_picker_open {
        return Ok(handle_model_picker_key(key, app));
    }
    match key.code {
        KeyCode::Enter => {
            if key.kind != KeyEventKind::Press {
                return Ok(false);
            }
            app.reset_ctrl_c();
            if app.complete_slash_selection() {
                return Ok(false);
            }
            let input = std::mem::take(&mut app.input);
            if input.trim().is_empty() {
                if app.view == ViewState::SessionMenu {
                    app.open_selected_session();
                    app.allow_empty_session_open = false;
                } else {
                    app.status = "type a message or /command".to_string();
                }
                return Ok(false);
            }
            app.allow_empty_session_open = false;
            if apply_mode_input(&input, app) {
                return Ok(false);
            }
            if app.view == ViewState::SessionMenu && !input.trim().starts_with('/') {
                app.status = "choose New chat first, then type your message".to_string();
                app.input = input;
                return Ok(false);
            }
            if input.trim() == "/clear" {
                app.messages.clear();
            }
            if is_interactive_login(&input) {
                if input.trim().eq_ignore_ascii_case("/login") {
                    app.push(MessageRole::System, "Opening Ollama sign-in…");
                    app.status = "opening Ollama sign-in".to_string();
                }
                suspend_terminal(terminal)?;
                println!("Kim login\n");
                let outcome = handle_command(&input, &mut app.config).await;
                if let CommandOutcome::Message(ref msg) = outcome {
                    println!("{msg}");
                }
                println!("\nPress Enter to return to Kim…");
                let mut pause = String::new();
                let _ = io::stdin().read_line(&mut pause);
                resume_terminal(terminal)?;
                return apply_outcome(app, outcome).await;
            }
            let input = prompt_with_file_references(&input);
            let outcome = handle_command(&input, &mut app.config).await;
            apply_outcome(app, outcome).await
        }
        KeyCode::Esc => {
            // Cancel an in-flight request first.
            if app.busy {
                app.cancel_in_flight();
                return Ok(false);
            }
            if app.close_overlays() {
                return Ok(false);
            }
            if app.view == ViewState::InChat {
                app.show_session_menu();
                return Ok(false);
            }
            Ok(app.arm_or_exit_with_ctrl_c())
        }
        KeyCode::Tab => {
            if key.kind != KeyEventKind::Press {
                return Ok(false);
            }
            app.reset_ctrl_c();
            app.allow_empty_session_open = false;
            app.toggle_mode();
            Ok(false)
        }
        KeyCode::Backspace => {
            app.reset_ctrl_c();
            app.allow_empty_session_open = false;
            app.input.pop();
            app.sync_slash_selection();
            Ok(false)
        }
        KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            if app.busy {
                app.cancel_in_flight();
                return Ok(false);
            }
            Ok(app.arm_or_exit_with_ctrl_c())
        }
        KeyCode::Char('l') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            app.reset_ctrl_c();
            app.allow_empty_session_open = false;
            app.messages.clear();
            app.status = "cleared".to_string();
            Ok(false)
        }
        KeyCode::Char('t') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            app.reset_ctrl_c();
            app.allow_empty_session_open = false;
            app.toggle_mode();
            Ok(false)
        }
        KeyCode::Char(char) => {
            app.reset_ctrl_c();
            app.allow_empty_session_open = false;
            app.input.push(char);
            app.sync_slash_selection();
            if app.view == ViewState::SessionMenu && !app.input.starts_with('/') {
                app.status = "press Enter on New chat first, or type /command".to_string();
            }
            Ok(false)
        }
        KeyCode::Up => {
            app.reset_ctrl_c();
            if app.input.starts_with('/') {
                app.move_slash_selection(-1);
            } else if app.view == ViewState::SessionMenu && app.input.is_empty() {
                app.allow_empty_session_open = true;
                app.move_session_selection(-1);
            } else {
                // Disengage follow-mode and scroll up into history.
                if app.follow {
                    app.follow = false;
                    app.scroll = app.last_max_scroll.get();
                }
                app.scroll = app.scroll.saturating_sub(3);
            }
            Ok(false)
        }
        KeyCode::Down => {
            app.reset_ctrl_c();
            if app.input.starts_with('/') {
                app.move_slash_selection(1);
            } else if app.view == ViewState::SessionMenu && app.input.is_empty() {
                app.allow_empty_session_open = true;
                app.move_session_selection(1);
            } else if !app.follow {
                app.scroll = app.scroll.saturating_add(3);
                // Re-engage follow-mode when we reach the bottom.
                if app.scroll >= app.last_max_scroll.get() {
                    app.follow = true;
                    app.scroll = 0;
                }
            }
            Ok(false)
        }
        _ => Ok(false),
    }
}

/* ===========================================================
outcome dispatch
=========================================================== */

async fn apply_outcome(
    app: &mut App,
    outcome: CommandOutcome,
) -> Result<bool, Box<dyn std::error::Error>> {
    match outcome {
        CommandOutcome::Exit => Ok(true),
        CommandOutcome::Message(message) => {
            if message == "Conversation cleared." {
                app.messages.clear();
            } else if message == "__KIM_REFRESH_SESSIONS__" {
                app.show_session_menu();
                app.push(
                    MessageRole::System,
                    format!(
                        "Loaded {} sessions. Choose New chat or use ↑/↓ then Enter.",
                        app.sessions.len()
                    ),
                );
                app.status = "session menu".to_string();
                return Ok(false);
            } else if message == "__KIM_TOGGLE_MODE__" {
                app.toggle_mode();
                return Ok(false);
            } else if message == "__KIM_COMPACT__" {
                let kept = app.messages.len().min(6);
                let dropped = app.messages.len().saturating_sub(kept);
                if dropped > 0 {
                    let tail = app.messages.split_off(app.messages.len() - kept);
                    app.messages = tail;
                }
                app.scroll = 0;
                app.push(
                    MessageRole::System,
                    format!("Compacted conversation: dropped {dropped} older message(s), kept last {kept}."),
                );
                app.status = "compacted".to_string();
                return Ok(false);
            } else {
                app.push(MessageRole::System, message);
            }
            app.status = "ready".to_string();
            Ok(false)
        }
        CommandOutcome::OpenModelPicker(options) => {
            app.open_model_picker(options);
            Ok(false)
        }
        CommandOutcome::Compact => {
            compact_app_messages(app);
            Ok(false)
        }
        CommandOutcome::SendPrompt(prompt) => {
            if app.view == ViewState::SessionMenu {
                app.status = "choose New chat or open a session before sending".to_string();
                return Ok(false);
            }
            app.push(MessageRole::User, prompt.clone());
            if let Some(last_user) = app
                .messages
                .iter()
                .rev()
                .find(|m| m.role == MessageRole::User)
                .map(|m| m.content.clone())
            {
                app.ensure_current_session_listed(&last_user);
            }
            let history = app.chat_history();
            app.push(MessageRole::Assistant, String::new());
            app.follow = true; // snap to bottom
            app.scroll = 0;
            app.busy = true;
            app.trace.clear();
            app.trace.push(TraceItem::Tool {
                verb: "Thinking".to_string(),
                target: format!("{} stream", app.config.model),
            });
            app.thinking_start = Some(Instant::now());
            app.status = "working".to_string();

            let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<AppEvent>();
            app.event_rx = Some(rx);

            let config = app.config.clone();
            let code_mode = app.mode == AppMode::Code;

            let handle = tokio::spawn(async move {
                stream_kim_request(&config, &history, code_mode, tx).await;
            });
            app.task_handle = Some(handle);

            Ok(false)
        }
    }
}

/* ===========================================================
compaction
=========================================================== */

fn compact_app_messages(app: &mut App) {
    let mut session = RuntimeSession::new();
    session.session_id.clone_from(&app.current_session_id);
    session.messages = app
        .messages
        .iter()
        .filter_map(ui_message_to_runtime)
        .collect::<Vec<_>>();
    let result = compact_session(
        &session,
        runtime::CompactionConfig {
            preserve_recent_messages: 6,
            max_estimated_tokens: 1,
        },
    );
    if result.removed_message_count == 0 {
        app.push(
            MessageRole::System,
            "Nothing to compact yet; keeping the current conversation as-is.",
        );
        app.status = "compact skipped".to_string();
        return;
    }
    app.messages = result
        .compacted_session
        .messages
        .iter()
        .filter_map(runtime_message_to_ui)
        .collect();
    app.push(
        MessageRole::System,
        format!(
            "Compacted {} earlier messages with Claw-style local compaction.",
            result.removed_message_count
        ),
    );
    app.status = "context compacted".to_string();
}

/* ===========================================================
file reference helpers
=========================================================== */

fn prompt_with_file_references(input: &str) -> String {
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
    let expanded = if trimmed == "~" {
        dirs::home_dir()?
    } else if let Some(rest) = trimmed.strip_prefix("~/") {
        dirs::home_dir()?.join(rest)
    } else {
        PathBuf::from(trimmed)
    };
    let candidate = if expanded.is_absolute() {
        expanded
    } else {
        std::env::current_dir().ok()?.join(expanded)
    };
    candidate
        .exists()
        .then(|| std::fs::canonicalize(candidate).ok())
        .flatten()
}

fn split_shellish_tokens(input: &str) -> Vec<String> {
    let mut tokens = Vec::new();
    let mut current = String::new();
    let mut quote = None;
    let mut escaped = false;
    for ch in input.chars() {
        if escaped {
            current.push(ch);
            escaped = false;
            continue;
        }
        if ch == '\\' {
            escaped = true;
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
    if escaped {
        current.push('\\');
    }
    if !current.is_empty() {
        tokens.push(current);
    }
    tokens
}

/* ===========================================================
message converters
=========================================================== */

fn ui_message_to_runtime(message: &UiMessage) -> Option<ConversationMessage> {
    let role = match message.role {
        MessageRole::User => RuntimeMessageRole::User,
        MessageRole::Assistant => RuntimeMessageRole::Assistant,
        MessageRole::System => RuntimeMessageRole::System,
        MessageRole::Error => return None,
    };
    Some(ConversationMessage {
        role,
        blocks: vec![ContentBlock::Text {
            text: message.content.clone(),
        }],
        usage: None,
    })
}

fn runtime_message_to_ui(message: &ConversationMessage) -> Option<UiMessage> {
    let role = match message.role {
        RuntimeMessageRole::User => MessageRole::User,
        RuntimeMessageRole::Assistant => MessageRole::Assistant,
        RuntimeMessageRole::System => MessageRole::System,
        RuntimeMessageRole::Tool => return None,
    };
    let content = message
        .blocks
        .iter()
        .filter_map(|b| match b {
            ContentBlock::Text { text } => Some(text.as_str()),
            ContentBlock::ToolUse { .. } | ContentBlock::ToolResult { .. } => None,
        })
        .collect::<Vec<_>>()
        .join("\n");
    if content.trim().is_empty() {
        None
    } else {
        Some(UiMessage { role, content })
    }
}

/* ===========================================================
misc helpers
=========================================================== */

fn apply_mode_input(input: &str, app: &mut App) -> bool {
    match input.trim().to_ascii_lowercase().as_str() {
        "/mode chat" | "/chat" => {
            app.set_mode(AppMode::Chat);
            true
        }
        "/mode code" | "/code" => {
            app.set_mode(AppMode::Code);
            true
        }
        _ => false,
    }
}

fn handle_model_picker_key(key: KeyEvent, app: &mut App) -> bool {
    match key.code {
        KeyCode::Esc => {
            app.close_overlays();
            false
        }
        KeyCode::Enter => {
            app.confirm_model_selection();
            false
        }
        KeyCode::Up => {
            app.move_model_selection(-1);
            false
        }
        KeyCode::Down => {
            app.move_model_selection(1);
            false
        }
        KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            app.close_overlays();
            false
        }
        _ => false,
    }
}

fn is_interactive_login(input: &str) -> bool {
    let trimmed = input.trim().to_ascii_lowercase();
    trimmed.starts_with("/login ")
        && !matches!(trimmed.trim_start_matches("/login").trim(), "desktop")
        || trimmed == "/login"
}

/* ===========================================================
terminal lifecycle
=========================================================== */

static RAW_MODE_ACTIVE: AtomicBool = AtomicBool::new(false);

fn install_panic_hook() {
    let original = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        if RAW_MODE_ACTIVE.load(Ordering::SeqCst) {
            let _ = disable_raw_mode();
            let _ = execute!(
                stdout(),
                DisableMouseCapture,
                DisableBracketedPaste,
                LeaveAlternateScreen,
                crossterm::cursor::Show
            );
        }
        original(info);
    }));
}

fn enter_terminal() -> io::Result<Terminal<CrosstermBackend<std::io::Stdout>>> {
    enable_raw_mode()?;
    RAW_MODE_ACTIVE.store(true, Ordering::SeqCst);
    execute!(
        stdout(),
        EnterAlternateScreen,
        EnableBracketedPaste,
        EnableMouseCapture
    )?;
    Terminal::new(CrosstermBackend::new(stdout()))
}

fn leave_terminal(terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>) -> io::Result<()> {
    RAW_MODE_ACTIVE.store(false, Ordering::SeqCst);
    disable_raw_mode()?;
    execute!(
        terminal.backend_mut(),
        DisableMouseCapture,
        DisableBracketedPaste,
        LeaveAlternateScreen
    )?;
    terminal.show_cursor()
}

fn suspend_terminal(terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>) -> io::Result<()> {
    disable_raw_mode()?;
    execute!(
        terminal.backend_mut(),
        DisableMouseCapture,
        DisableBracketedPaste,
        LeaveAlternateScreen
    )?;
    terminal.show_cursor()
}

fn resume_terminal(terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>) -> io::Result<()> {
    enable_raw_mode()?;
    execute!(
        terminal.backend_mut(),
        EnterAlternateScreen,
        EnableBracketedPaste,
        EnableMouseCapture
    )?;
    terminal.clear()
}

fn help_text() -> &'static str {
    "Kim terminal CLI\n\nUsage:\n  kim                    Launch the terminal UI\n  kim --resume <id>      Resume a Kim session\n  kim --help             Show this help\n\nInside Kim, type /help for commands and /login to connect Ollama."
}

fn new_session_id() -> String {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_millis());
    format!("session-{millis}")
}

fn truncate_for_sidebar(text: &str) -> String {
    let compact = text.split_whitespace().collect::<Vec<_>>().join(" ");
    let mut chars = compact.chars();
    let title = chars.by_ref().take(34).collect::<String>();
    if chars.next().is_some() {
        format!("{title}…")
    } else if title.is_empty() {
        "New conversation".to_string()
    } else {
        title
    }
}

/* ===========================================================
tests
=========================================================== */

#[cfg(test)]
mod tests {
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

    use super::{prompt_file_references, prompt_with_file_references, split_shellish_tokens};

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

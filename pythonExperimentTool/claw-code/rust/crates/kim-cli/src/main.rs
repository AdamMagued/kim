#![allow(dead_code)]

mod commands;
mod config;
mod provider;
mod sessions;
mod theme;
mod thinking;
mod ui;

use std::io::{self, stdout, IsTerminal, Write};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use commands::{
    command_summary, handle_command, login_with_key, CommandOutcome, SUPPORTED_COMMANDS,
};
use config::KimConfig;
use crossterm::cursor::{MoveToColumn, MoveUp};
use crossterm::event::{
    self, DisableBracketedPaste, EnableBracketedPaste, Event, KeyCode, KeyEvent, KeyEventKind,
    KeyModifiers,
};
use crossterm::execute;
use crossterm::style::{Color as TerminalColor, Stylize};
use crossterm::terminal::{disable_raw_mode, enable_raw_mode, Clear, ClearType};
use provider::{stream_kim_request, AppEvent, ChatMessage};
use ratatui::backend::CrosstermBackend;

use ratatui::Terminal;
use runtime::{
    compact_session, ContentBlock, ConversationMessage, MessageRole as RuntimeMessageRole,
    Session as RuntimeSession,
};
use rustyline::completion::{Completer, Pair};
use rustyline::error::ReadlineError;
use rustyline::highlight::Highlighter;
use rustyline::hint::Hinter;
use rustyline::validate::{ValidationContext, ValidationResult, Validator};
use rustyline::{Context, Helper};
use sessions::{
    discover_project_sessions, discover_sessions, find_session_by_id, load_session_messages,
    save_session_messages, SessionEntry,
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
    Reasoning,
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
    pub input_cursor: usize,
    pub status: String,
    /// Current scroll offset (lines from top). Only meaningful when `follow = false`.
    pub scroll: u16,
    /// When true, always pin the view to the bottom of content (auto-scroll mode).
    pub follow: bool,
    /// Actual max scroll computed by draw_chat each frame, published via interior mutability.
    pub last_max_scroll: std::cell::Cell<u16>,
    /// Number of wrapped transcript rows already copied into terminal scrollback.
    pub scrollback_committed_rows: usize,
    pub scrollback_width: u16,
    pub scrollback_session_id: String,
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
    pub provider_picker_open: bool,
    pub selected_provider: usize,
    pub allow_empty_session_open: bool,
    pub view: ViewState,
    pub bridge_connected: bool,
    /// True when the current provider has a saved key (or is keyless like ollama/desktop
    /// and was successfully connected via /login).
    pub provider_ready: bool,
    // input history (Up/Down arrow recall, like a terminal shell)
    pub input_history: Vec<String>,
    pub history_cursor: Option<usize>,
    pub input_before_history: String,
    // secure input mode: API key entry rendered as • inside the TUI
    pub secure_input: bool,
    pub secure_input_provider: String,
    // streaming / thinking panel state
    pub trace: Vec<TraceItem>,
    pub thinking_start: Option<Instant>,
    pub event_rx: Option<UnboundedReceiver<AppEvent>>,
    pub task_handle: Option<JoinHandle<()>>,
}

impl App {
    pub fn clamp_cursor(&mut self) {
        self.input_cursor = self.input_cursor.min(self.input.chars().count());
    }

    pub fn cursor_byte_idx(&self) -> usize {
        self.input
            .char_indices()
            .nth(self.input_cursor)
            .map_or(self.input.len(), |(i, _)| i)
    }

    fn new(config: KimConfig, resume_id: Option<&str>) -> Self {
        let mut app = Self {
            current_session_id: new_session_id(),
            config,
            messages: Vec::new(),
            input: String::new(),
            input_cursor: 0,
            status: "ready".to_string(),
            scroll: 0,
            follow: true,
            last_max_scroll: std::cell::Cell::new(0),
            scrollback_committed_rows: 0,
            scrollback_width: 0,
            scrollback_session_id: String::new(),
            busy: false,
            sessions: discover_sessions(),
            selected_session: 0,
            slash_selected: 0,
            ctrl_c_armed: false,
            mode: AppMode::Chat,
            model_options: Vec::new(),
            selected_model: 0,
            model_picker_open: false,
            provider_picker_open: false,
            selected_provider: 0,
            allow_empty_session_open: false,
            view: ViewState::SessionMenu,
            bridge_connected: false,
            provider_ready: false,
            input_history: Vec::new(),
            history_cursor: None,
            input_before_history: String::new(),
            secure_input: false,
            secure_input_provider: String::new(),
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

    fn push_history(&mut self, input: String) {
        if self
            .input_history
            .last()
            .map_or(true, |last| last != &input)
        {
            self.input_history.push(input);
        }
    }

    fn history_up(&mut self) {
        if self.input_history.is_empty() {
            return;
        }
        let new_idx = match self.history_cursor {
            None => {
                self.input_before_history = self.input.clone();
                self.input_history.len() - 1
            }
            Some(0) => 0,
            Some(i) => i - 1,
        };
        self.history_cursor = Some(new_idx);
        self.input = self.input_history[new_idx].clone();
        self.input_cursor = self.input.chars().count();
        self.sync_slash_selection();
    }

    fn history_down(&mut self) {
        let Some(i) = self.history_cursor else { return };
        if i + 1 < self.input_history.len() {
            self.history_cursor = Some(i + 1);
            self.input = self.input_history[i + 1].clone();
        } else {
            self.history_cursor = None;
            self.input = self.input_before_history.clone();
        }
        self.input_cursor = self.input.chars().count();
        self.sync_slash_selection();
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
            self.input_cursor = 0;
            self.sync_slash_selection();
            self.ctrl_c_armed = true;
            self.status = format!(
                "cleared input · press Ctrl-C again to exit · resume with kim --resume {}",
                self.current_session_id
            );
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
        self.view = ViewState::InChat;
        self.status = format!(
            "kim {} mode · {}",
            self.mode.label(),
            if self.mode == AppMode::Code {
                "type here or use /sessions to choose a project session"
            } else {
                "type here or use /sessions to choose a saved chat"
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
        if self.provider_picker_open {
            self.provider_picker_open = false;
            self.status = "provider picker closed".to_string();
            return true;
        }
        false
    }

    fn open_provider_picker(&mut self) {
        self.selected_provider = provider::PROVIDERS
            .iter()
            .position(|p| p.name == self.config.provider)
            .unwrap_or(0);
        self.provider_picker_open = true;
        self.status = "choose provider · ↑/↓ navigate · Enter confirm · Esc close".to_string();
    }

    fn move_provider_selection(&mut self, delta: isize) {
        let max = isize::try_from(provider::PROVIDERS.len().saturating_sub(1)).unwrap_or(0);
        let current = isize::try_from(self.selected_provider).unwrap_or(0);
        self.selected_provider =
            usize::try_from(current.saturating_add(delta).clamp(0, max)).unwrap_or(0);
    }

    fn confirm_provider_selection(&mut self) {
        let Some(p) = provider::PROVIDERS.get(self.selected_provider) else {
            self.provider_picker_open = false;
            return;
        };
        let changed = self.config.provider != p.name;
        self.config.provider = p.name.to_string();
        if changed {
            self.config.model = p.default_model.to_string();
        }
        let _ = self.config.save();
        self.status = format!("provider → {}  model → {}", p.name, self.config.model);
        self.provider_picker_open = false;
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
                MessageRole::System | MessageRole::Error | MessageRole::Reasoning => None,
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
    match run_repl(resume_id).await {
        Ok(session_id) => println!("Resume this Kim session with: kim --resume {session_id}"),
        Err(error) => {
            eprintln!("kim error: {error}");
            std::process::exit(1);
        }
    }
    Ok(())
}

/* ===========================================================
Claude-style prompt loop
=========================================================== */

async fn run_repl(resume_id: Option<&str>) -> Result<String, Box<dyn std::error::Error>> {
    let mut app = App::new(KimConfig::load(), resume_id);
    app.provider_ready = provider_is_ready(&app.config);
    app.view = ViewState::InChat;

    if resume_id.is_none() && io::stdin().is_terminal() {
        choose_start_mode(&mut app)?;
    }

    print_repl_header(&app);
    if resume_id.is_some() && !app.messages.is_empty() {
        println!("Resumed {}.", app.current_session_id);
        print_recent_transcript(&app);
    }

    if io::stdin().is_terminal() {
        run_repl_readline(&mut app).await?;
    } else {
        run_repl_stdio(&mut app).await?;
    }

    Ok(app.current_session_id)
}

fn choose_start_mode(app: &mut App) -> Result<(), Box<dyn std::error::Error>> {
    println!("{}", paint_bold("Choose a mode", kim_accent_color()));
    println!(
        "  {}  {}",
        paint_bold("c / code", kim_accent_color()),
        paint_text("Coding agent mode. Best for repo work, bugs, files, commands, and diffs.")
    );
    println!(
        "  {}  {}",
        paint_bold("h / chat", kim_accent_color()),
        paint_text("General chat mode. Best for normal questions, writing, and lightweight help.")
    );
    println!(
        "{}",
        paint_dim("Switch later with /mode, /code, or /chat. Press Enter for chat.")
    );

    loop {
        print!("{}", paint_bold("mode [c/h]> ", kim_accent_color()));
        stdout().flush()?;
        let mut input = String::new();
        if io::stdin().read_line(&mut input)? == 0 {
            println!();
            app.set_mode(AppMode::Chat);
            app.view = ViewState::InChat;
            return Ok(());
        }
        match input.trim().to_ascii_lowercase().as_str() {
            "" | "h" | "chat" | "2" => {
                app.set_mode(AppMode::Chat);
                break;
            }
            "c" | "code" | "1" => {
                app.set_mode(AppMode::Code);
                break;
            }
            _ => println!("{}", paint_dim("Press c for code or h for chat.")),
        }
    }
    app.view = ViewState::InChat;
    println!();
    Ok(())
}

async fn run_repl_stdio(app: &mut App) -> Result<(), Box<dyn std::error::Error>> {
    loop {
        print_repl_prompt(&app)?;
        let mut input = String::new();
        if io::stdin().read_line(&mut input)? == 0 {
            println!();
            break;
        }
        let input = input.trim_end_matches(['\r', '\n']).to_string();
        if input.trim().is_empty() {
            continue;
        }
        if handle_repl_input(app, input).await? {
            break;
        }
    }
    Ok(())
}

async fn run_repl_readline(app: &mut App) -> Result<(), Box<dyn std::error::Error>> {
    let mut editor = rustyline::Editor::<SlashHelper, rustyline::history::DefaultHistory>::new()?;
    editor.set_helper(Some(SlashHelper));
    loop {
        let prompt = repl_prompt(app);
        match editor.readline(&prompt) {
            Ok(input) => {
                app.ctrl_c_armed = false;
                let input = input.trim_end_matches(['\r', '\n']).to_string();
                if input.trim().is_empty() {
                    continue;
                }
                let _ = editor.add_history_entry(input.as_str());
                if handle_repl_input(app, input).await? {
                    break;
                }
            }
            Err(ReadlineError::Interrupted) => {
                if app.ctrl_c_armed {
                    break;
                }
                app.ctrl_c_armed = true;
                println!("press Ctrl-C again to exit");
            }
            Err(ReadlineError::Eof) => {
                println!();
                break;
            }
            Err(error) => return Err(Box::new(error)),
        }
    }
    Ok(())
}

#[derive(Clone, Copy, Debug)]
struct SlashHelper;

impl Helper for SlashHelper {}

impl Completer for SlashHelper {
    type Candidate = Pair;

    fn complete(
        &self,
        line: &str,
        pos: usize,
        _ctx: &Context<'_>,
    ) -> Result<(usize, Vec<Pair>), ReadlineError> {
        let prefix = &line[..pos.min(line.len())];
        if !prefix.starts_with('/') || prefix.contains(char::is_whitespace) {
            return Ok((0, Vec::new()));
        }
        let candidates = SUPPORTED_COMMANDS
            .iter()
            .copied()
            .filter(|command| command.starts_with(prefix))
            .map(|command| Pair {
                display: format!("{:<12} {}", command, command_summary(command)),
                replacement: command.to_string(),
            })
            .collect::<Vec<_>>();
        Ok((0, candidates))
    }
}

impl Hinter for SlashHelper {
    type Hint = String;
}

impl Highlighter for SlashHelper {}

impl Validator for SlashHelper {
    fn validate(
        &self,
        _ctx: &mut ValidationContext<'_>,
    ) -> Result<ValidationResult, ReadlineError> {
        Ok(ValidationResult::Valid(None))
    }
}

fn print_repl_header(app: &App) {
    println!("{}", paint_bold("Kim CLI", kim_accent_color()));
    println!(
        "{} {}  {} {}  {} {}",
        paint_dim("Provider:"),
        paint_text(&app.config.provider),
        paint_dim("Model:"),
        paint_text(&app.config.model),
        paint_dim("Mode:"),
        paint_bold(app.mode.label(), kim_accent_color())
    );
    if app.provider_ready {
        println!(
            "{}",
            paint_dim(
                "Type /commands for the command menu. Type /mode, /code, or /chat to switch."
            )
        );
    } else {
        println!(
            "{}",
            paint_dim("Not signed in yet. Run /login ollama, /login claude, or /provider <name>.")
        );
    }
    println!();
}

fn print_repl_prompt(app: &App) -> io::Result<()> {
    print!("{}", repl_prompt(app));
    stdout().flush()
}

fn repl_prompt(app: &App) -> String {
    if app.mode == AppMode::Code {
        paint_bold("code> ", kim_accent_color())
    } else {
        paint_bold("> ", kim_accent_color())
    }
}

fn colors_enabled() -> bool {
    stdout().is_terminal() && std::env::var_os("NO_COLOR").is_none()
}

fn kim_accent_color() -> TerminalColor {
    TerminalColor::Rgb {
        r: 0xe8,
        g: 0xb8,
        b: 0x9a,
    }
}

#[allow(dead_code)]
fn kim_text_color() -> TerminalColor {
    TerminalColor::Rgb {
        r: 0x35,
        g: 0x30,
        b: 0x2a,
    }
}

fn kim_dim_color() -> TerminalColor {
    TerminalColor::Grey
}

fn paint_text(text: &str) -> String {
    text.to_string()
}

fn paint_dim(text: &str) -> String {
    paint(text, kim_dim_color())
}

fn paint_bold(text: &str, color: TerminalColor) -> String {
    if colors_enabled() {
        format!("{}", text.with(color).bold())
    } else {
        text.to_string()
    }
}

fn paint(text: &str, color: TerminalColor) -> String {
    if colors_enabled() {
        format!("{}", text.with(color))
    } else {
        text.to_string()
    }
}

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

fn choose_model_interactively(
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

fn choose_session_interactively(app: &mut App) -> Result<(), Box<dyn std::error::Error>> {
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

async fn handle_repl_input(
    app: &mut App,
    input: String,
) -> Result<bool, Box<dyn std::error::Error>> {
    if apply_mode_input(&input, app) {
        app.view = ViewState::InChat;
        print_note(&app.status);
        return Ok(false);
    }

    let input = if input.trim_start().starts_with('/') {
        input
    } else {
        prompt_with_file_references(&input)
    };
    let outcome = handle_command(&input, &mut app.config).await;
    app.provider_ready = provider_is_ready(&app.config);
    apply_repl_outcome(app, outcome).await
}

async fn apply_repl_outcome(
    app: &mut App,
    outcome: CommandOutcome,
) -> Result<bool, Box<dyn std::error::Error>> {
    match outcome {
        CommandOutcome::Exit => Ok(true),
        CommandOutcome::Info(message) => {
            print_note(&message);
            Ok(false)
        }
        CommandOutcome::NeedApiKey(provider) => {
            let prompt = format!("{provider} API key: ");
            let key = rpassword::prompt_password(prompt)?;
            let outcome = login_with_key(&provider, &key, &mut app.config).await;
            app.provider_ready = provider_is_ready(&app.config);
            Box::pin(apply_repl_outcome(app, outcome)).await
        }
        CommandOutcome::ProviderConnected(message) => {
            app.provider_ready = true;
            app.push(MessageRole::System, message.clone());
            print_note(&message);
            save_current_session(app);
            Ok(false)
        }
        CommandOutcome::Message(message) => handle_repl_message(app, message),
        CommandOutcome::OpenModelPicker(options) => {
            if io::stdin().is_terminal() {
                choose_model_interactively(app, &options)?;
            } else {
                print_model_options(&app.config.model, &options);
            }
            Ok(false)
        }
        CommandOutcome::OpenProviderPicker => {
            print_provider_options(&app.config.provider);
            Ok(false)
        }
        CommandOutcome::Compact => {
            compact_app_messages(app);
            if let Some(last) = app.messages.last() {
                print_message(last);
            }
            save_current_session(app);
            Ok(false)
        }
        CommandOutcome::SendPrompt(prompt) => stream_repl_turn(app, prompt).await,
    }
}

fn handle_repl_message(app: &mut App, message: String) -> Result<bool, Box<dyn std::error::Error>> {
    if message == "Conversation cleared." {
        app.messages.clear();
        save_current_session_allow_empty(app);
        print_note("Conversation cleared.");
        return Ok(false);
    }
    if let Some(session_id) = message.strip_prefix("__KIM_RESUME_SESSION__:") {
        app.resume_session(session_id);
        app.view = ViewState::InChat;
        print_recent_transcript(app);
        return Ok(false);
    }
    match message.as_str() {
        "__KIM_REFRESH_SESSIONS__" => {
            app.refresh_sessions();
            if io::stdin().is_terminal() {
                choose_session_interactively(app)?;
            } else {
                print_session_list(&app.sessions);
            }
        }
        "__KIM_TOGGLE_MODE__" => {
            app.toggle_mode();
            app.view = ViewState::InChat;
            print_note(&format!("mode -> {}", app.mode.label()));
        }
        "__KIM_COMPACT__" => {
            compact_app_messages(app);
            if let Some(last) = app.messages.last() {
                print_message(last);
            }
            save_current_session(app);
        }
        _ => {
            app.push(MessageRole::System, message.clone());
            print_message(&UiMessage {
                role: MessageRole::System,
                content: message,
            });
            save_current_session(app);
        }
    }
    Ok(false)
}

async fn stream_repl_turn(
    app: &mut App,
    prompt: String,
) -> Result<bool, Box<dyn std::error::Error>> {
    app.view = ViewState::InChat;
    app.push(MessageRole::User, prompt.clone());
    let is_local_agent = app.config.provider != "desktop" && app.mode != AppMode::Code;
    if !is_local_agent {
        save_current_session(app);
    }

    let history = app.chat_history();
    let config = app.config.clone();
    let code_mode = app.mode == AppMode::Code;
    let session_id = app.current_session_id.clone();
    let spawn_session_id = session_id.clone();
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<AppEvent>();
    tokio::spawn(async move {
        stream_kim_request(&config, &history, code_mode, &spawn_session_id, tx).await;
    });

    let started = Instant::now();
    let mut assistant = String::new();
    let mut printed_answer_label = false;
    let mut printed_thinking = false;
    let mut last_tool_line = String::new();
    let mut bridge_used = false;

    while let Some(event) = rx.recv().await {
        match event {
            AppEvent::TextChunk(chunk) => {
                if !printed_answer_label {
                    if printed_thinking {
                        println!();
                    }
                    print!("{}", paint_bold("Kim: ", kim_accent_color()));
                    stdout().flush()?;
                    printed_answer_label = true;
                }
                print!("{}", paint_text(&chunk));
                stdout().flush()?;
                assistant.push_str(&chunk);
            }
            AppEvent::ThoughtChunk(chunk) => {
                if !printed_thinking && !printed_answer_label {
                    printed_thinking = true;
                }
                print!("{}", paint_dim(&chunk));
                stdout().flush()?;
            }
            AppEvent::ToolEvent { verb, target } => {
                let line = format!("{verb}: {target}");
                if line != last_tool_line {
                    if printed_answer_label && !assistant.ends_with('\n') {
                        println!();
                    }
                    print_note(&line);
                    last_tool_line = line;
                }
            }
            AppEvent::Done(used_bridge) => {
                bridge_used = used_bridge;
                break;
            }
            AppEvent::Err(error) => {
                if printed_answer_label && !assistant.ends_with('\n') {
                    println!();
                }
                app.push(MessageRole::Error, error.clone());
                print_message(&UiMessage {
                    role: MessageRole::Error,
                    content: error,
                });
                save_current_session(app);
                return Ok(false);
            }
        }
    }

    if is_local_agent {
        if let Some(home) = dirs::home_dir() {
            let session_file = home.join(".kim").join("sessions").join(format!("{}.jsonl", session_id));
            if session_file.exists() {
                if let Ok(new_messages) = sessions::load_session_messages(&session_file) {
                    let old_len = app.messages.len();
                    app.messages = new_messages;
                    for msg in app.messages.iter().skip(old_len) {
                        if msg.role == MessageRole::Assistant {
                            if !printed_answer_label {
                                if printed_thinking {
                                    println!();
                                }
                                print!("{}", paint_bold("Kim: ", kim_accent_color()));
                                stdout().flush()?;
                                printed_answer_label = true;
                            }
                            println!("{}", msg.content);
                        }
                    }
                }
            }
        }
        if !printed_answer_label {
            println!("Kim: (no response)");
        }
    } else {
        if printed_answer_label {
            if !assistant.ends_with('\n') {
                println!();
            }
        } else {
            println!("Kim: (no response)");
        }

        if !assistant.trim().is_empty() {
            app.push(MessageRole::Assistant, assistant);
            save_current_session(app);
        }
    }

    let via = if bridge_used { " via Kim desktop" } else { "" };
    print_note(&format!(
        "done in {}{via}",
        format_repl_elapsed(started.elapsed())
    ));
    Ok(false)
}

fn format_repl_elapsed(duration: Duration) -> String {
    let secs = duration.as_secs();
    if secs < 60 {
        format!("{secs}s")
    } else {
        format!("{}m {:02}s", secs / 60, secs % 60)
    }
}

fn print_recent_transcript(app: &App) {
    let visible = app.messages.iter().rev().take(12).collect::<Vec<_>>();
    for message in visible.into_iter().rev() {
        print_message(message);
    }
    if !app.messages.is_empty() {
        println!();
    }
}

fn print_message(message: &UiMessage) {
    let label = match message.role {
        MessageRole::User => "You",
        MessageRole::Assistant => "Kim",
        MessageRole::System => "Note",
        MessageRole::Error => "Error",
        MessageRole::Reasoning => "Thinking",
    };
    for (index, line) in message.content.lines().enumerate() {
        if index == 0 {
            println!(
                "{} {}",
                paint_bold(&format!("{label}:"), kim_accent_color()),
                paint_text(line)
            );
        } else {
            println!("{}  {}", " ".repeat(label.len()), paint_text(line));
        }
    }
    if message.content.lines().next().is_none() {
        println!("{}", paint_bold(&format!("{label}:"), kim_accent_color()));
    }
}

fn print_note(message: &str) {
    println!("{}", paint_dim(message));
}

fn print_session_list(sessions: &[SessionEntry]) {
    if sessions.is_empty() {
        println!("{}", paint_dim("No saved sessions yet."));
        return;
    }
    println!("{}", paint_bold("Saved sessions:", kim_accent_color()));
    for (index, session) in sessions.iter().enumerate() {
        println!(
            "  {:>2}. {}  {}",
            index + 1,
            paint_text(&session.label),
            paint_dim(&format!("({})", session.id))
        );
    }
    println!("{}", paint_dim("Resume with: /resume <session-id>"));
}

fn print_model_options(current: &str, options: &[String]) {
    if options.is_empty() {
        println!(
            "{}",
            paint_dim("No model options found. Set one with /model <name>.")
        );
        return;
    }
    println!("{}", paint_bold("Available models:", kim_accent_color()));
    for model in options {
        let marker = if model == current { "*" } else { " " };
        println!("  {marker} {}", paint_text(model));
    }
    println!("{}", paint_dim("Set one with: /model <name>"));
}

fn print_provider_options(current: &str) {
    println!("{}", paint_bold("Providers:", kim_accent_color()));
    for provider in provider::PROVIDERS {
        let marker = if provider.name == current { "*" } else { " " };
        println!("  {marker} {}", paint_text(provider.name));
    }
    println!("{}", paint_dim("Set one with: /provider <name>"));
}

fn save_current_session(app: &App) {
    if !app
        .messages
        .iter()
        .any(|message| matches!(message.role, MessageRole::User | MessageRole::Assistant))
    {
        return;
    }
    save_current_session_allow_empty(app);
}

fn save_current_session_allow_empty(app: &App) {
    if let Err(error) = save_session_messages(&app.current_session_id, &app.messages) {
        eprintln!("Could not save session: {error}");
    }
}

/* ===========================================================
main loop — non-blocking so the streaming task can run
=========================================================== */

fn provider_is_ready(config: &KimConfig) -> bool {
    match config.provider.as_str() {
        // Keyless providers: considered ready only after explicit /login check.
        // We can't know at startup if the server is running without a network call,
        // so we only set ready=true via ProviderConnected. However, if user already
        // had ollama/desktop set and there's no key to check, we optimistically
        // return true so existing sessions don't show "not signed in" every launch.
        "ollama" | "desktop" => true,
        p => config.api_keys.contains_key(p),
    }
}

async fn run_app(
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    resume_id: Option<&str>,
) -> Result<String, Box<dyn std::error::Error>> {
    let mut app = App::new(KimConfig::load(), resume_id);
    let mut viewport_height = terminal_height();
    app.provider_ready = provider_is_ready(&app.config);
    let welcome = if app.provider_ready {
        format!(
            "Kim CLI v1. Signed in as {} · {}. Pick New chat or a session.",
            app.config.provider, app.config.model
        )
    } else {
        "Kim CLI v1. Not signed in yet. Run /login to connect to a provider, then start chatting."
            .to_string()
    };
    app.push(MessageRole::System, welcome);

    'main: loop {
        drain_events(&mut app);

        let desired_viewport_height = desired_inline_viewport_height(&app);
        if desired_viewport_height != viewport_height {
            *terminal = enter_terminal_with_height(desired_viewport_height)?;
            viewport_height = desired_viewport_height;
        }

        sync_inline_scrollback(&mut app, terminal, viewport_height)?;

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
                _ => {}
            }
        }

        // Yield to let the streaming task produce more chunks.
        tokio::time::sleep(Duration::from_millis(if app.busy { 33 } else { 60 })).await;
    }
    Ok(app.current_session_id)
}

fn sync_inline_scrollback(
    app: &mut App,
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    _viewport_height: u16,
) -> io::Result<()> {
    if !uses_compact_chat_viewport(app) {
        return Ok(());
    }

    terminal.autoresize()?;
    let size = terminal.size()?;
    if size.width == 0 {
        return Ok(());
    }

    if app.scrollback_session_id != app.current_session_id {
        app.scrollback_session_id
            .clone_from(&app.current_session_id);
        app.scrollback_committed_rows = 0;
        app.scrollback_width = 0;
    }

    let rows = ui::chat_visual_rows(app, size.width);
    let overflow_rows = rows.len();

    if app.scrollback_width != size.width {
        if app.scrollback_width != 0 {
            app.scrollback_committed_rows = overflow_rows;
            app.scrollback_width = size.width;
            return Ok(());
        }
        app.scrollback_width = size.width;
    }

    if overflow_rows < app.scrollback_committed_rows {
        app.scrollback_committed_rows = overflow_rows;
        return Ok(());
    }

    if overflow_rows == app.scrollback_committed_rows {
        return Ok(());
    }

    let rows_to_insert = &rows[app.scrollback_committed_rows..overflow_rows];
    insert_rows_before_viewport(terminal, rows_to_insert)?;
    app.scrollback_committed_rows = overflow_rows;
    Ok(())
}

fn desired_inline_viewport_height(app: &App) -> u16 {
    if uses_compact_chat_viewport(app) {
        1
    } else {
        terminal_height()
    }
}

fn uses_compact_chat_viewport(app: &App) -> bool {
    let _ = app;
    false
}

fn insert_rows_before_viewport(
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    rows: &[String],
) -> io::Result<()> {
    for chunk in rows.chunks(usize::from(u16::MAX)) {
        let height = u16::try_from(chunk.len()).unwrap_or(u16::MAX);
        terminal.insert_before(height, |buffer| {
            let max_width = usize::from(buffer.area.width);
            for (row_index, row) in chunk.iter().enumerate() {
                let y = u16::try_from(row_index).unwrap_or(u16::MAX);
                buffer.set_stringn(
                    0,
                    y,
                    row.as_str(),
                    max_width,
                    ratatui::style::Style::default(),
                );
            }
        })?;
    }
    Ok(())
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
            if let Some(last) = app.messages.last_mut() {
                if last.role == MessageRole::Reasoning {
                    last.content.push_str(&chunk);
                } else {
                    app.push(MessageRole::Reasoning, chunk.clone());
                }
            } else {
                app.push(MessageRole::Reasoning, chunk.clone());
            }
            if app.follow {
                app.scroll = 0;
            }

            // Update app.trace for TUI panel
            let trimmed = chunk.trim();
            if trimmed.starts_with("[PLAN]") {
                if let Ok(val) = serde_json::from_str::<serde_json::Value>(&trimmed["[PLAN]".len()..]) {
                    if let Some(steps) = val.get("steps").and_then(serde_json::Value::as_array) {
                        let items = steps.iter().filter_map(|s| s.as_str().map(|t| thinking::PlanItem {
                            text: t.to_string(),
                            status: thinking::Status::Pending,
                        })).collect::<Vec<_>>();
                        app.trace.push(TraceItem::Plan(thinking::Plan {
                            title: "Plan".to_string(),
                            items,
                        }));
                    }
                }
            } else if trimmed.starts_with("[STEP]") {
                if let Ok(val) = serde_json::from_str::<serde_json::Value>(&trimmed["[STEP]".len()..]) {
                    let idx = val.get("index").and_then(serde_json::Value::as_u64).unwrap_or(1) as usize;
                    if let Some(TraceItem::Plan(plan)) = app.trace.iter_mut().rev().find(|it| matches!(it, TraceItem::Plan(_))) {
                        for (i, item) in plan.items.iter_mut().enumerate() {
                            if i + 1 < idx {
                                item.status = thinking::Status::Done;
                            } else if i + 1 == idx {
                                item.status = thinking::Status::Active;
                            } else {
                                item.status = thinking::Status::Pending;
                            }
                        }
                    }
                }
            } else if trimmed.starts_with("[DONE]") {
                if let Ok(val) = serde_json::from_str::<serde_json::Value>(&trimmed["[DONE]".len()..]) {
                    let idx = val.get("index").and_then(serde_json::Value::as_u64).unwrap_or(1) as usize;
                    if let Some(TraceItem::Plan(plan)) = app.trace.iter_mut().rev().find(|it| matches!(it, TraceItem::Plan(_))) {
                        if let Some(item) = plan.items.get_mut(idx - 1) {
                            item.status = thinking::Status::Done;
                        }
                    }
                }
            } else {
                let clean_chunk = if trimmed.starts_with("[STATUS]") {
                    trimmed["[STATUS]".len()..].trim().to_string()
                } else {
                    chunk.clone()
                };
                if !clean_chunk.is_empty() {
                    if let Some(TraceItem::Thought(last)) = app.trace.last_mut() {
                        last.push_str(&clean_chunk);
                    } else {
                        app.trace.push(TraceItem::Thought(clean_chunk));
                    }
                }
            }
        }
        AppEvent::ToolEvent { verb, target } => {
            let line = format!("\n→ {verb}: {target}");
            if let Some(last) = app.messages.last_mut() {
                if last.role == MessageRole::Reasoning {
                    last.content.push_str(&line);
                } else {
                    app.push(MessageRole::Reasoning, format!("→ {verb}: {target}"));
                }
            } else {
                app.push(MessageRole::Reasoning, format!("→ {verb}: {target}"));
            }
            if app.follow {
                app.scroll = 0;
            }

            // Update app.trace for TUI panel
            app.trace.push(TraceItem::Tool {
                verb: verb.clone(),
                target: target.clone(),
            });
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
    _terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
) -> Result<bool, Box<dyn std::error::Error>> {
    if app.model_picker_open {
        return Ok(handle_model_picker_key(key, app));
    }
    if app.provider_picker_open {
        return Ok(handle_provider_picker_key(key, app));
    }
    match key.code {
        KeyCode::Enter => {
            if key.kind != KeyEventKind::Press {
                return Ok(false);
            }
            app.reset_ctrl_c();
            // Secure input mode: submit API key without leaving the TUI.
            if app.secure_input {
                let key_text = std::mem::take(&mut app.input);
                app.secure_input = false;
                let provider = std::mem::take(&mut app.secure_input_provider);
                app.status = format!("validating {provider} key…");
                let outcome = login_with_key(&provider, &key_text, &mut app.config).await;
                let result = apply_outcome(app, outcome).await;
                app.provider_ready = provider_is_ready(&app.config);
                return result;
            }
            if app.complete_slash_selection() {
                return Ok(false);
            }
            let input = std::mem::take(&mut app.input);
            app.history_cursor = None;
            if !input.trim().is_empty() {
                app.push_history(input.clone());
            }
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
            let input = if input.trim_start().starts_with('/') {
                input
            } else {
                prompt_with_file_references(&input)
            };
            let outcome = handle_command(&input, &mut app.config).await;
            let result = apply_outcome(app, outcome).await;
            app.provider_ready = provider_is_ready(&app.config);
            result
        }
        KeyCode::Esc => {
            // Cancel secure input mode.
            if app.secure_input {
                app.secure_input = false;
                app.secure_input_provider = String::new();
                app.input.clear();
                app.status = "login cancelled".to_string();
                return Ok(false);
            }
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
        KeyCode::Left => {
            app.reset_ctrl_c();
            app.clamp_cursor();
            if app.input_cursor > 0 {
                app.input_cursor -= 1;
            }
            Ok(false)
        }
        KeyCode::Right => {
            app.reset_ctrl_c();
            app.clamp_cursor();
            if app.input_cursor < app.input.chars().count() {
                app.input_cursor += 1;
            }
            Ok(false)
        }
        KeyCode::Backspace => {
            app.reset_ctrl_c();
            app.allow_empty_session_open = false;
            app.history_cursor = None;
            app.clamp_cursor();
            if app.input_cursor > 0 {
                app.input_cursor -= 1;
                let byte_idx = app.cursor_byte_idx();
                app.input.remove(byte_idx);
            }
            app.sync_slash_selection();
            Ok(false)
        }
        KeyCode::Delete => {
            app.reset_ctrl_c();
            app.allow_empty_session_open = false;
            app.history_cursor = None;
            app.clamp_cursor();
            if app.input_cursor < app.input.chars().count() {
                let byte_idx = app.cursor_byte_idx();
                app.input.remove(byte_idx);
            }
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
            app.history_cursor = None;
            app.clamp_cursor();
            let byte_idx = app.cursor_byte_idx();
            app.input.insert(byte_idx, char);
            app.input_cursor += 1;
            app.sync_slash_selection();
            if app.view == ViewState::SessionMenu && !app.input.starts_with('/') {
                app.status = "press Enter on New chat first, or type /command".to_string();
            }
            Ok(false)
        }
        KeyCode::Up => {
            app.reset_ctrl_c();
            let is_scroll_modifier = key.modifiers.contains(KeyModifiers::SHIFT)
                || key.modifiers.contains(KeyModifiers::CONTROL)
                || key.modifiers.contains(KeyModifiers::ALT);

            if is_scroll_modifier {
                if app.follow {
                    app.follow = false;
                    app.scroll = app.last_max_scroll.get();
                }
                app.scroll = app.scroll.saturating_sub(3);
            } else if app.history_cursor.is_some() {
                // Already in history — keep going regardless of what input contains.
                app.history_up();
            } else if app.input.starts_with('/') && !app.slash_matches().is_empty() {
                app.move_slash_selection(-1);
            } else if app.view == ViewState::SessionMenu && app.input.is_empty() {
                app.allow_empty_session_open = true;
                app.move_session_selection(-1);
            } else if app.view == ViewState::InChat && !app.input_history.is_empty() {
                app.history_up();
            } else {
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
            let is_scroll_modifier = key.modifiers.contains(KeyModifiers::SHIFT)
                || key.modifiers.contains(KeyModifiers::CONTROL)
                || key.modifiers.contains(KeyModifiers::ALT);

            if is_scroll_modifier {
                if !app.follow {
                    app.scroll = app.scroll.saturating_add(3);
                    if app.scroll >= app.last_max_scroll.get() {
                        app.follow = true;
                        app.scroll = 0;
                    }
                }
            } else if app.history_cursor.is_some() {
                // Already in history — keep going regardless of what input contains.
                app.history_down();
            } else if app.input.starts_with('/') && !app.slash_matches().is_empty() {
                app.move_slash_selection(1);
            } else if app.view == ViewState::SessionMenu && app.input.is_empty() {
                app.allow_empty_session_open = true;
                app.move_session_selection(1);
            } else if !app.follow {
                app.scroll = app.scroll.saturating_add(3);
                if app.scroll >= app.last_max_scroll.get() {
                    app.follow = true;
                    app.scroll = 0;
                }
            }
            Ok(false)
        }
        KeyCode::PageUp => {
            app.reset_ctrl_c();
            if app.view == ViewState::InChat {
                let step = app.last_max_scroll.get().min(20).max(5);
                if app.follow {
                    app.follow = false;
                    app.scroll = app.last_max_scroll.get();
                }
                app.scroll = app.scroll.saturating_sub(step);
            }
            Ok(false)
        }
        KeyCode::PageDown => {
            app.reset_ctrl_c();
            if app.view == ViewState::InChat && !app.follow {
                let step = app.last_max_scroll.get().min(20).max(5);
                app.scroll = app.scroll.saturating_add(step);
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
        CommandOutcome::Info(message) => {
            app.status = message;
            Ok(false)
        }
        CommandOutcome::NeedApiKey(provider) => {
            app.secure_input = true;
            app.secure_input_provider = provider.clone();
            app.input.clear();
            app.status = format!("enter {provider} API key and press Enter · Esc to cancel");
            Ok(false)
        }
        CommandOutcome::ProviderConnected(message) => {
            app.provider_ready = true;
            app.push(MessageRole::System, message);
            app.status = format!("signed in · {}", app.config.provider);
            Ok(false)
        }
        CommandOutcome::Message(message) => {
            if message == "Conversation cleared." {
                app.messages.clear();
            } else if let Some(session_id) = message.strip_prefix("__KIM_RESUME_SESSION__:") {
                app.resume_session(session_id);
                return Ok(false);
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
        CommandOutcome::OpenProviderPicker => {
            app.open_provider_picker();
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
            app.thinking_start = Some(Instant::now());
            app.status = "working".to_string();

            let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<AppEvent>();
            app.event_rx = Some(rx);

            let config = app.config.clone();
            let code_mode = app.mode == AppMode::Code;

            let session_id = app.current_session_id.clone();
            let handle = tokio::spawn(async move {
                stream_kim_request(&config, &history, code_mode, &session_id, tx).await;
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
            max_estimated_tokens: 10_000,
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
        MessageRole::Error | MessageRole::Reasoning => return None,
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

fn handle_provider_picker_key(key: KeyEvent, app: &mut App) -> bool {
    match key.code {
        KeyCode::Esc => {
            app.close_overlays();
            false
        }
        KeyCode::Enter => {
            app.confirm_provider_selection();
            false
        }
        KeyCode::Up => {
            app.move_provider_selection(-1);
            false
        }
        KeyCode::Down => {
            app.move_provider_selection(1);
            false
        }
        KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            app.close_overlays();
            false
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

/* ===========================================================
terminal lifecycle
=========================================================== */

static RAW_MODE_ACTIVE: AtomicBool = AtomicBool::new(false);

fn install_panic_hook() {
    let original = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        if RAW_MODE_ACTIVE.load(Ordering::SeqCst) {
            let _ = disable_raw_mode();
            let _ = execute!(stdout(), DisableBracketedPaste, crossterm::cursor::Show);
        }
        original(info);
    }));
}

fn enter_terminal() -> io::Result<Terminal<CrosstermBackend<std::io::Stdout>>> {
    enter_terminal_with_height(terminal_height())
}

fn enter_terminal_with_height(
    viewport_height: u16,
) -> io::Result<Terminal<CrosstermBackend<std::io::Stdout>>> {
    enable_raw_mode()?;
    RAW_MODE_ACTIVE.store(true, Ordering::SeqCst);
    execute!(stdout(), EnableBracketedPaste)?;
    Terminal::with_options(
        CrosstermBackend::new(stdout()),
        ratatui::TerminalOptions {
            viewport: ratatui::Viewport::Inline(viewport_height),
        },
    )
}

fn terminal_height() -> u16 {
    let (_, height) = crossterm::terminal::size().unwrap_or((80, 24));
    height
}

fn leave_terminal(terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>) -> io::Result<()> {
    RAW_MODE_ACTIVE.store(false, Ordering::SeqCst);
    disable_raw_mode()?;
    execute!(terminal.backend_mut(), DisableBracketedPaste)?;
    terminal.show_cursor()
}

fn help_text() -> &'static str {
    "Kim terminal CLI\n\nUsage:\n  kim                    Launch the Claude-style prompt UI\n  kim --resume <id>      Resume a Kim session\n  kim --resume latest    Resume the newest saved session\n  kim --help             Show this help\n\nInside Kim, type /help for commands and /login to connect a provider."
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

mod commands;
mod config;
mod provider;
mod sessions;
mod theme;
mod ui;

use std::io::{self, stdout};
use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use commands::{handle_command, CommandOutcome, SUPPORTED_COMMANDS};
use config::KimConfig;
use crossterm::event::{self, Event, KeyCode, KeyEvent, KeyModifiers};
use crossterm::execute;
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use provider::{send_chat, ChatMessage};
use ratatui::backend::CrosstermBackend;
use ratatui::Terminal;
use sessions::{
    discover_project_sessions, discover_sessions, find_session_by_id, load_session_messages,
    SessionEntry,
};

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

#[derive(Debug)]
#[allow(clippy::struct_excessive_bools)]
pub struct App {
    pub config: KimConfig,
    pub messages: Vec<UiMessage>,
    pub input: String,
    pub status: String,
    pub scroll: u16,
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
        if let Some(index) = self
            .sessions
            .iter()
            .position(|entry| entry.path == session.path)
        {
            self.selected_session = index;
        }
        match load_session_messages(&session.path) {
            Ok(messages) => {
                self.messages = messages;
                self.view = ViewState::InChat;
                self.scroll =
                    u16::try_from(self.messages.len().saturating_mul(3)).unwrap_or(u16::MAX);
                self.status = format!("resumed {}", session.label);
            }
            Err(error) => self.push(MessageRole::Error, error),
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
            .filter(|command| command.starts_with(typed))
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
            .position(|model| model == &self.config.model)
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

    fn visible_messages(&self) -> impl Iterator<Item = &UiMessage> {
        self.messages
            .iter()
            .skip(self.messages.len().saturating_sub(120))
    }

    fn push(&mut self, role: MessageRole, content: impl Into<String>) {
        self.messages.push(UiMessage {
            role,
            content: content.into(),
        });
        self.scroll = u16::try_from(self.messages.len().saturating_mul(3)).unwrap_or(u16::MAX);
    }

    fn chat_history(&self) -> Vec<ChatMessage> {
        self.messages
            .iter()
            .filter_map(|message| match message.role {
                MessageRole::User => Some(ChatMessage {
                    role: "user".to_string(),
                    content: message.content.clone(),
                }),
                MessageRole::Assistant => Some(ChatMessage {
                    role: "assistant".to_string(),
                    content: message.content.clone(),
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
            self.selected_session = self.selected_session.min(self.sessions.len() - 1);
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
                self.scroll =
                    u16::try_from(self.messages.len().saturating_mul(3)).unwrap_or(u16::MAX);
                self.current_session_id.clone_from(&session.id);
                self.status = format!("opened {}", session.label);
            }
            Err(error) => self.push(MessageRole::Error, error),
        }
    }

    fn ensure_current_session_listed(&mut self, title: &str) {
        if self
            .sessions
            .iter()
            .any(|session| session.id == self.current_session_id)
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
        self.view = ViewState::InChat;
        self.status = format!("new Kim {} chat · type your message", self.mode.label());
    }

    fn show_session_menu(&mut self) {
        self.refresh_sessions();
        self.view = ViewState::SessionMenu;
        self.input.clear();
        self.status = "session menu · choose New chat or a saved session".to_string();
    }
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    if args
        .iter()
        .any(|arg| matches!(arg.as_str(), "--help" | "-h"))
    {
        println!("{}", help_text());
        return Ok(());
    }
    if args
        .iter()
        .any(|arg| matches!(arg.as_str(), "--version" | "-V"))
    {
        println!("kim {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    let resume_id = args
        .windows(2)
        .find_map(|window| (window[0] == "--resume").then_some(window[1].as_str()));

    let mut terminal = enter_terminal()?;
    let result = run_app(&mut terminal, resume_id).await;
    leave_terminal(&mut terminal)?;
    match result {
        Ok(session_id) => {
            println!("Resume this Kim session with: kim --resume {session_id}");
        }
        Err(error) => {
            eprintln!("kim error: {error}");
            std::process::exit(1);
        }
    }
    Ok(())
}

async fn run_app(
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    resume_id: Option<&str>,
) -> Result<String, Box<dyn std::error::Error>> {
    let mut app = App::new(KimConfig::load(), resume_id);
    app.push(
        MessageRole::System,
        "Kim CLI v1. Pick New chat or a session. /login signs into Ollama; /mode switches chat/code.",
    );

    loop {
        terminal.draw(|frame| ui::draw(frame, &app))?;
        if !event::poll(Duration::from_millis(60))? {
            continue;
        }
        if let Event::Key(key) = event::read()? {
            if handle_key(key, &mut app, terminal).await? {
                break;
            }
        }
    }
    Ok(app.current_session_id)
}

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
            app.reset_ctrl_c();
            if app.complete_slash_selection() {
                return Ok(false);
            }
            let input = std::mem::take(&mut app.input);
            if input.trim().is_empty() {
                if app.view == ViewState::SessionMenu || app.allow_empty_session_open {
                    app.open_selected_session();
                    app.allow_empty_session_open = false;
                } else {
                    app.status =
                        "type a message, /command, or use ↑/↓ then Enter to open a session"
                            .to_string();
                    app.allow_empty_session_open = true;
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
            if input.trim().eq_ignore_ascii_case("/login") {
                app.push(MessageRole::System, "Checking Ollama sign-in…");
                app.status = "running ollama signin".to_string();
            }
            if is_interactive_secret_login(&input) {
                suspend_terminal(terminal)?;
                println!("Kim login\n");
                let outcome = handle_command(&input, &mut app.config).await;
                println!("\nPress enter to return to Kim…");
                let mut pause = String::new();
                let _ = io::stdin().read_line(&mut pause);
                resume_terminal(terminal)?;
                return apply_outcome(app, outcome).await;
            }
            let outcome = handle_command(&input, &mut app.config).await;
            apply_outcome(app, outcome).await
        }
        KeyCode::Esc => {
            if !app.close_overlays() {
                return Ok(true);
            }
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
            } else if app.input.is_empty() {
                app.allow_empty_session_open = true;
                app.move_session_selection(-1);
            } else {
                app.scroll = app.scroll.saturating_sub(3);
            }
            Ok(false)
        }
        KeyCode::Down => {
            app.reset_ctrl_c();
            if app.input.starts_with('/') {
                app.move_slash_selection(1);
            } else if app.input.is_empty() {
                app.allow_empty_session_open = true;
                app.move_session_selection(1);
            } else {
                app.scroll = app.scroll.saturating_add(3);
            }
            Ok(false)
        }
        _ => Ok(false),
    }
}

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
        CommandOutcome::SendPrompt(prompt) => {
            if app.view == ViewState::SessionMenu {
                app.status = "choose New chat or open a session before sending".to_string();
                return Ok(false);
            }
            app.push(MessageRole::User, prompt);
            if let Some(last_user) = app
                .messages
                .iter()
                .rev()
                .find(|message| message.role == MessageRole::User)
                .map(|message| message.content.clone())
            {
                app.ensure_current_session_listed(&last_user);
            }
            app.busy = true;
            app.status = "thinking".to_string();
            let started = Instant::now();
            let history = app.chat_history();
            match send_chat(&app.config, &history).await {
                Ok(reply) => {
                    app.push(MessageRole::Assistant, reply);
                    app.status = format!("worked for {:.1}s", started.elapsed().as_secs_f32());
                }
                Err(error) => {
                    app.push(MessageRole::Error, error);
                    app.status = "provider error".to_string();
                }
            }
            app.busy = false;
            Ok(false)
        }
    }
}

fn apply_mode_input(input: &str, app: &mut App) -> bool {
    let trimmed = input.trim().to_ascii_lowercase();
    match trimmed.as_str() {
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

fn is_interactive_secret_login(input: &str) -> bool {
    let trimmed = input.trim().to_ascii_lowercase();
    trimmed.starts_with("/login ")
        && !matches!(
            trimmed.trim_start_matches("/login").trim(),
            "" | "ollama" | "desktop"
        )
}

fn enter_terminal() -> io::Result<Terminal<CrosstermBackend<std::io::Stdout>>> {
    enable_raw_mode()?;
    execute!(stdout(), EnterAlternateScreen)?;
    let backend = CrosstermBackend::new(stdout());
    Terminal::new(backend)
}

fn leave_terminal(terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>) -> io::Result<()> {
    disable_raw_mode()?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen)?;
    terminal.show_cursor()
}

fn suspend_terminal(terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>) -> io::Result<()> {
    disable_raw_mode()?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen)?;
    terminal.show_cursor()
}

fn resume_terminal(terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>) -> io::Result<()> {
    enable_raw_mode()?;
    execute!(terminal.backend_mut(), EnterAlternateScreen)?;
    terminal.clear()
}

fn help_text() -> &'static str {
    "Kim terminal CLI\n\nUsage:\n  kim                    Launch the terminal UI\n  kim --resume <id>      Resume a Kim session\n  kim --help             Show this help\n\nInside Kim, type /help for commands and /login to connect Ollama."
}

fn new_session_id() -> String {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or(0);
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

mod commands;
mod config;
mod provider;
mod sessions;

use std::io::{self, stdout, IsTerminal, Read, Write};
use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use commands::{
    command_summary, handle_command, login_with_key, CommandOutcome, SUPPORTED_COMMANDS,
};
use config::KimConfig;
use crossterm::cursor::{MoveToColumn, MoveUp};
use crossterm::event::{self, Event, KeyCode, KeyEventKind};
use crossterm::execute;
use crossterm::style::{Color as TerminalColor, Stylize};
use crossterm::terminal::{disable_raw_mode, enable_raw_mode, Clear, ClearType};
use provider::{provider_info, stream_kim_request, AppEvent, ChatMessage};
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

#[derive(Debug)]
enum CliCommand {
    ShowHelp,
    ShowVersion,
    Doctor,
    Oneshot {
        mode: AppMode,
        prompt: Option<String>,
    },
    Repl {
        resume_id: Option<String>,
    },
}

fn parse_cli_args(args: &[String]) -> CliCommand {
    if args.iter().any(|a| matches!(a.as_str(), "--help" | "-h")) {
        return CliCommand::ShowHelp;
    }
    if args
        .iter()
        .any(|a| matches!(a.as_str(), "--version" | "-V"))
    {
        return CliCommand::ShowVersion;
    }
    match args.first().map(String::as_str) {
        Some("doctor") => CliCommand::Doctor,
        Some(sub @ "chat") | Some(sub @ "code") => {
            let mode = if sub == "code" {
                AppMode::Code
            } else {
                AppMode::Chat
            };
            let rest = &args[1..];
            let prompt = if rest.is_empty() {
                None
            } else {
                Some(rest.join(" "))
            };
            CliCommand::Oneshot { mode, prompt }
        }
        _ => {
            let resume_id = args
                .windows(2)
                .find_map(|w| (w[0] == "--resume").then_some(w[1].clone()));
            CliCommand::Repl { resume_id }
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UiMessage {
    pub role: MessageRole,
    pub content: String,
}

pub struct App {
    pub config: KimConfig,
    pub messages: Vec<UiMessage>,
    pub sessions: Vec<SessionEntry>,
    pub selected_session: usize,
    pub current_session_id: String,
    pub ctrl_c_armed: bool,
    pub mode: AppMode,
    pub view: ViewState,
    pub provider_ready: bool,
    pub status: String,
}

impl App {
    fn new(config: KimConfig, resume_id: Option<&str>) -> Self {
        let mut app = Self {
            current_session_id: new_session_id(),
            config,
            messages: Vec::new(),
            sessions: discover_sessions(),
            selected_session: 0,
            ctrl_c_armed: false,
            mode: AppMode::Chat,
            view: ViewState::SessionMenu,
            provider_ready: false,
            status: "ready".to_string(),
        };
        if let Some(id) = resume_id {
            app.resume_session(id);
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
        if let Some(index) = self.sessions.iter().position(|e| e.path == session.path) {
            self.selected_session = index;
        }
        match load_session_messages(&session.path) {
            Ok(messages) => {
                self.current_session_id.clone_from(&session.id);
                self.messages = messages;
                self.view = ViewState::InChat;
                self.status = format!("resumed {}", session.label);
            }
            Err(error) => {
                self.view = ViewState::InChat;
                self.push(MessageRole::Error, error);
            }
        }
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
        if !self.sessions.is_empty() {
            self.selected_session = self.selected_session.min(self.sessions.len());
        } else {
            self.selected_session = 0;
        }
    }

    fn set_mode(&mut self, mode: AppMode) {
        self.mode = mode;
        self.refresh_sessions();
        self.view = ViewState::InChat;
        self.status = format!("kim {} mode", self.mode.label());
    }

    fn toggle_mode(&mut self) {
        let next = match self.mode {
            AppMode::Chat => AppMode::Code,
            AppMode::Code => AppMode::Chat,
        };
        self.set_mode(next);
    }

    fn start_new_chat(&mut self) {
        self.current_session_id = new_session_id();
        self.messages.clear();
        self.view = ViewState::InChat;
        self.status = format!("new Kim {} chat", self.mode.label());
    }
}

/* ===========================================================
entry point
=========================================================== */

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    match parse_cli_args(&args) {
        CliCommand::ShowHelp => println!("{}", help_text()),
        CliCommand::ShowVersion => println!("kim {}", env!("CARGO_PKG_VERSION")),
        CliCommand::Doctor => {
            let mut config = KimConfig::load();
            match handle_command("/doctor", &mut config).await {
                CommandOutcome::Message(message) | CommandOutcome::Info(message) => {
                    println!("{message}")
                }
                other => eprintln!("kim doctor returned unexpected outcome: {other:?}"),
            }
        }
        CliCommand::Oneshot { mode, prompt } => {
            let prompt = match prompt {
                Some(p) => p,
                None => {
                    if !io::stdin().is_terminal() {
                        let mut buf = String::new();
                        io::stdin().read_to_string(&mut buf)?;
                        let trimmed = buf.trim().to_string();
                        if trimmed.is_empty() {
                            eprintln!("kim {}: no prompt provided", mode.label());
                            std::process::exit(1);
                        }
                        trimmed
                    } else {
                        eprintln!("Usage: kim {} <prompt...>", mode.label());
                        std::process::exit(1);
                    }
                }
            };
            if let Err(error) = run_oneshot(mode, prompt).await {
                eprintln!("kim error: {error}");
                std::process::exit(1);
            }
        }
        CliCommand::Repl { resume_id } => match run_repl(resume_id.as_deref()).await {
            Ok(session_id) => {
                // Only advertise --resume when a session file was actually
                // written (empty REPLs and skipped saves leave none). (A7)
                let session_saved = dirs::home_dir()
                    .map(|h| {
                        h.join(".kim")
                            .join("sessions")
                            .join(format!("{session_id}.jsonl"))
                    })
                    .map(|p| p.exists())
                    .unwrap_or(false);
                if session_saved {
                    println!("Resume this Kim session with: kim --resume {session_id}");
                }
            }
            Err(error) => {
                eprintln!("kim error: {error}");
                std::process::exit(1);
            }
        },
    }
    Ok(())
}

/* ===========================================================
One-shot commands: kim chat <prompt> / kim code <prompt>
=========================================================== */

async fn run_oneshot(mode: AppMode, prompt: String) -> Result<(), Box<dyn std::error::Error>> {
    let mut app = App::new(KimConfig::load(), None);
    app.provider_ready = provider_is_ready(&app.config);

    if mode == AppMode::Code && app.config.provider == "openai" {
        eprintln!(
            "Code mode does not support OpenAI. Switch provider first: /provider ollama or /provider claude."
        );
        std::process::exit(2);
    }

    app.set_mode(mode);
    stream_repl_turn(&mut app, prompt).await?;

    if app
        .messages
        .last()
        .is_some_and(|m| m.role == MessageRole::Error)
    {
        std::process::exit(1);
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
        print_repl_prompt(app)?;
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
            paint_dim("Not signed in yet. Run /login ollama, /login claude, /login browser:claude, or /provider <name>.")
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
        CommandOutcome::SetChatMode => {
            app.set_mode(AppMode::Chat);
            print_note(&app.status);
            Ok(false)
        }
        CommandOutcome::SetCodeMode => {
            if app.config.provider == "openai" {
                app.push(
                    MessageRole::Error,
                    "Code mode does not support OpenAI. Switch provider first: /provider ollama or /provider claude.",
                );
                return Ok(false);
            }
            app.set_mode(AppMode::Code);
            print_note(&app.status);
            Ok(false)
        }
        CommandOutcome::ToggleMode => {
            let next = match app.mode {
                AppMode::Chat => AppMode::Code,
                AppMode::Code => AppMode::Chat,
            };
            if next == AppMode::Code && app.config.provider == "openai" {
                app.push(
                    MessageRole::Error,
                    "Code mode does not support OpenAI. Switch provider first: /provider ollama or /provider claude.",
                );
                return Ok(false);
            }
            app.toggle_mode();
            print_note(&format!("mode -> {}", app.mode.label()));
            Ok(false)
        }
        CommandOutcome::NewChat => {
            app.start_new_chat();
            print_note(&app.status);
            Ok(false)
        }
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
    // Persist the user turn up front so the session file exists even if the
    // request errors or is interrupted, and so resumed chats keep their history.
    // (A1/A2 — the old `is_local_agent` reload-from-file branch was vestigial:
    // nothing writes that file mid-stream, so it wiped state every turn.)
    save_current_session(app);

    let history = app.chat_history();
    let config = app.config.clone();
    let code_mode = app.mode == AppMode::Code;
    let session_id = app.current_session_id.clone();
    let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<AppEvent>();
    tokio::spawn(async move {
        stream_kim_request(&config, &history, code_mode, &session_id, tx).await;
    });

    consume_turn_events(app, rx, Instant::now(), save_current_session).await
}

/// Consume one turn's streamed events, render them, and persist the result.
/// Extracted from `stream_repl_turn` so it can be driven by a stubbed event
/// channel with an injected `save` sink in tests — no network required. (A1/A2)
async fn consume_turn_events<S>(
    app: &mut App,
    mut rx: tokio::sync::mpsc::UnboundedReceiver<AppEvent>,
    started: Instant,
    mut save: S,
) -> Result<bool, Box<dyn std::error::Error>>
where
    S: FnMut(&App),
{
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
                save(app);
                return Ok(false);
            }
        }
    }

    if printed_answer_label {
        if !assistant.ends_with('\n') {
            println!();
        }
    } else {
        println!("Kim: (no response)");
    }

    // Push the streamed assistant reply into the session and persist again, so
    // the next turn's `chat_history()` actually includes Kim's response. (A1)
    if !assistant.trim().is_empty() {
        app.push(MessageRole::Assistant, assistant);
        save(app);
    }

    let via = if bridge_used { " via Kim desktop" } else { "" };
    print_note(&format!(
        "done in {}{}",
        format_repl_elapsed(started.elapsed()),
        via
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
    for p in provider::PROVIDERS {
        let marker = if p.name == current { "*" } else { " " };
        println!("  {marker} {}", paint_text(p.name));
    }
    println!("{}", paint_dim("Set one with: /provider <name>"));
}

fn save_current_session(app: &App) {
    if !app
        .messages
        .iter()
        .any(|m| matches!(m.role, MessageRole::User | MessageRole::Assistant))
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

fn provider_is_ready(config: &KimConfig) -> bool {
    provider_is_ready_with_env(config, |key| std::env::var(key).ok())
}

fn provider_is_ready_with_env<F>(config: &KimConfig, env_var: F) -> bool
where
    F: Fn(&str) -> Option<String>,
{
    let p = config.provider.as_str();
    if p == "ollama" || p == "desktop" || provider::is_browser_provider(p) {
        return true;
    }
    if config
        .api_keys
        .get(p)
        .map(|key| !key.trim().is_empty())
        .unwrap_or(false)
    {
        return true;
    }
    provider_info(p)
        .and_then(|info| info.key_env)
        .and_then(env_var)
        .map(|key| !key.trim().is_empty())
        .unwrap_or(false)
}

/* ===========================================================
compaction — local, no runtime crate needed
=========================================================== */

fn compact_app_messages(app: &mut App) {
    let preserve = 6usize;
    let exchange_indices: Vec<usize> = app
        .messages
        .iter()
        .enumerate()
        .filter(|(_, m)| matches!(m.role, MessageRole::User | MessageRole::Assistant))
        .map(|(i, _)| i)
        .collect();

    if exchange_indices.len() <= preserve {
        app.push(
            MessageRole::System,
            "Nothing to compact yet; keeping the current conversation as-is.",
        );
        return;
    }

    let cut_at = exchange_indices[exchange_indices.len() - preserve];
    let removed = app
        .messages
        .drain(..cut_at)
        .filter(|m| matches!(m.role, MessageRole::User | MessageRole::Assistant))
        .count();
    app.messages.insert(
        0,
        UiMessage {
            role: MessageRole::System,
            content: format!("Earlier context compacted — {removed} message(s) removed."),
        },
    );
    app.push(
        MessageRole::System,
        format!("Compacted {removed} older message(s)."),
    );
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
misc helpers
=========================================================== */

fn help_text() -> &'static str {
    "Kim terminal CLI\n\nUsage:\n  kim                      Launch the interactive chat/code REPL\n  kim chat <prompt...>     Send one prompt in chat mode and exit\n  kim code <prompt...>     Send one prompt in code-agent mode and exit\n  kim doctor               Check install, providers, desktop bridge, and code mode\n  kim --resume <id>        Resume a Kim session in the REPL\n  kim --resume latest      Resume the newest saved session\n  kim --help               Show this help\n  kim --version            Show the version\n\nPipe a prompt via stdin:\n  echo 'explain this' | kim chat\n  echo 'fix the build' | kim code\n\nInside Kim, type /help for commands and /login to connect a provider."
}

fn new_session_id() -> String {
    // Counter suffix: two sessions created within the same millisecond
    // (new chat right after launch, tests) must still get distinct IDs.
    static COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_millis());
    let n = COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    format!("session-{millis}-{n}")
}

/* ===========================================================
tests
=========================================================== */

#[cfg(test)]
mod tests {
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

    use super::{
        consume_turn_events, parse_cli_args, prompt_file_references, prompt_with_file_references,
        provider_is_ready, provider_is_ready_with_env, split_shellish_tokens, App, AppEvent,
        AppMode, CliCommand, MessageRole, ViewState,
    };
    use crate::config::KimConfig;
    use std::path::{Path, PathBuf};
    use std::time::Instant;

    fn temp_session_dir() -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "kim-cli-sesstest-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("clock should be after epoch")
                .as_nanos()
        ));
        fs::create_dir_all(&dir).expect("temp session dir");
        dir
    }

    fn test_app(session_id: &str) -> App {
        App {
            config: KimConfig::default(),
            messages: Vec::new(),
            sessions: Vec::new(),
            selected_session: 0,
            current_session_id: session_id.to_string(),
            ctrl_c_armed: false,
            mode: AppMode::Chat,
            view: ViewState::InChat,
            provider_ready: true,
            status: "ready".to_string(),
        }
    }

    fn save_into<'a>(dir: &'a Path) -> impl FnMut(&App) + 'a {
        move |a: &App| {
            crate::sessions::save_session_messages_in(dir, &a.current_session_id, &a.messages)
                .expect("session save");
        }
    }

    // A1: a normal chat turn must persist BOTH the user message and the streamed
    // assistant reply, and app.messages must carry the reply for the next turn.
    #[tokio::test]
    async fn turn_persists_user_and_assistant_reply() {
        let dir = temp_session_dir();
        let mut app = test_app("persist-test-1234");
        app.push(MessageRole::User, "what is 2+2?");
        save_into(&dir)(&app); // pre-turn save (as stream_repl_turn does)

        let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<AppEvent>();
        tx.send(AppEvent::TextChunk("4".to_string())).unwrap();
        tx.send(AppEvent::Done(false)).unwrap();
        drop(tx);
        consume_turn_events(&mut app, rx, Instant::now(), save_into(&dir))
            .await
            .unwrap();

        // (b) reply is in app.messages
        assert!(app
            .messages
            .iter()
            .any(|m| m.role == MessageRole::Assistant && m.content == "4"));
        // (a) session file exists with both turns
        let file = dir.join("persist-test-1234.jsonl");
        assert!(file.exists(), "session file should be written");
        let loaded = crate::sessions::load_session_messages(&file).unwrap();
        assert!(loaded
            .iter()
            .any(|m| m.role == MessageRole::User && m.content == "what is 2+2?"));
        assert!(loaded
            .iter()
            .any(|m| m.role == MessageRole::Assistant && m.content == "4"));
        let _ = fs::remove_dir_all(&dir);
    }

    // A2: resuming a session then taking another turn must preserve old AND new
    // messages (the old reload branch wiped the new exchange every turn).
    #[tokio::test]
    async fn resumed_session_preserves_old_and_new_messages() {
        let dir = temp_session_dir();
        let sid = "resume-test-9999";

        // Turn 1
        let mut app = test_app(sid);
        app.push(MessageRole::User, "first question");
        save_into(&dir)(&app);
        let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<AppEvent>();
        tx.send(AppEvent::TextChunk("first answer".to_string())).unwrap();
        tx.send(AppEvent::Done(false)).unwrap();
        drop(tx);
        consume_turn_events(&mut app, rx, Instant::now(), save_into(&dir))
            .await
            .unwrap();

        // Resume into a fresh app from the saved file, then take turn 2.
        let file = dir.join(format!("{sid}.jsonl"));
        let resumed = crate::sessions::load_session_messages(&file).unwrap();
        let mut app2 = test_app(sid);
        app2.messages = resumed;
        app2.push(MessageRole::User, "second question");
        save_into(&dir)(&app2);
        let (tx2, rx2) = tokio::sync::mpsc::unbounded_channel::<AppEvent>();
        tx2.send(AppEvent::TextChunk("second answer".to_string())).unwrap();
        tx2.send(AppEvent::Done(false)).unwrap();
        drop(tx2);
        consume_turn_events(&mut app2, rx2, Instant::now(), save_into(&dir))
            .await
            .unwrap();

        let final_msgs = crate::sessions::load_session_messages(&file).unwrap();
        for expected in [
            "first question",
            "first answer",
            "second question",
            "second answer",
        ] {
            assert!(
                final_msgs.iter().any(|m| m.content == expected),
                "resumed session lost message: {expected}"
            );
        }
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn start_new_chat_resets_session_and_clears_messages() {
        let mut app = App::new(KimConfig::default(), None);
        let old_id = app.current_session_id.clone();
        app.push(MessageRole::User, "hello");
        app.start_new_chat();
        assert!(app.messages.is_empty(), "messages should be cleared");
        assert_ne!(
            app.current_session_id, old_id,
            "new chat must get a fresh session ID"
        );
        assert_eq!(app.view, ViewState::InChat);
    }

    #[test]
    fn browser_providers_are_ready_without_api_key() {
        for name in &[
            "browser",
            "browser:claude",
            "browser:chatgpt",
            "browser:gemini",
        ] {
            let config = KimConfig {
                provider: name.to_string(),
                ..KimConfig::default()
            };
            assert!(
                provider_is_ready(&config),
                "{name} should be ready without a key"
            );
        }
    }

    #[test]
    fn api_key_providers_require_key_to_be_ready() {
        for name in &["claude", "openai", "gemini", "deepseek"] {
            let config = KimConfig {
                provider: name.to_string(),
                ..KimConfig::default()
            };
            assert!(
                !provider_is_ready(&config),
                "{name} should not be ready without a key"
            );
        }
    }

    #[test]
    fn api_key_provider_is_ready_with_non_empty_env_key() {
        let config = KimConfig {
            provider: "claude".to_string(),
            ..KimConfig::default()
        };
        assert!(provider_is_ready_with_env(&config, |name| {
            (name == "ANTHROPIC_API_KEY").then(|| "sk-env".to_string())
        }));
    }

    #[test]
    fn api_key_provider_is_not_ready_with_blank_env_key() {
        let config = KimConfig {
            provider: "openai".to_string(),
            ..KimConfig::default()
        };
        assert!(!provider_is_ready_with_env(&config, |name| {
            (name == "OPENAI_API_KEY").then(|| "   ".to_string())
        }));
    }

    #[test]
    fn api_key_provider_is_not_ready_with_blank_saved_key() {
        let mut config = KimConfig {
            provider: "gemini".to_string(),
            ..KimConfig::default()
        };
        config
            .api_keys
            .insert("gemini".to_string(), " \n ".to_string());
        assert!(!provider_is_ready_with_env(&config, |_| None));
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

    // ── parse_cli_args tests ──────────────────────────────────────────────────

    fn args(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn parse_args_empty_launches_repl() {
        let cmd = parse_cli_args(&args(&[]));
        assert!(matches!(cmd, CliCommand::Repl { resume_id: None }));
    }

    #[test]
    fn parse_args_help_flags() {
        for flag in &["--help", "-h"] {
            let cmd = parse_cli_args(&args(&[flag]));
            assert!(
                matches!(cmd, CliCommand::ShowHelp),
                "{flag} should show help"
            );
        }
    }

    #[test]
    fn parse_args_version_flags() {
        for flag in &["--version", "-V"] {
            let cmd = parse_cli_args(&args(&[flag]));
            assert!(
                matches!(cmd, CliCommand::ShowVersion),
                "{flag} should show version"
            );
        }
    }

    #[test]
    fn parse_args_doctor() {
        let cmd = parse_cli_args(&args(&["doctor"]));
        assert!(matches!(cmd, CliCommand::Doctor));
    }

    #[test]
    fn parse_args_chat_with_prompt() {
        let cmd = parse_cli_args(&args(&["chat", "hello", "world"]));
        match cmd {
            CliCommand::Oneshot {
                mode: AppMode::Chat,
                prompt: Some(p),
            } => assert_eq!(p, "hello world"),
            other => panic!("expected Oneshot Chat, got {other:?}"),
        }
    }

    #[test]
    fn parse_args_code_with_prompt() {
        let cmd = parse_cli_args(&args(&["code", "fix", "this", "bug"]));
        match cmd {
            CliCommand::Oneshot {
                mode: AppMode::Code,
                prompt: Some(p),
            } => assert_eq!(p, "fix this bug"),
            other => panic!("expected Oneshot Code, got {other:?}"),
        }
    }

    #[test]
    fn parse_args_chat_no_prompt_is_none() {
        let cmd = parse_cli_args(&args(&["chat"]));
        assert!(matches!(
            cmd,
            CliCommand::Oneshot {
                mode: AppMode::Chat,
                prompt: None
            }
        ));
    }

    #[test]
    fn parse_args_code_no_prompt_is_none() {
        let cmd = parse_cli_args(&args(&["code"]));
        assert!(matches!(
            cmd,
            CliCommand::Oneshot {
                mode: AppMode::Code,
                prompt: None
            }
        ));
    }

    #[test]
    fn parse_args_resume_with_id() {
        let cmd = parse_cli_args(&args(&["--resume", "session-1234"]));
        match cmd {
            CliCommand::Repl {
                resume_id: Some(id),
            } => assert_eq!(id, "session-1234"),
            other => panic!("expected Repl with resume_id, got {other:?}"),
        }
    }

    #[test]
    fn parse_args_resume_latest() {
        let cmd = parse_cli_args(&args(&["--resume", "latest"]));
        match cmd {
            CliCommand::Repl {
                resume_id: Some(id),
            } => assert_eq!(id, "latest"),
            other => panic!("expected Repl with resume_id=latest, got {other:?}"),
        }
    }

    #[test]
    fn parse_args_unknown_arg_falls_through_to_repl() {
        let cmd = parse_cli_args(&args(&["--unknown-flag"]));
        assert!(matches!(cmd, CliCommand::Repl { resume_id: None }));
    }

    #[test]
    fn parse_args_chat_prompt_is_joined_with_spaces() {
        let cmd = parse_cli_args(&args(&["chat", "one", "two", "three"]));
        match cmd {
            CliCommand::Oneshot {
                prompt: Some(p), ..
            } => assert_eq!(p, "one two three"),
            other => panic!("unexpected {other:?}"),
        }
    }
}

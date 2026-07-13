mod agentic;
mod commands;
mod config;
mod file_refs;
mod markdown;
mod oneshot;
mod paint;
mod pickers;
mod provider;
mod repl_turn;
use repl_turn::consume_turn_events;
mod sessions;
mod stdin_reader;

use std::io::{self, stdout, IsTerminal, Read, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use commands::{
    command_summary, handle_command, login_with_key, CommandOutcome, SUPPORTED_COMMANDS,
};
use config::KimConfig;
use file_refs::prompt_with_file_references;
pub(crate) use file_refs::split_shellish_tokens;
use oneshot::{help_text, parse_cli_args, run_oneshot, CliCommand};
use paint::{kim_accent_color, paint_bold, paint_dim, paint_text, print_message, print_note};
use pickers::{choose_model_interactively, choose_session_interactively};
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

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UiMessage {
    pub role: MessageRole,
    pub content: String,
    /// F-E-3: the epoch-millis this message was created, if known. Set when the
    /// message is first pushed and preserved verbatim across load→save, so a
    /// long conversation keeps its real per-message times instead of having
    /// every record re-stamped to the last save instant. `None` for ephemeral
    /// (print-only) messages that are never persisted.
    pub timestamp_ms: Option<u64>,
}

impl UiMessage {
    /// Current epoch-millis, for stamping a freshly-created message.
    fn now_ms() -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |d| d.as_millis() as u64)
    }
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
    /// Set once the user confirms running Codex outside a git repo (code mode),
    /// so we don't re-prompt every turn of the session.
    pub allow_non_git_codex: bool,
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
            allow_non_git_codex: false,
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
            // F-E-3: stamp the creation time now, so it survives future saves.
            timestamp_ms: Some(UiMessage::now_ms()),
        });
    }

    fn chat_history(&self) -> Vec<ChatMessage> {
        // A20: keep the newest messages within BOTH a message-count cap (24) and a
        // crude char budget (~48k), dropping oldest first, so a few huge messages
        // can't blow the context window. The newest message is always included.
        const MAX_MSGS: usize = 24;
        const MAX_CHARS: usize = 48_000;
        let mut out: Vec<ChatMessage> = Vec::new();
        let mut total = 0usize;
        for m in self.messages.iter().rev() {
            let role = match m.role {
                MessageRole::User => "user",
                MessageRole::Assistant => "assistant",
                MessageRole::System | MessageRole::Error | MessageRole::Reasoning => continue,
            };
            if out.len() >= MAX_MSGS {
                break;
            }
            if !out.is_empty() && total + m.content.len() > MAX_CHARS {
                break;
            }
            total += m.content.len();
            out.push(ChatMessage {
                role: role.to_string(),
                content: m.content.clone(),
            });
        }
        out.reverse();
        out
    }

    fn refresh_sessions(&mut self) {
        self.sessions = match self.mode {
            AppMode::Chat => discover_sessions(),
            AppMode::Code => discover_project_sessions(),
        };
        // A17: clamp to the last valid index, not len() (which is out of range).
        if !self.sessions.is_empty() {
            self.selected_session = self.selected_session.min(self.sessions.len() - 1);
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
        CliCommand::UsageError(message) => {
            eprintln!("{message}");
            std::process::exit(2);
        }
        CliCommand::Doctor { strict } => {
            // F-E-1: `kim doctor` must exit non-zero when a required check fails
            // (and, under --strict, when any provider-specific check fails) so
            // install scripts and CI can gate on it. The old path built the
            // report via handle_command and always fell through to Ok(()) → 0.
            let config = KimConfig::load();
            let report = commands::doctor_report(&config).await;
            println!("{}", report.text);
            if commands::doctor_should_fail(report.required_ok, report.all_ok, strict) {
                std::process::exit(1);
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
                let session_saved = crate::config::kim_home()
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
Claude-style prompt loop
=========================================================== */

/// F-E-14: which providers may run in code mode. Codex only has two backends
/// here: the local `codex` binary talking to ollama through the responses proxy
/// (`ollama` / `ollama-cloud` / empty→ollama), and the codex browser bridge
/// (`browser` / `browser:<site>`). Every OTHER provider (openai, claude, gemini,
/// deepseek, desktop) used to fall through the `== "openai"`-only gate into the
/// local-codex branch, which unconditionally pointed codex at
/// `config.ollama_base_url` with `config.model` — e.g. `claude-sonnet-4-6` sent
/// to an ollama endpoint, which 404s with a misleading "check that ollama is
/// running" error and no hint that the provider choice was ignored.
///
/// Returns `Some(reason)` for a disallowed provider, `None` when code mode may
/// proceed. Mirrors the scheduled-runner allowlist
/// (`orchestrator/scheduled_runner.py::is_allowed_provider`).
fn code_mode_denied_reason(provider: &str) -> Option<String> {
    let p = provider.trim().to_ascii_lowercase();
    if p.is_empty()
        || p == "ollama"
        || p == "ollama-cloud"
        || crate::provider::is_browser_provider(&p)
    {
        return None;
    }
    Some(format!(
        "Code mode does not support the '{provider}' provider — it runs only on ollama \
         or a browser provider. Switch first: /provider ollama  (or e.g. /provider browser:chatgpt)."
    ))
}

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
                if let Some(reason) = code_mode_denied_reason(&app.config.provider) {
                    println!("{}", paint_dim(&reason));
                    continue;
                }
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
            // Code mode + browser provider: the browser thread is the
            // cross-task memory, so /compact runs through the codex bridge
            // service (summarize the live thread → seed the next fresh chat).
            // Chat mode keeps the local TUI-transcript trim below.
            if app.mode == AppMode::Code && provider::is_browser_provider(&app.config.provider) {
                return stream_repl_turn(app, "/compact".to_string()).await;
            }
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
            if let Some(reason) = code_mode_denied_reason(&app.config.provider) {
                app.push(MessageRole::Error, reason);
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
            if next == AppMode::Code {
                if let Some(reason) = code_mode_denied_reason(&app.config.provider) {
                    app.push(MessageRole::Error, reason);
                    return Ok(false);
                }
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
        // A11: real outcome variants replacing magic-string sentinels.
        CommandOutcome::ClearConversation => {
            app.messages.clear();
            save_current_session_allow_empty(app);
            print_note("Conversation cleared.");
            Ok(false)
        }
        CommandOutcome::OpenSessionPicker => {
            app.refresh_sessions();
            if io::stdin().is_terminal() {
                choose_session_interactively(app)?;
            } else {
                print_session_list(&app.sessions);
            }
            Ok(false)
        }
        CommandOutcome::ResumeSession(session_id) => {
            app.resume_session(&session_id);
            app.view = ViewState::InChat;
            print_recent_transcript(app);
            Ok(false)
        }
    }
}

fn handle_repl_message(app: &mut App, message: String) -> Result<bool, Box<dyn std::error::Error>> {
    match message.as_str() {
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
                timestamp_ms: None,
            });
            save_current_session(app);
        }
    }
    Ok(false)
}

/// P7: print a one-time note when chat falls back to plain (non-agentic) mode
/// because no Kim source root was found.
fn maybe_note_plain_chat(code_mode: bool, provider: &str) {
    use std::sync::atomic::{AtomicBool, Ordering};
    static SHOWN: AtomicBool = AtomicBool::new(false);
    if code_mode {
        return;
    }
    let p = provider.trim().to_lowercase();
    if p == "desktop" || p.starts_with("browser") {
        return;
    }
    if crate::sessions::find_kim_repo_root().is_none() && !SHOWN.swap(true, Ordering::Relaxed) {
        print_note(
            "plain chat — no Kim source root found; run the installer for agentic tool-using chat.",
        );
    }
}

/// Control tasks that never spawn Codex (they compact the browser thread), so
/// they must skip the git-repo confirmation. Mirrors `_COMPACT_CONTROL_TASKS`
/// in orchestrator/codex_bridge_service.py.
fn is_compact_control_task(task: &str) -> bool {
    matches!(
        task.trim().to_ascii_lowercase().as_str(),
        "/compact" | "compact" | "__kim_compact_context__"
    )
}

/// Interactive y/N: confirm running Codex in a directory that is not a git repo.
/// Returns true only on an explicit yes; a non-tty / EOF / read error is "no".
fn confirm_run_outside_git_repo(cwd: &Path) -> bool {
    // F5: when stdin is not a terminal (piped/one-shot), reading here would
    // steal the next queued prompt line as the y/N answer (or hit EOF).
    // Auto-decline instead of consuming input that isn't an answer.
    if !io::stdin().is_terminal() {
        eprintln!(
            "⚠  {} is not a git repository and stdin is not a terminal — \
             declining the code-mode run. Run from a git repo, `git init` first, \
             or use an interactive terminal to confirm.",
            cwd.display()
        );
        return false;
    }
    print!(
        "⚠  {} is not a git repository.\n   \
         Codex can't track or undo its edits here. Run anyway? [y/N] ",
        cwd.display()
    );
    let _ = io::stdout().flush();
    let mut input = String::new();
    if io::stdin().read_line(&mut input).is_err() {
        return false;
    }
    matches!(input.trim().to_ascii_lowercase().as_str(), "y" | "yes")
}

async fn stream_repl_turn(
    app: &mut App,
    prompt: String,
) -> Result<bool, Box<dyn std::error::Error>> {
    // Code mode runs Codex, which refuses to operate outside a git repository
    // (so its edits stay trackable/undoable). Rather than fail hard, confirm
    // once per session before running in a non-git directory. Compact control
    // tasks never reach Codex, so they skip this.
    if app.mode == AppMode::Code && !is_compact_control_task(&prompt) && !app.allow_non_git_codex {
        let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
        if !crate::provider::is_git_repo(&cwd) {
            if confirm_run_outside_git_repo(&cwd) {
                app.allow_non_git_codex = true;
            } else {
                println!(
                    "Cancelled. Codex needs a git repo — run Kim from inside one, \
                     or `git init` here first."
                );
                // F5: record the cancellation so one-shot mode exits non-zero.
                // Previously nothing was pushed, `run_oneshot`'s last-message-is-
                // Error check never fired, and scripts saw a bogus exit 0.
                app.push(
                    MessageRole::Error,
                    format!(
                        "Cancelled: {} is not a git repository; the code-mode run was declined.",
                        cwd.display()
                    ),
                );
                return Ok(false);
            }
        }
    }

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
    let allow_non_git = app.allow_non_git_codex;
    let session_id = app.current_session_id.clone();
    let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<AppEvent>();

    // P7: in chat mode, run the REAL Kim agent (tool loop) when a Kim source root
    // + Python are available; otherwise fall back to plain LLM chat with a note.
    let agentic = if code_mode {
        None
    } else if provider::is_browser_provider(&config.provider) {
        // Chat-mode browser provider: if the Kim desktop bridge is already
        // running, reuse it (keeps the in-app webview experience and avoids a
        // second Chrome). Otherwise drive ChatGPT/Gemini/Claude directly via the
        // local Playwright agent — same mechanism code mode uses, so a bare
        // `kim` works without launching the desktop app.
        if provider::bridge_is_available(&config.desktop_bridge_url).await {
            None
        } else {
            crate::agentic::agentic_available(&config.provider)
        }
    } else {
        crate::agentic::agentic_available(&config.provider)
    };
    // Ctrl-C → SIGTERM (graceful) before the hard kill. The pid slot is shared
    // by anything that spawns a killable child this turn:
    //   - code mode's codex child (via CodexTurnControl.pid_slot), and
    //   - F-E-5: the chat-mode agentic child (orchestrator.agent), directly and
    //     via the browser-provider TOCTOU fallback inside stream_kim_request.
    // Populated iff a child could be spawned; the HTTP bridge path leaves it
    // None (there is no local child to signal).
    let child_pid: Option<std::sync::Arc<std::sync::Mutex<Option<u32>>>> =
        if code_mode || agentic.is_some() || provider::is_browser_provider(&config.provider) {
            Some(std::sync::Arc::new(std::sync::Mutex::new(None::<u32>)))
        } else {
            None
        };
    // Parity Part 4: code-mode turns also get a decision channel (REPL → child
    // stdin, for native codex approvals).
    let (codex_control, decision_tx) = if code_mode {
        let (dtx, drx) = tokio::sync::mpsc::unbounded_channel::<String>();
        (
            Some(crate::provider::CodexTurnControl {
                decision_rx: drx,
                pid_slot: child_pid
                    .clone()
                    .expect("code_mode implies a pid slot was created above"),
            }),
            Some(dtx),
        )
    } else {
        (None, None)
    };

    let handle = if let Some((root, python)) = agentic {
        let prompt2 = prompt.clone();
        let sid = session_id.clone();
        let provider = config.provider.clone();
        let pid_slot = child_pid.clone();
        tokio::spawn(async move {
            let session_dir = root.join("kim_sessions");
            crate::agentic::stream_agentic_request(
                &root,
                &python,
                &prompt2,
                &provider,
                &session_dir,
                Some(&sid),
                pid_slot,
                tx,
            )
            .await;
        })
    } else {
        maybe_note_plain_chat(code_mode, &config.provider);
        let pid_slot = child_pid.clone();
        tokio::spawn(async move {
            stream_kim_request(
                &config,
                &history,
                code_mode,
                &session_id,
                allow_non_git,
                tx,
                codex_control,
                pid_slot,
            )
            .await;
        })
    };

    // A6: Ctrl-C cancels the current generation instead of killing the CLI.
    // tokio::signal::ctrl_c fires only while we await here (between turns,
    // rustyline owns the prompt and its own Ctrl-C handling resumes).
    let cancel = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    let result = consume_turn_events(
        app,
        rx,
        Instant::now(),
        save_current_session,
        cancel,
        decision_tx,
        child_pid,
    )
    .await;
    // Reap the request task (kill_on_drop reaps any child subprocess). No-op if
    // it already finished.
    handle.abort();
    result
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
            timestamp_ms: Some(UiMessage::now_ms()),
        },
    );
    app.push(
        MessageRole::System,
        format!("Compacted {removed} older message(s)."),
    );
}

/* ===========================================================
misc helpers
=========================================================== */

fn new_session_id() -> String {
    // Include PID so two `kim` processes that start within the same millisecond
    // (e.g. parallel invocations in scripts or tests) never produce the same ID
    // and therefore never silently clobber each other's .jsonl via atomic rename.
    // The counter still disambiguates multiple sessions within the same process.
    static COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_millis());
    let pid = std::process::id();
    let n = COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    format!("session-{millis}-{pid}-{n}")
}

/* ===========================================================
tests
=========================================================== */

#[cfg(test)]
mod tests {
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

    use super::{
        code_mode_denied_reason, consume_turn_events, new_session_id, provider_is_ready,
        provider_is_ready_with_env, App, AppEvent, AppMode, MessageRole, ViewState,
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
            allow_non_git_codex: false,
        }
    }

    fn save_into<'a>(dir: &'a Path) -> impl FnMut(&App) + 'a {
        move |a: &App| {
            crate::sessions::save_session_messages_in(dir, &a.current_session_id, &a.messages)
                .expect("session save");
        }
    }

    // A6: a cancel signal must end the turn (return to prompt) instead of
    // hanging on an open stream / killing the process.
    #[tokio::test]
    async fn cancel_signal_ends_turn_without_hanging() {
        let dir = temp_session_dir();
        let mut app = test_app("cancel-test-1");
        app.push(MessageRole::User, "do something long");
        save_into(&dir)(&app);

        // tx stays open with no Done — without cancel, recv() would block forever.
        let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<AppEvent>();
        tx.send(AppEvent::TextChunk("partial".to_string())).unwrap();

        let res = consume_turn_events(
            &mut app,
            rx,
            Instant::now(),
            save_into(&dir),
            std::future::ready(()),
            None,
            None,
        )
        .await;
        assert!(res.is_ok(), "cancelled turn should return Ok, not hang");
        drop(tx);
        let _ = fs::remove_dir_all(&dir);
    }

    // F-E-4: a Ctrl-C interrupt mid-turn must leave a trailing Error message so
    // one-shot `kim chat`/`kim code` exits non-zero (run_oneshot checks the last
    // message's role). The partial answer is still kept.
    #[tokio::test]
    async fn cancelled_turn_marks_error_for_nonzero_exit() {
        let dir = temp_session_dir();
        let mut app = test_app("cancel-err-1");
        app.push(MessageRole::User, "long task");
        save_into(&dir)(&app);

        let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<AppEvent>();
        // One partial chunk arrives, then the cancel fires (biased select drains
        // the ready chunk first, then takes the cancel branch).
        tx.send(AppEvent::TextChunk("partial answer".to_string()))
            .unwrap();

        consume_turn_events(
            &mut app,
            rx,
            Instant::now(),
            save_into(&dir),
            std::future::ready(()),
            None,
            None,
        )
        .await
        .unwrap();

        assert!(
            matches!(app.messages.last(), Some(m) if m.role == MessageRole::Error),
            "a Ctrl-C cancel must leave a trailing Error; messages: {:?}",
            app.messages
        );
        // The partial answer is preserved (as an Assistant message before the Error).
        assert!(app
            .messages
            .iter()
            .any(|m| m.role == MessageRole::Assistant && m.content.contains("partial answer")));
        drop(tx);
        let _ = fs::remove_dir_all(&dir);
    }

    // F-E-4: a turn that ends with no answer at all (child exited / empty stream)
    // must leave a trailing Error so one-shot mode exits non-zero.
    #[tokio::test]
    async fn no_response_turn_marks_error_for_nonzero_exit() {
        let dir = temp_session_dir();
        let mut app = test_app("noresp-1");
        app.push(MessageRole::User, "hi");
        save_into(&dir)(&app);

        let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<AppEvent>();
        tx.send(AppEvent::Done(false)).unwrap(); // stream ended, never produced text
        drop(tx);

        consume_turn_events(
            &mut app,
            rx,
            Instant::now(),
            save_into(&dir),
            std::future::pending::<()>(),
            None,
            None,
        )
        .await
        .unwrap();

        assert!(
            matches!(app.messages.last(), Some(m) if m.role == MessageRole::Error),
            "a no-response turn must leave a trailing Error; messages: {:?}",
            app.messages
        );
        let _ = fs::remove_dir_all(&dir);
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
        consume_turn_events(
            &mut app,
            rx,
            Instant::now(),
            save_into(&dir),
            std::future::pending::<()>(),
            None,
            None,
        )
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
        tx.send(AppEvent::TextChunk("first answer".to_string()))
            .unwrap();
        tx.send(AppEvent::Done(false)).unwrap();
        drop(tx);
        consume_turn_events(
            &mut app,
            rx,
            Instant::now(),
            save_into(&dir),
            std::future::pending::<()>(),
            None,
            None,
        )
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
        tx2.send(AppEvent::TextChunk("second answer".to_string()))
            .unwrap();
        tx2.send(AppEvent::Done(false)).unwrap();
        drop(tx2);
        consume_turn_events(
            &mut app2,
            rx2,
            Instant::now(),
            save_into(&dir),
            std::future::pending::<()>(),
            None,
            None,
        )
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

    // ── A20: chat_history budget ────────────────────────────────────────────

    #[test]
    fn chat_history_caps_message_count() {
        let mut app = test_app("budget-count");
        for i in 0..40 {
            app.push(MessageRole::User, format!("m{i}"));
        }
        let hist = app.chat_history();
        assert!(hist.len() <= 24, "expected <=24, got {}", hist.len());
        assert_eq!(hist.last().unwrap().content, "m39"); // newest preserved
    }

    #[test]
    fn chat_history_respects_char_budget() {
        let mut app = test_app("budget-chars");
        app.push(MessageRole::User, "a".repeat(30_000));
        app.push(MessageRole::Assistant, "b".repeat(30_000));
        app.push(MessageRole::User, "c".repeat(30_000));
        let hist = app.chat_history();
        // Only the newest fits under the ~48k budget (the next would exceed it).
        assert_eq!(hist.len(), 1);
        assert_eq!(hist[0].content.chars().next(), Some('c'));
    }

    // ── F-E-14: code-mode provider gate ──────────────────────────────────────

    #[test]
    fn code_mode_allows_only_ollama_and_browser() {
        // Allowed: the two real codex backends (+ empty → ollama).
        for ok in [
            "ollama",
            "ollama-cloud",
            "",
            "browser",
            "browser:chatgpt",
            "BROWSER:Claude",
        ] {
            assert!(
                code_mode_denied_reason(ok).is_none(),
                "code mode must allow {ok:?}"
            );
        }
        // Rejected: everything the old `== \"openai\"` gate let silently route to
        // ollama with a non-ollama model name.
        for bad in ["openai", "claude", "gemini", "deepseek", "desktop"] {
            let reason = code_mode_denied_reason(bad)
                .unwrap_or_else(|| panic!("code mode must reject {bad:?}"));
            assert!(
                reason.contains(bad) && reason.contains("ollama"),
                "rejection for {bad:?} should name the provider and the fix; got: {reason}"
            );
        }
    }

    // ── session_id regression guards ─────────────────────────────────────────

    /// The session ID must embed the current process ID so that two `kim`
    /// processes starting in the same millisecond never share an ID and
    /// therefore never clobber each other's session files.
    #[test]
    fn session_id_includes_pid() {
        let id = new_session_id();
        let pid = std::process::id();
        // Expected format: "session-<millis>-<pid>-<n>"
        assert!(
            id.starts_with("session-"),
            "id must start with 'session-', got: {id}"
        );
        let pid_str = pid.to_string();
        assert!(
            id.contains(&pid_str),
            "id must contain the current PID ({pid_str}), got: {id}"
        );
        // Structural check: exactly 4 dash-separated parts.
        let parts: Vec<&str> = id.splitn(4, '-').collect();
        assert_eq!(
            parts.len(),
            4,
            "id must have 4 dash-separated parts (session-<millis>-<pid>-<n>), got: {id}"
        );
        assert_eq!(parts[0], "session");
        assert!(
            parts[1].parse::<u128>().is_ok(),
            "second part must be a millisecond timestamp, got: {}",
            parts[1]
        );
        assert_eq!(
            parts[2], pid_str,
            "third part must be the PID, got: {}",
            parts[2]
        );
        assert!(
            parts[3].parse::<u64>().is_ok(),
            "fourth part must be the counter, got: {}",
            parts[3]
        );
    }

    /// Two successive calls within the same process must produce distinct IDs
    /// because the atomic counter suffix increments on every call.
    #[test]
    fn session_ids_distinct_within_process() {
        let id1 = new_session_id();
        let id2 = new_session_id();
        assert_ne!(
            id1, id2,
            "successive new_session_id() calls must return distinct IDs"
        );
        // The counter suffix (last segment) of id2 must be exactly one higher
        // than that of id1.
        let n1: u64 = id1
            .rsplit('-')
            .next()
            .expect("id1 must have a counter suffix")
            .parse()
            .expect("counter suffix must be a u64");
        let n2: u64 = id2
            .rsplit('-')
            .next()
            .expect("id2 must have a counter suffix")
            .parse()
            .expect("counter suffix must be a u64");
        assert_eq!(
            n2,
            n1 + 1,
            "counter suffix must increment by 1 between calls (got {n1} then {n2})"
        );
    }
}

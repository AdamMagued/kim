//! The interactive REPL: `kim`'s prompt loop, slash-command dispatch, and
//! all its terminal rendering. Split out of the former `main.rs`/`lib.rs`
//! god-file (see `app.rs` for the `App` struct and `turn.rs` for
//! turn-streaming/compaction) — pure relocation, no behavior changes.

use std::io::{self, stdout, IsTerminal, Write};

use rustyline::completion::{Completer, Pair};
use rustyline::error::ReadlineError;
use rustyline::highlight::Highlighter;
use rustyline::hint::Hinter;
use rustyline::validate::{ValidationContext, ValidationResult, Validator};
use rustyline::{Context, Helper};

use crate::app::{
    save_current_session, save_current_session_allow_empty, App, AppMode, MessageRole, UiMessage,
    ViewState,
};
use crate::commands::{
    command_summary, handle_command, login_with_key, CommandOutcome, SUPPORTED_COMMANDS,
};
use crate::config::KimConfig;
use crate::file_refs::prompt_with_file_references;
use crate::paint::{
    kim_accent_color, paint_bold, paint_dim, paint_text, print_message, print_note,
};
use crate::pickers::{choose_model_interactively, choose_session_interactively};
use crate::provider;
use crate::sessions::SessionEntry;
use crate::turn::{
    code_mode_denied_reason, compact_app_messages, provider_is_ready, stream_repl_turn,
};

pub(crate) async fn run_repl(
    resume_id: Option<&str>,
) -> Result<String, Box<dyn std::error::Error>> {
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
            paint_dim("Not signed in yet. Run /login ollama, /login claude, /login browser:chatgpt, or /provider <name>.")
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
pub(crate) fn maybe_note_plain_chat(code_mode: bool, provider: &str) {
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

pub(crate) fn print_recent_transcript(app: &App) {
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

pub(crate) fn print_model_options(current: &str, options: &[String]) {
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

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::time::{Instant, SystemTime, UNIX_EPOCH};

    use crate::app::{App, AppMode, MessageRole, ViewState};
    use crate::config::KimConfig;
    use crate::provider::AppEvent;
    use crate::repl_turn::consume_turn_events;

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
}

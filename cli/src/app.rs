//! Core REPL state: message/role/mode types and the `App` struct itself, plus
//! the small helpers that are really just `App`-persistence/identity concerns
//! (`save_current_session*`, `new_session_id`). Split out of the former
//! `main.rs`/`lib.rs` god-file (see `repl.rs` for the interactive loop and
//! `turn.rs` for turn-streaming/compaction) — pure relocation, no behavior
//! changes.

use std::time::{SystemTime, UNIX_EPOCH};

use crate::config::KimConfig;
use crate::provider::ChatMessage;
use crate::sessions::{
    discover_project_sessions, discover_sessions, find_session_by_id, load_session_messages,
    save_session_messages, SessionEntry,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MessageRole {
    User,
    Assistant,
    System,
    Error,
    // Matched against (paint.rs's renderer, the persistence filters in
    // sessions.rs and chat_history above) and constructed by sessions.rs's
    // persistence tests, but nothing in the non-test REPL path currently
    // pushes a Reasoning-role UiMessage (ThoughtChunk streaming renders
    // directly instead of going through App::push). Kept for the model/tests
    // and reserved for future reasoning-trace rendering; the dead-code lint
    // can't see the `#[cfg(test)]` construction sites in a normal build.
    #[allow(dead_code)]
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
    pub(crate) fn now_ms() -> u64 {
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
    pub(crate) fn new(config: KimConfig, resume_id: Option<&str>) -> Self {
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

    pub(crate) fn resume_session(&mut self, session_id: &str) {
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

    pub(crate) fn push(&mut self, role: MessageRole, content: impl Into<String>) {
        self.messages.push(UiMessage {
            role,
            content: content.into(),
            // F-E-3: stamp the creation time now, so it survives future saves.
            timestamp_ms: Some(UiMessage::now_ms()),
        });
    }

    pub(crate) fn chat_history(&self) -> Vec<ChatMessage> {
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

    pub(crate) fn refresh_sessions(&mut self) {
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

    pub(crate) fn set_mode(&mut self, mode: AppMode) {
        self.mode = mode;
        self.refresh_sessions();
        self.view = ViewState::InChat;
        self.status = format!("kim {} mode", self.mode.label());
    }

    pub(crate) fn toggle_mode(&mut self) {
        let next = match self.mode {
            AppMode::Chat => AppMode::Code,
            AppMode::Code => AppMode::Chat,
        };
        self.set_mode(next);
    }

    pub(crate) fn start_new_chat(&mut self) {
        self.current_session_id = new_session_id();
        self.messages.clear();
        self.view = ViewState::InChat;
        self.status = format!("new Kim {} chat", self.mode.label());
    }
}

pub(crate) fn save_current_session(app: &App) {
    if !app
        .messages
        .iter()
        .any(|m| matches!(m.role, MessageRole::User | MessageRole::Assistant))
    {
        return;
    }
    save_current_session_allow_empty(app);
}

pub(crate) fn save_current_session_allow_empty(app: &App) {
    if let Err(error) = save_session_messages(&app.current_session_id, &app.messages) {
        eprintln!("Could not save session: {error}");
    }
}

pub(crate) fn new_session_id() -> String {
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

#[cfg(test)]
mod tests {
    use super::{App, AppMode, MessageRole, ViewState};
    use crate::config::KimConfig;

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

    // ── session_id regression guards ─────────────────────────────────────────

    /// The session ID must embed the current process ID so that two `kim`
    /// processes starting in the same millisecond never share an ID and
    /// therefore never clobber each other's session files.
    #[test]
    fn session_id_includes_pid() {
        let id = super::new_session_id();
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
        let id1 = super::new_session_id();
        let id2 = super::new_session_id();
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

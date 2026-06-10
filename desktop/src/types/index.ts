// ── Session types ────────────────────────────────────────────────────────────

export interface SessionInfo {
  session_id: string;
  session_key?: string;
  title?: string;
  date: string;
  message_count: number;
  has_summary: boolean;
  summary?: string;
  session_type: 'kim' | 'codex';
  project_path?: string;
  browser_threads?: Record<string, string>;
  browser_last_site?: string;
  browser_threads_updated_at_ms?: number;
  /** Composer LLM choice for this session, e.g. `browser:gemini` or `claude`. */
  last_llm_provider?: string;
}

export interface BrowserSessionMeta {
  browser_threads: Record<string, string>;
  browser_last_site?: string;
  browser_threads_updated_at_ms?: number;
  last_llm_provider?: string;
}

export interface BrowserRestoreResult {
  restored: boolean;
  site: string;
  url: string;
  reason: string;
  message?: string;
}

// ── Message / content types ──────────────────────────────────────────────────

export interface TextBlock {
  type: 'text';
  text: string;
}

export interface ToolUseBlock {
  type: 'tool_use';
  id: string;
  name: string;
  input: Record<string, unknown>;
}

export interface ToolResultBlock {
  type: 'tool_result';
  tool_use_id: string;
  content: string | ContentBlock[];
}

export interface ImageBlock {
  type: 'image';
  // base64 data stripped from disk; we just show a placeholder
}

export type ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock | ImageBlock;

export interface OpenAIToolCall {
  id: string;
  type: 'function';
  function: {
    name: string;
    arguments: string; // JSON string
  };
}

export interface KimMessage {
  role: 'user' | 'assistant' | 'tool' | 'system' | 'compact_summary';
  content: string | ContentBlock[];
  tool_calls?: OpenAIToolCall[];
  tool_call_id?: string;
  name?: string;
}

// ── Account ──────────────────────────────────────────────────────────────────

export interface GoogleAccount {
  email: string;
  authuser_index: number;
}

export interface GoogleApiAccount {
  /** Google identity connected through OAuth for official Gemini API calls. */
  email: string;
  connected: boolean;
  /** Epoch seconds for the current access token, when known. Refresh token stays in OS secure storage. */
  expires_at?: number;
  /** Quota/billing project used for x-goog-user-project when configured. */
  project_id?: string;
  needs_reauth?: boolean;
}

export interface KimAccount {
  display_name: string;
  github_username?: string;
  github_token?: string;
  github_avatar_url?: string;
  gist_id?: string;
  created_at: string;
  /** Explicit project roots shown in the Code tab — scans .codex/ inside each */
  code_projects?: string[];
  /** Google accounts used for Gemini browser sign-in. */
  google_accounts?: GoogleAccount[];
  /** Active Google account email for Gemini browser sessions. */
  google_active_account?: string;
  /** Official Gemini API OAuth connection. Refresh tokens are not stored here. */
  google_api_account?: GoogleApiAccount;
}

// ── Codex (Code) project types ───────────────────────────────────────────────

export interface CodexSession {
  session_id: string;
  /** YYYY-MM-DD bucket folder name under .codex/ (used to locate file). */
  date: string;
  /** ISO timestamp (UTC) based on file modified time (used for sorting/display). */
  updated_at: string;
  message_count: number;
  summary?: string;
  title: string;
}

export interface CodexBranch {
  name: string;
  sessions: CodexSession[];
}

export interface CodexProject {
  path: string;
  name: string;
  current_branch: string;
  branches: CodexBranch[];
}

// ── Settings ─────────────────────────────────────────────────────────────────

export type Theme = 'dark' | 'light' | 'system';
export type Provider = 'claude' | 'openai' | 'gemini' | 'deepseek' | 'browser' | 'ollama';
export type AccentTheme = 'indigo' | 'ocean' | 'ember' | 'teal' | 'jade' | 'mono';
/** HITL permission gate. full_auto = no gate; ask_risky = high-risk tools only; ask_always = medium+high. */
export type PermissionMode = 'full_auto' | 'ask_risky' | 'ask_always';
export type VoiceEngine = 'kokoro' | 'maya1' | 'http' | 'hume';
export type TypingAnimation = 'none' | 'typewriter' | 'word-fade' | 'char-blur';

export interface OllamaSettings {
  base_url: string;
  mode: 'local' | 'cloud';
  local_model: string;
  cloud_model: string;
  connected: boolean;
  cloud_connected: boolean;
  context_limit_override: number | null;
}

export interface VoiceSettings {
  enabled: boolean;
  engine: VoiceEngine;
  voice_id: string;    // kokoro voice_id OR hume voice_name OR http voice
}

export interface ScheduleTimerSettings {
  enabled: boolean;
  interval_seconds: number;
}

export interface Settings {
  kim_sessions_dir: string;
  codex_sessions_dir: string;
  project_root: string;
  provider: Provider;
  allow_message_queue: boolean;
  keep_browser_visible: boolean;
  /** Cumulative input/context budget before Kim prompts to compact. */
  context_budget_tokens: number;
  theme: Theme;
  accent: AccentTheme;
  voice: VoiceSettings;
  schedule_timer: ScheduleTimerSettings;
  typing_animation: TypingAnimation;
  ollama: OllamaSettings;
  permission_mode: PermissionMode;
}

export const DEFAULT_SETTINGS: Settings = {
  kim_sessions_dir: '',
  codex_sessions_dir: '',
  project_root: '',
  provider: 'ollama',
  allow_message_queue: false,
  keep_browser_visible: false,
  context_budget_tokens: 200_000,
  theme: 'system',
  accent: 'indigo',
  voice: {
    enabled: true,
    engine: 'kokoro',
    voice_id: 'af_heart',
  },
  schedule_timer: {
    enabled: false,
    interval_seconds: 300,
  },
  typing_animation: 'none',
  permission_mode: 'full_auto' as PermissionMode,
  ollama: {
    base_url: 'http://localhost:11434',
    mode: 'cloud',
    local_model: '',
    cloud_model: 'gpt-oss:120b-cloud',
    connected: false,
    cloud_connected: false,
    context_limit_override: null,
  },
};

// Voice catalog per engine — used by SettingsPanel to populate the voice dropdown.
export const VOICES_BY_ENGINE: Record<VoiceEngine, { value: string; label: string }[]> = {
  kokoro: [
    { value: 'af_heart', label: 'Heart (warm female)' },
    { value: 'af_sky',   label: 'Sky (bright female)' },
    { value: 'af_bella', label: 'Bella (soft female)' },
    { value: 'af_sarah', label: 'Sarah (neutral female)' },
    { value: 'am_adam',  label: 'Adam (male)' },
    { value: 'am_michael', label: 'Michael (male)' },
    { value: 'bf_emma',  label: 'Emma (British female)' },
    { value: 'bm_george', label: 'George (British male)' },
  ],
  maya1: [
    { value: 'default', label: 'Default (speaker description from config)' },
  ],
  http: [
    { value: 'nova',    label: 'Nova' },
    { value: 'alloy',   label: 'Alloy' },
    { value: 'echo',    label: 'Echo' },
    { value: 'fable',   label: 'Fable' },
    { value: 'onyx',    label: 'Onyx' },
    { value: 'shimmer', label: 'Shimmer' },
  ],
  hume: [
    { value: 'Alice Bennett', label: 'Alice Bennett (warm female)' },
    { value: 'Ava Song',      label: 'Ava Song' },
    { value: 'Colton Rivers', label: 'Colton Rivers (male)' },
    { value: 'Dacher Keltner', label: 'Dacher Keltner (male)' },
  ],
};

// ── Agent events ─────────────────────────────────────────────────────────────

export interface AgentOutputEvent {
  line: string;
}

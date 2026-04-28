// ── Session types ────────────────────────────────────────────────────────────

export interface SessionInfo {
  session_id: string;
  title?: string;
  date: string;
  message_count: number;
  has_summary: boolean;
  summary?: string;
  session_type: 'kim' | 'claw';
  project_path?: string;
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
  role: 'user' | 'assistant' | 'tool' | 'system';
  content: string | ContentBlock[];
  tool_calls?: OpenAIToolCall[];
  tool_call_id?: string;
  name?: string;
}

// ── Account ──────────────────────────────────────────────────────────────────

export interface KimAccount {
  display_name: string;
  github_username?: string;
  github_token?: string;
  github_avatar_url?: string;
  gist_id?: string;
  created_at: string;
  /** Explicit project roots shown in the Code tab — scans .claw/sessions/ inside each */
  code_projects?: string[];
}

// ── Claw (Code) project types ────────────────────────────────────────────────

export interface ClawSession {
  session_id: string;
  date: string;
  message_count: number;
  summary?: string;
}

export interface ClawBranch {
  name: string;
  sessions: ClawSession[];
}

export interface ClawProject {
  path: string;
  name: string;
  current_branch: string;
  branches: ClawBranch[];
}

// ── Settings ─────────────────────────────────────────────────────────────────

export type Theme = 'dark' | 'light' | 'system';
export type Provider = 'claude' | 'openai' | 'gemini' | 'deepseek' | 'browser';
export type AccentTheme = 'indigo' | 'ocean' | 'ember' | 'teal' | 'jade' | 'mono';
export type VoiceEngine = 'kokoro' | 'maya1' | 'http' | 'hume';
export type TypingAnimation = 'none' | 'typewriter' | 'word-fade' | 'char-blur';

export interface VoiceSettings {
  enabled: boolean;
  engine: VoiceEngine;
  voice_id: string;    // kokoro voice_id OR hume voice_name OR http voice
}

export interface Settings {
  kim_sessions_dir: string;
  claw_sessions_dir: string;
  project_root: string;
  provider: Provider;
  allow_message_queue: boolean;
  theme: Theme;
  accent: AccentTheme;
  voice: VoiceSettings;
  typing_animation: TypingAnimation;
}

export const DEFAULT_SETTINGS: Settings = {
  kim_sessions_dir: '',
  claw_sessions_dir: '',
  project_root: '',
  provider: 'browser',
  allow_message_queue: false,
  theme: 'system',
  accent: 'indigo',
  voice: {
    enabled: true,
    engine: 'kokoro',
    voice_id: 'af_heart',
  },
  typing_animation: 'none',
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

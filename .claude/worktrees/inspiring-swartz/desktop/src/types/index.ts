// ── Session types ────────────────────────────────────────────────────────────

export interface SessionInfo {
  session_id: string;
  date: string;
  message_count: number;
  has_summary: boolean;
  summary?: string;
  session_type: 'kim' | 'claw';
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

// ── Settings ─────────────────────────────────────────────────────────────────

export type Theme = 'dark' | 'light' | 'system';
export type Provider = 'claude' | 'openai' | 'gemini' | 'deepseek' | 'browser';
export type VoiceEngine = 'kokoro' | 'maya1' | 'http' | 'hume';

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
  theme: Theme;
  voice: VoiceSettings;
}

export const DEFAULT_SETTINGS: Settings = {
  kim_sessions_dir: '',   // empty = use Rust default
  claw_sessions_dir: '',
  project_root: '',       // empty = use Rust default
  provider: 'browser',
  theme: 'system',
  voice: {
    enabled: true,
    engine: 'kokoro',
    voice_id: 'af_heart',
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

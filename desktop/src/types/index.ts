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

export interface Settings {
  kim_sessions_dir: string;
  claw_sessions_dir: string;
  project_root: string;
  provider: Provider;
  theme: Theme;
}

export const DEFAULT_SETTINGS: Settings = {
  kim_sessions_dir: '',   // empty = use Rust default
  claw_sessions_dir: '',
  project_root: '',       // empty = use Rust default
  provider: 'claude',
  theme: 'system',
};

// ── Agent events ─────────────────────────────────────────────────────────────

export interface AgentOutputEvent {
  line: string;
}

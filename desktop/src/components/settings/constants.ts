/**
 * Pure data constants for SettingsPanel.
 *
 * Extracted from SettingsPanel.tsx (Phase 9 restructure).
 * No JSX, no hooks — safe to import from non-React contexts.
 */

import type { Provider, VoiceEngine, AccentTheme } from '../../types';

export const PROVIDERS: { value: Provider; label: string }[] = [
  { value: 'ollama', label: 'Ollama (local/cloud, no API key)' },
  { value: 'browser', label: 'Browser (no API key)' },
  { value: 'claude', label: 'Claude (Anthropic)' },
  { value: 'openai', label: 'GPT-4o (OpenAI)' },
  { value: 'gemini', label: 'Gemini (Google)' },
  { value: 'deepseek', label: 'DeepSeek' },
];

export const VOICE_ENGINES: { value: VoiceEngine; label: string }[] = [
  { value: 'kokoro', label: 'Kokoro (local, fast)' },
  { value: 'maya1', label: 'Maya-1 (local, expressive)' },
  { value: 'http', label: 'HTTP (OpenAI-compatible)' },
  { value: 'hume', label: 'Hume (cloud)' },
];

export const ACCENTS: { value: AccentTheme; label: string; light: string; dark: string }[] = [
  { value: 'indigo', label: 'Terracotta', light: '#b88a74', dark: '#d4a08a' },
  { value: 'ocean',  label: 'Mist',       light: '#6a8f9d', dark: '#90b7c3' },
  { value: 'ember',  label: 'Sienna',     light: '#c4835a', dark: '#e4a37a' },
  { value: 'teal',   label: 'Sage',       light: '#6a8f66', dark: '#a8c5a3' },
  { value: 'jade',   label: 'Rose',       light: '#9d7a7a', dark: '#d4a0a0' },
  { value: 'mono',   label: 'Mono',       light: '#2d2822', dark: '#e8dfd6' },
];

export const TYPING_ANIMATIONS: { value: string; label: string; desc: string; icon: string }[] = [
  { value: 'none',       label: 'Instant',    desc: 'No animation',                    icon: '—' },
  { value: 'typewriter', label: 'Typewriter',  desc: 'Characters appear one by one',   icon: '|' },
  { value: 'word-fade',  label: 'Word fade',   desc: 'Words drift up and fade in',     icon: '✦' },
  { value: 'char-blur',  label: 'Char blur',   desc: 'Letters crystallise from blur',  icon: '◎' },
];

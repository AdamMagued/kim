import { describe, it, expect } from 'vitest';
import {
  OLLAMA_CLOUD_DEFAULT_MODEL,
  DEFAULT_SETTINGS,
  ALL_PROVIDERS,
  BROWSER_PROVIDERS,
  API_PROVIDERS,
} from '../index';

// ── 1. ollama_cloud_default_constant ─────────────────────────────────────────

describe('ollama_cloud_default_constant', () => {
  it('OLLAMA_CLOUD_DEFAULT_MODEL equals gpt-oss:120b-cloud', () => {
    expect(OLLAMA_CLOUD_DEFAULT_MODEL).toBe('gpt-oss:120b-cloud');
  });

  it('DEFAULT_SETTINGS.ollama.cloud_model references OLLAMA_CLOUD_DEFAULT_MODEL (single source of truth)', () => {
    expect(DEFAULT_SETTINGS.ollama.cloud_model).toBe(OLLAMA_CLOUD_DEFAULT_MODEL);
  });
});

// ── 2. all_providers_grid ─────────────────────────────────────────────────────

describe('all_providers_grid', () => {
  it('contains exactly six providers', () => {
    expect(ALL_PROVIDERS).toHaveLength(6);
  });

  it('contains exactly the expected provider values', () => {
    const values = ALL_PROVIDERS.map((p) => p.value);
    expect(values).toEqual(['ollama', 'browser', 'claude', 'openai', 'gemini', 'deepseek']);
  });

  it('ollama entry has correct title and sub', () => {
    const entry = ALL_PROVIDERS.find((p) => p.value === 'ollama');
    expect(entry).toBeDefined();
    expect(entry!.title).toBe('Ollama');
    expect(entry!.sub).toBe('local/cloud · no API key');
  });

  it('browser entry has correct title and sub', () => {
    const entry = ALL_PROVIDERS.find((p) => p.value === 'browser');
    expect(entry).toBeDefined();
    expect(entry!.title).toBe('Browser');
    expect(entry!.sub).toBe('via browser · no API key');
  });

  it('claude entry has correct title and sub', () => {
    const entry = ALL_PROVIDERS.find((p) => p.value === 'claude');
    expect(entry).toBeDefined();
    expect(entry!.title).toBe('Claude');
    expect(entry!.sub).toBe('Anthropic');
  });

  it('openai entry has correct title and sub', () => {
    const entry = ALL_PROVIDERS.find((p) => p.value === 'openai');
    expect(entry).toBeDefined();
    expect(entry!.title).toBe('OpenAI');
    expect(entry!.sub).toBe('OpenAI');
  });

  it('gemini entry has correct title and sub', () => {
    const entry = ALL_PROVIDERS.find((p) => p.value === 'gemini');
    expect(entry).toBeDefined();
    expect(entry!.title).toBe('Gemini');
    expect(entry!.sub).toBe('Google');
  });

  it('deepseek entry has correct title and sub', () => {
    const entry = ALL_PROVIDERS.find((p) => p.value === 'deepseek');
    expect(entry).toBeDefined();
    expect(entry!.title).toBe('DeepSeek');
    expect(entry!.sub).toBe('DeepSeek');
  });
});

// ── 3. browser_vs_api_provider_sets ──────────────────────────────────────────

describe('browser_vs_api_provider_sets', () => {
  it('BROWSER_PROVIDERS includes chatgpt', () => {
    const ids = BROWSER_PROVIDERS.map((p) => p.id);
    expect(ids).toContain('chatgpt');
  });

  it('BROWSER_PROVIDERS contains exactly chatgpt, gemini, deepseek (claude + grok removed)', () => {
    const ids = BROWSER_PROVIDERS.map((p) => p.id);
    expect(ids).toEqual(['chatgpt', 'gemini', 'deepseek']);
    expect(ids).not.toContain('claude');
    expect(ids).not.toContain('grok');
  });

  it('BROWSER_PROVIDERS chatgpt label is ChatGPT', () => {
    const entry = BROWSER_PROVIDERS.find((p) => p.id === 'chatgpt');
    expect(entry!.label).toBe('ChatGPT');
  });

  it('API_PROVIDERS lists the four key-requiring providers: claude, openai, gemini, deepseek', () => {
    const ids = API_PROVIDERS.map((p) => p.id);
    expect(ids).toEqual(['claude', 'openai', 'gemini', 'deepseek']);
  });

  it('API_PROVIDERS claude label is Claude API', () => {
    const entry = API_PROVIDERS.find((p) => p.id === 'claude');
    expect(entry!.label).toBe('Claude API');
  });

  it('API_PROVIDERS openai label is OpenAI API', () => {
    const entry = API_PROVIDERS.find((p) => p.id === 'openai');
    expect(entry!.label).toBe('OpenAI API');
  });

  it('API_PROVIDERS gemini label is Gemini API', () => {
    const entry = API_PROVIDERS.find((p) => p.id === 'gemini');
    expect(entry!.label).toBe('Gemini API');
  });

  it('API_PROVIDERS deepseek label is DeepSeek API', () => {
    const entry = API_PROVIDERS.find((p) => p.id === 'deepseek');
    expect(entry!.label).toBe('DeepSeek API');
  });

  it('API_PROVIDERS does not include browser-only providers (grok, chatgpt)', () => {
    const ids = API_PROVIDERS.map((p) => p.id);
    expect(ids).not.toContain('grok');
    expect(ids).not.toContain('chatgpt');
  });

  it('BROWSER_PROVIDERS does not include openai (API-only id)', () => {
    const ids = BROWSER_PROVIDERS.map((p) => p.id);
    expect(ids).not.toContain('openai');
  });
});

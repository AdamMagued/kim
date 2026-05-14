import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { open as openDialog } from '@tauri-apps/plugin-dialog';
import type {
  Settings,
  Provider,
  Theme,
  VoiceEngine,
  VoiceSettings,
  AccentTheme,
  KimAccount,
  TypingAnimation,
} from '../../types';
import { VOICES_BY_ENGINE } from '../../types';
import { toast } from '../Toast';

// ── Types ─────────────────────────────────────────────────────────────────────

type PaneId =
  | 'appearance'
  | 'ai'
  | 'voice'
  | 'paths'
  | 'data'
  | 'account'
  | 'mcp'
  | 'feedback'
  | 'about';

interface Props {
  settings: Settings;
  onChange: (settings: Settings) => void;
  onClose: () => void;
  appVersion: string;
  onCheckUpdate: () => void;
  account: KimAccount;
  onAccountChange: (account: KimAccount) => Promise<void>;
  initialPane?: PaneId;
}

// ── Static config ─────────────────────────────────────────────────────────────

const NAV: { id: PaneId; label: string; icon: PaneId }[] = [
  { id: 'appearance', label: 'Appearance', icon: 'appearance' },
  { id: 'ai', label: 'AI', icon: 'ai' },
  { id: 'voice', label: 'Voice', icon: 'voice' },
  { id: 'paths', label: 'Paths', icon: 'paths' },
  { id: 'data', label: 'Data', icon: 'data' },
  { id: 'account', label: 'Account', icon: 'account' },
  { id: 'mcp', label: 'MCP', icon: 'mcp' },
  { id: 'feedback', label: 'Feedback', icon: 'feedback' },
  { id: 'about', label: 'About', icon: 'about' },
];

const ACCENTS: { value: AccentTheme; label: string; color: string }[] = [
  { value: 'indigo', label: 'Terracotta', color: '#e8b89a' },
  { value: 'ocean', label: 'Mist', color: '#a9c8e8' },
  { value: 'ember', label: 'Sienna', color: '#e4a37a' },
  { value: 'teal', label: 'Sage', color: '#a8c5a3' },
  { value: 'jade', label: 'Rose', color: '#d4a0a0' },
  { value: 'mono', label: 'Mono', color: '#e8e0d2' },
];

const PROVIDERS: { value: Provider; title: string; sub: string }[] = [
  { value: 'ollama', title: 'Ollama', sub: 'local/cloud · no API key' },
  { value: 'browser', title: 'Browser', sub: 'via browser · no API key' },
  { value: 'claude', title: 'Claude', sub: 'Anthropic' },
  { value: 'openai', title: 'GPT-4o', sub: 'OpenAI' },
  { value: 'gemini', title: 'Gemini', sub: 'Google' },
  { value: 'deepseek', title: 'DeepSeek', sub: 'DeepSeek' },
];

const OLLAMA_SUGGESTED_LOCAL_MODELS: OllamaModelInfo[] = [
  { name: 'llama3.2:3b', size: 0, family: 'llama', parameter_size: '3B', quantization_level: null, cloud: false, installed: false },
  { name: 'gemma3:4b', size: 0, family: 'gemma', parameter_size: '4B', quantization_level: null, cloud: false, installed: false },
  { name: 'qwen2.5-coder:7b', size: 0, family: 'qwen', parameter_size: '7B', quantization_level: null, cloud: false, installed: false },
  { name: 'llama3.1:8b', size: 0, family: 'llama', parameter_size: '8B', quantization_level: null, cloud: false, installed: false },
  { name: 'deepseek-r1:8b', size: 0, family: 'deepseek', parameter_size: '8B', quantization_level: null, cloud: false, installed: false },
  { name: 'llama3.3:70b', size: 0, family: 'llama', parameter_size: '70B', quantization_level: null, cloud: false, installed: false },
];

const OLLAMA_SUGGESTED_CLOUD_MODELS: OllamaModelInfo[] = [
  { name: 'gpt-oss:20b-cloud', size: 0, family: 'gpt-oss', parameter_size: '20B', quantization_level: null, cloud: true, installed: false },
  { name: 'gpt-oss:120b-cloud', size: 0, family: 'gpt-oss', parameter_size: '120B', quantization_level: null, cloud: true, installed: false },
];

const VOICE_ENGINES: { value: VoiceEngine; title: string; sub: string }[] = [
  { value: 'kokoro', title: 'Kokoro', sub: 'Local · fast' },
  { value: 'maya1', title: 'Maya-1', sub: 'Local · expressive' },
  { value: 'http', title: 'HTTP', sub: 'OpenAI-compatible API' },
  { value: 'hume', title: 'Hume', sub: 'Cloud · API key' },
];

const ANIMATIONS: { value: TypingAnimation; label: string; desc: string }[] = [
  { value: 'none', label: 'Instant', desc: 'No animation, just appear' },
  { value: 'typewriter', label: 'Typewriter', desc: 'One character at a time' },
  { value: 'word-fade', label: 'Word fade', desc: 'Words drift up and fade in' },
  { value: 'char-blur', label: 'Char blur', desc: 'Letters crystallise from blur' },
];

const BUILT_IN_TOOLS: [string, string][] = [
  ['take_screenshot', 'Capture the current screen'],
  ['read_file', 'Read a file from the filesystem'],
  ['write_file', 'Write or create a file'],
  ['run_command', 'Execute a shell command'],
  ['click', 'Click at screen coordinates'],
  ['type_text', 'Type using the keyboard'],
  ['browser_navigate', 'Navigate a browser to a URL'],
  ['search_files', 'Search files by name or content'],
  ['focus_window', 'Bring an application window to focus'],
  ['get_screen_text', 'Extract text visible on screen'],
];

interface OllamaModelInfo {
  name: string;
  size: number;
  modified_at?: string | null;
  family?: string | null;
  parameter_size?: string | null;
  quantization_level?: string | null;
  cloud: boolean;
  installed: boolean;
}

interface OllamaStatus {
  installed: boolean;
  running: boolean;
  version?: string | null;
  state: string;
  message: string;
  installed_path?: string | null;
  local_models: OllamaModelInfo[];
  cloud_models: OllamaModelInfo[];
  cloud_connected: boolean;
  cloud_message?: string | null;
  selected_model?: string | null;
  selected_model_available: boolean;
  selected_mode: string;
  context_limit?: number | null;
  context_limit_source?: string | null;
  error?: string | null;
}

interface OllamaPullProgress {
  model: string;
  line: string;
}

interface OllamaPullFinished {
  model: string;
  success: boolean;
  error?: string | null;
}

// ── Icons ─────────────────────────────────────────────────────────────────────

function NavIcon({ name }: { name: PaneId }) {
  const stroke = {
    stroke: 'currentColor',
    strokeWidth: 1.3,
    fill: 'none',
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
  };
  switch (name) {
    case 'appearance':
      return (
        <svg width="15" height="15" viewBox="0 0 16 16">
          <path
            {...stroke}
            d="M8 1.5c3.5 0 6.5 2.5 6.5 5.5 0 2-1.5 3-3 3h-1c-.7 0-1 .5-1 1s.3 1 .7 1.3c.5.3.8.7.8 1.2 0 .8-.7 1.5-1.5 1.5C5.5 14.5 1.5 11 1.5 7 1.5 4 4.5 1.5 8 1.5z"
          />
          <circle {...stroke} cx="5" cy="6.5" r=".8" />
          <circle {...stroke} cx="8" cy="4.5" r=".8" />
          <circle {...stroke} cx="11" cy="6.5" r=".8" />
        </svg>
      );
    case 'ai':
      return (
        <svg width="15" height="15" viewBox="0 0 16 16">
          <path {...stroke} d="M8 1.5v3M8 11.5v3M1.5 8h3M11.5 8h3M3.5 3.5l2 2M10.5 10.5l2 2M3.5 12.5l2-2M10.5 5.5l2-2" />
        </svg>
      );
    case 'voice':
      return (
        <svg width="15" height="15" viewBox="0 0 16 16">
          <rect {...stroke} x="6" y="2" width="4" height="8" rx="2" />
          <path {...stroke} d="M3 7.5c0 2.8 2.2 5 5 5s5-2.2 5-5M8 12.5v2" />
        </svg>
      );
    case 'paths':
      return (
        <svg width="15" height="15" viewBox="0 0 16 16">
          <path {...stroke} d="M1.5 4.5a1 1 0 011-1h3l1.5 1.5h6a1 1 0 011 1v6a1 1 0 01-1 1h-10.5a1 1 0 01-1-1v-7.5z" />
        </svg>
      );
    case 'data':
      return (
        <svg width="15" height="15" viewBox="0 0 16 16">
          <ellipse {...stroke} cx="8" cy="3.5" rx="5.5" ry="2" />
          <path {...stroke} d="M2.5 3.5v9c0 1.1 2.5 2 5.5 2s5.5-.9 5.5-2v-9M2.5 8c0 1.1 2.5 2 5.5 2s5.5-.9 5.5-2" />
        </svg>
      );
    case 'account':
      return (
        <svg width="15" height="15" viewBox="0 0 16 16">
          <circle {...stroke} cx="8" cy="5.5" r="2.5" />
          <path {...stroke} d="M2.5 14c0-2.5 2.5-4.5 5.5-4.5s5.5 2 5.5 4.5" />
        </svg>
      );
    case 'mcp':
      return (
        <svg width="15" height="15" viewBox="0 0 16 16">
          <path {...stroke} d="M5.5 1.5v3M10.5 1.5v3M3.5 4.5h9v3a4.5 4.5 0 01-9 0v-3zM8 12v2.5" />
        </svg>
      );
    case 'feedback':
      return (
        <svg width="15" height="15" viewBox="0 0 16 16">
          <path {...stroke} d="M2 4.5a2 2 0 012-2h8a2 2 0 012 2V10a2 2 0 01-2 2H6l-3 2.5V12h-1a1 1 0 010-2V4.5z" />
        </svg>
      );
    case 'about':
      return (
        <svg width="15" height="15" viewBox="0 0 16 16">
          <circle {...stroke} cx="8" cy="8" r="6.5" />
          <path {...stroke} d="M8 7v4M8 5v.5" />
        </svg>
      );
  }
}

// ── Primitives ────────────────────────────────────────────────────────────────

function PaneHeader({ title, subtitle, onClose }: { title: string; subtitle?: string; onClose?: () => void }) {
  return (
    <div
      style={{
        marginBottom: 26,
        paddingBottom: 18,
        borderBottom: '1px solid var(--kim-border)',
        display: 'flex',
        alignItems: 'flex-end',
        justifyContent: 'space-between',
        gap: 12,
      }}
    >
      <div>
        <h2 style={{ margin: 0, fontSize: 22, fontWeight: 500, letterSpacing: '-0.015em', color: 'var(--kim-text)' }}>
          {title}
        </h2>
        {subtitle && <p style={{ margin: '4px 0 0', color: 'var(--kim-text-3)', fontSize: 13 }}>{subtitle}</p>}
      </div>
      {onClose && (
        <button type="button" className="kr-icon-btn" onClick={onClose} style={{ width: 28, height: 28 }} aria-label="Close">
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
            <path d="M3 3l6 6M9 3l-6 6" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
          </svg>
        </button>
      )}
    </div>
  );
}

function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <div className="kr-eyebrow" style={{ marginBottom: 12 }}>
      {children}
    </div>
  );
}

function Row({
  title,
  subtitle,
  children,
  danger,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  danger?: boolean;
}) {
  return (
    <div
      className={danger ? 'kr-danger' : ''}
      style={{
        background: 'var(--kim-surface)',
        border: '1px solid var(--kim-border)',
        borderRadius: 12,
        padding: '14px 16px',
        display: 'flex',
        alignItems: 'center',
        gap: 18,
        marginBottom: 10,
        paddingLeft: danger ? 14 : 16,
      }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 14, color: 'var(--kim-text)', marginBottom: subtitle ? 3 : 0 }}>{title}</div>
        {subtitle && <div style={{ fontSize: 12.5, color: 'var(--kim-text-3)', lineHeight: 1.5 }}>{subtitle}</div>}
      </div>
      <div style={{ flexShrink: 0 }}>{children}</div>
    </div>
  );
}

function Toggle({ on, onClick }: { on: boolean; onClick: () => void }) {
  return <button type="button" className={`kr-toggle${on ? ' kr-on' : ''}`} onClick={onClick} aria-pressed={on} />;
}

// ── Panes ─────────────────────────────────────────────────────────────────────

function PaneAppearance({ settings, onChange }: { settings: Settings; onChange: (s: Settings) => void }) {
  function update<K extends keyof Settings>(key: K, value: Settings[K]) {
    onChange({ ...settings, [key]: value });
  }
  const themes: { id: Theme; lbl: string; icon: ReactNode }[] = [
    {
      id: 'light',
      lbl: 'Light',
      icon: (
        <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
          <circle cx="9" cy="9" r="3.5" stroke="currentColor" strokeWidth="1.3" />
          <path
            d="M9 2v1.5M9 14.5V16M2 9h1.5M14.5 9H16M3.5 3.5l1.1 1.1M13.4 13.4l1.1 1.1M3.5 14.5l1.1-1.1M13.4 4.6l1.1-1.1"
            stroke="currentColor"
            strokeWidth="1.3"
            strokeLinecap="round"
          />
        </svg>
      ),
    },
    {
      id: 'system',
      lbl: 'System',
      icon: (
        <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
          <rect x="2.5" y="3" width="13" height="9" rx="1" stroke="currentColor" strokeWidth="1.3" />
          <path d="M6 15h6M9 12v3" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
        </svg>
      ),
    },
    {
      id: 'dark',
      lbl: 'Dark',
      icon: (
        <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
          <path d="M14 10.5A6 6 0 117.5 4a5 5 0 006.5 6.5z" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
        </svg>
      ),
    },
  ];
  const accent = settings.accent ?? 'indigo';
  const anim = settings.typing_animation ?? 'none';
  const accentName = ACCENTS.find((a) => a.value === accent)?.label ?? '';
  return (
    <>
      <SectionLabel>theme</SectionLabel>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 10, marginBottom: 28 }}>
        {themes.map((t) => (
          <button
            key={t.id}
            type="button"
            className={`kr-tile${settings.theme === t.id ? ' kr-on' : ''}`}
            onClick={() => update('theme', t.id)}
            style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8, padding: '18px 0' }}
          >
            <span style={{ color: settings.theme === t.id ? 'var(--kim-accent)' : 'var(--kim-text-2)' }}>{t.icon}</span>
            <span style={{ fontSize: 13, color: 'var(--kim-text)' }}>{t.lbl}</span>
          </button>
        ))}
      </div>

      <SectionLabel>accent</SectionLabel>
      <div style={{ display: 'flex', gap: 14, alignItems: 'center', marginBottom: 6, flexWrap: 'wrap' }}>
        {ACCENTS.map((a) => (
          <button
            key={a.value}
            type="button"
            className={`kr-swatch${accent === a.value ? ' kr-on' : ''}`}
            onClick={() => update('accent', a.value)}
            style={{ background: a.color }}
            title={a.label}
            aria-label={a.label}
          >
            {accent === a.value && (
              <svg
                width="14"
                height="14"
                viewBox="0 0 12 12"
                fill="none"
                style={{ position: 'absolute', inset: 0, margin: 'auto', color: 'var(--kim-on-accent)' }}
              >
                <path d="M2.5 6L5 8.5L9.5 3.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            )}
          </button>
        ))}
      </div>
      <div style={{ marginBottom: 28, fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace', fontSize: 12, color: 'var(--kim-text-3)' }}>
        {accentName}
      </div>

      <SectionLabel>message animation</SectionLabel>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginBottom: 10 }}>
        {ANIMATIONS.map((a) => (
          <button
            key={a.value}
            type="button"
            className={`kr-tile${anim === a.value ? ' kr-on' : ''}`}
            onClick={() => update('typing_animation', a.value)}
          >
            <div style={{ fontSize: 14, color: 'var(--kim-text)', marginBottom: 4 }}>{a.label}</div>
            <div style={{ fontSize: 12, color: 'var(--kim-text-3)' }}>{a.desc}</div>
          </button>
        ))}
      </div>
      <div style={{ fontSize: 12, color: 'var(--kim-text-3)', marginTop: 8 }}>Applies to the newest message only.</div>
    </>
  );
}

function PaneAI({ settings, onChange }: { settings: Settings; onChange: (s: Settings) => void }) {
  function update<K extends keyof Settings>(key: K, value: Settings[K]) {
    onChange({ ...settings, [key]: value });
  }
  function updateOllama(patch: Partial<Settings['ollama']>) {
    onChange({ ...settings, ollama: { ...settings.ollama, ...patch } });
  }

  const [ollamaStatus, setOllamaStatus] = useState<OllamaStatus | null>(null);
  const [ollamaBusy, setOllamaBusy] = useState<'idle' | 'refreshing' | 'signin' | 'testing' | 'pulling'>('idle');
  const [ollamaError, setOllamaError] = useState<string | null>(null);
  const [ollamaPullLog, setOllamaPullLog] = useState<string[]>([]);
  const [showModelDetails, setShowModelDetails] = useState(false);
  const [showOllamaAdvanced, setShowOllamaAdvanced] = useState(false);

  async function refreshOllamaStatus() {
    setOllamaBusy(prev => (prev === 'pulling' ? prev : 'refreshing'));
    setOllamaError(null);
    try {
      const selectedModel =
        settings.ollama.mode === 'cloud'
          ? settings.ollama.cloud_model
          : settings.ollama.local_model;
      const status = await invoke<OllamaStatus>('ollama_get_status', {
        baseUrl: settings.ollama.base_url || null,
        selectedModel: selectedModel || null,
        mode: settings.ollama.mode,
        contextLimitOverride: settings.ollama.context_limit_override ?? null,
      });
      setOllamaStatus(status);
      const nextPatch: Partial<Settings['ollama']> = {};
      if (!settings.ollama.local_model && status.local_models[0]?.name) {
        nextPatch.local_model = status.local_models[0].name;
      }
      if (settings.ollama.connected !== (status.running && status.selected_mode === 'local' && status.local_models.length > 0)) {
        nextPatch.connected = status.running && status.selected_mode === 'local' && status.local_models.length > 0;
      }
      if (settings.ollama.cloud_connected !== status.cloud_connected) {
        nextPatch.cloud_connected = status.cloud_connected;
      }
      if (Object.keys(nextPatch).length > 0) updateOllama(nextPatch);
    } catch (err) {
      setOllamaError(String(err));
    } finally {
      setOllamaBusy(prev => (prev === 'pulling' ? prev : 'idle'));
    }
  }

  useEffect(() => {
    void refreshOllamaStatus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    settings.ollama.base_url,
    settings.ollama.mode,
    settings.ollama.local_model,
    settings.ollama.cloud_model,
    settings.ollama.context_limit_override,
  ]);

  useEffect(() => {
    let unlistenProgress: (() => void) | undefined;
    let unlistenFinished: (() => void) | undefined;

    listen<OllamaPullProgress>('ollama-pull-progress', (event) => {
      setOllamaPullLog((prev) => [...prev.slice(-24), event.payload.line]);
    }).then((fn) => { unlistenProgress = fn; });

    listen<OllamaPullFinished>('ollama-pull-finished', (event) => {
      setOllamaBusy('idle');
      if (event.payload.success) {
        toast(`Pulled ${event.payload.model}.`, 'success', 3000);
        void refreshOllamaStatus();
      } else {
        setOllamaError(event.payload.error ?? `Could not pull ${event.payload.model}.`);
      }
    }).then((fn) => { unlistenFinished = fn; });

    return () => {
      unlistenProgress?.();
      unlistenFinished?.();
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selectedModel = settings.ollama.mode === 'cloud' ? settings.ollama.cloud_model : settings.ollama.local_model;
  const selectedLocal = ollamaStatus?.local_models.find((m) => m.name === settings.ollama.local_model) ?? null;
  const selectedCloud = ollamaStatus?.cloud_models.find((m) => m.name === settings.ollama.cloud_model) ?? null;
  const detailModel = settings.ollama.mode === 'cloud' ? selectedCloud : selectedLocal;
  const localModelOptions = [
    ...(ollamaStatus?.local_models ?? []),
    ...OLLAMA_SUGGESTED_LOCAL_MODELS.filter((candidate) =>
      !(ollamaStatus?.local_models ?? []).some((model) => model.name === candidate.name)
    ),
  ];
  const cloudModelOptions = [
    ...(ollamaStatus?.cloud_models ?? []),
    ...OLLAMA_SUGGESTED_CLOUD_MODELS.filter((candidate) =>
      !(ollamaStatus?.cloud_models ?? []).some((model) => model.name === candidate.name)
    ),
  ];
  const activeModelOptions = settings.ollama.mode === 'cloud' ? cloudModelOptions : localModelOptions;
  const statusTone =
    ollamaStatus?.state === 'connected'
      ? 'var(--kim-green)'
      : ollamaStatus?.state === 'not_installed' || ollamaStatus?.state === 'installed_not_running'
        ? 'var(--kim-red)'
        : 'var(--kim-accent)';

  return (
    <>
      <SectionLabel>default provider</SectionLabel>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginBottom: 28 }}>
        {PROVIDERS.map((p) => {
          const on = settings.provider === p.value;
          return (
            <button
              key={p.value}
              type="button"
              className={`kr-tile${on ? ' kr-on' : ''}`}
              onClick={() => update('provider', p.value)}
              style={{ display: 'flex', alignItems: 'center', gap: 12 }}
            >
              <div
                style={{
                  width: 36,
                  height: 36,
                  borderRadius: 9,
                  background: 'var(--kim-bg-2)',
                  border: '1px solid var(--kim-border)',
                  display: 'inline-flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
                  fontSize: 14,
                  color: 'var(--kim-accent)',
                }}
              >
                {p.title[0]}
              </div>
              <div style={{ flex: 1, textAlign: 'left' }}>
                <div style={{ fontSize: 14 }}>{p.title}</div>
                <div
                  style={{
                    fontSize: 11.5,
                    color: 'var(--kim-text-3)',
                    fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
                    marginTop: 2,
                  }}
                >
                  {p.sub}
                </div>
              </div>
              {on && <div style={{ width: 8, height: 8, borderRadius: 999, background: 'var(--kim-green)' }} />}
            </button>
          );
        })}
      </div>

      <SectionLabel>ollama</SectionLabel>
      <div
        style={{
          background: 'var(--kim-surface)',
          border: '1px solid var(--kim-border)',
          borderRadius: 12,
          padding: '14px 16px',
          marginBottom: 28,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12, marginBottom: 14 }}>
          <div>
            <div style={{ fontSize: 15, color: 'var(--kim-text)', marginBottom: 4 }}>Ollama local/cloud via localhost</div>
            <div style={{ fontSize: 12.5, color: 'var(--kim-text-3)', lineHeight: 1.5 }}>
              Kim talks only to the local Ollama daemon at `localhost`. No API key, no custom OAuth, and no stored Ollama credentials.
            </div>
          </div>
          <div
            style={{
              padding: '6px 10px',
              borderRadius: 999,
              border: '1px solid var(--kim-border)',
              fontSize: 11.5,
              color: statusTone,
              fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
              whiteSpace: 'nowrap',
            }}
          >
            {ollamaStatus?.message ?? 'Checking Ollama…'}
          </div>
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1.4fr', gap: 12, marginBottom: 12 }}>
          <div>
            <div style={{ fontSize: 12, color: 'var(--kim-text-3)', marginBottom: 6 }}>account</div>
            <button
              type="button"
              className="kr-btn"
              style={{ width: '100%', justifyContent: 'center' }}
              onClick={async () => {
                setOllamaBusy('signin');
                setOllamaError(null);
                try {
                  await invoke('ollama_signin');
                  toast('Ollama sign-in launched. Finish the Ollama flow, then Kim will re-check the daemon.', 'info', 5000);
                  window.setTimeout(() => { void refreshOllamaStatus(); }, 2500);
                  window.setTimeout(() => { void refreshOllamaStatus(); }, 7000);
                } catch (err) {
                  setOllamaError(String(err));
                } finally {
                  setOllamaBusy('idle');
                }
              }}
              disabled={ollamaBusy !== 'idle'}
            >
              Sign in to Ollama
            </button>
          </div>
          <div>
            <div style={{ fontSize: 12, color: 'var(--kim-text-3)', marginBottom: 6 }}>mode</div>
            <div className="kr-seg" style={{ width: '100%' }}>
              <button
                type="button"
                className={settings.ollama.mode === 'cloud' ? 'kr-on' : ''}
                onClick={() => updateOllama({ mode: 'cloud' })}
              >
                Cloud
              </button>
              <button
                type="button"
                className={settings.ollama.mode === 'local' ? 'kr-on' : ''}
                onClick={() => updateOllama({ mode: 'local' })}
              >
                Local
              </button>
            </div>
          </div>
          <div>
            <div style={{ fontSize: 12, color: 'var(--kim-text-3)', marginBottom: 6 }}>model</div>
            <select
              className="kr-input"
              value={selectedModel}
              onChange={(e) => {
                if (settings.ollama.mode === 'cloud') {
                  updateOllama({ cloud_model: e.target.value });
                } else {
                  updateOllama({ local_model: e.target.value });
                }
              }}
            >
              {settings.ollama.mode === 'local' && <option value="">Pick a local model…</option>}
              {activeModelOptions.map((model) => (
                <option key={model.name} value={model.name}>
                  {model.name}
                  {model.parameter_size ? ` · ${model.parameter_size}` : ''}
                  {model.quantization_level ? ` · ${model.quantization_level}` : ''}
                  {settings.ollama.mode === 'local' && !model.installed ? ' · pull to install' : ''}
                  {settings.ollama.mode === 'cloud' && model.installed ? ' · ready' : ''}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
          <button type="button" className="kr-btn" onClick={() => setShowOllamaAdvanced((v) => !v)}>
            {showOllamaAdvanced ? 'Hide additional options' : 'Additional options'}
          </button>
          {settings.ollama.mode === 'local' && (
            <button type="button" className="kr-btn" onClick={() => setShowOllamaAdvanced(true)}>
              Configure local
            </button>
          )}
        </div>

        {showOllamaAdvanced && (
          <div style={{ borderTop: '1px solid var(--kim-border)', paddingTop: 12, marginBottom: 12 }}>
            <div style={{ display: 'grid', gridTemplateColumns: '1.2fr 1fr', gap: 12, marginBottom: 12 }}>
              <div>
                <div style={{ fontSize: 12, color: 'var(--kim-text-3)', marginBottom: 6 }}>base url</div>
                <input
                  className="kr-input"
                  value={settings.ollama.base_url}
                  onChange={(e) => updateOllama({ base_url: e.target.value })}
                  placeholder="http://localhost:11434"
                />
              </div>
              <div>
                <div style={{ fontSize: 12, color: 'var(--kim-text-3)', marginBottom: 6 }}>context limit</div>
                <div style={{ fontSize: 13.5, color: 'var(--kim-text)' }}>
                  {ollamaStatus?.context_limit
                    ? `${ollamaStatus.context_limit.toLocaleString()} tokens`
                    : 'context limit unknown'}
                </div>
                <div style={{ fontSize: 11.5, color: 'var(--kim-text-3)', marginTop: 4 }}>
                  {ollamaStatus?.context_limit_source === 'ollama_ps'
                    ? 'Detected from running model'
                    : ollamaStatus?.context_limit_source === 'api_show'
                      ? 'Detected from model details'
                      : ollamaStatus?.context_limit_source === 'override'
                        ? 'Using your override'
                        : 'No reliable limit detected yet'}
                </div>
              </div>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
              <div>
                <div style={{ fontSize: 12, color: 'var(--kim-text-3)', marginBottom: 6 }}>optional context override</div>
                <input
                  className="kr-input"
                  type="number"
                  min={0}
                  step={1024}
                  value={settings.ollama.context_limit_override ?? ''}
                  onChange={(e) => {
                    const raw = e.target.value.trim();
                    if (!raw) {
                      updateOllama({ context_limit_override: null });
                      return;
                    }
                    const n = Number.parseInt(raw, 10);
                    if (Number.isFinite(n) && n > 0) updateOllama({ context_limit_override: n });
                  }}
                  placeholder="leave empty"
                />
              </div>
            </div>

            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
              <button type="button" className="kr-btn" onClick={() => void refreshOllamaStatus()} disabled={ollamaBusy !== 'idle'}>
                Refresh models
              </button>
              <button
                type="button"
                className="kr-btn"
                onClick={async () => {
                  if (!selectedModel) {
                    setOllamaError('Pick a model first.');
                    return;
                  }
                  setOllamaBusy('testing');
                  setOllamaError(null);
                  try {
                    await invoke('ollama_test_model', { baseUrl: settings.ollama.base_url || null, model: selectedModel });
                    toast(`Ollama model ${selectedModel} replied successfully.`, 'success', 3500);
                    await refreshOllamaStatus();
                  } catch (err) {
                    setOllamaError(String(err));
                  } finally {
                    setOllamaBusy('idle');
                  }
                }}
                disabled={ollamaBusy !== 'idle'}
              >
                Test model
              </button>
              <button
                type="button"
                className="kr-btn"
                onClick={async () => {
                  if (!selectedModel) {
                    setOllamaError('Pick a model first.');
                    return;
                  }
                  setOllamaBusy('pulling');
                  setOllamaPullLog([]);
                  setOllamaError(null);
                  try {
                    await invoke('ollama_pull_model', { model: selectedModel });
                  } catch (err) {
                    setOllamaBusy('idle');
                    setOllamaError(String(err));
                  }
                }}
                disabled={ollamaBusy !== 'idle'}
              >
                Pull model
              </button>
              <button
                type="button"
                className="kr-btn"
                onClick={() => setShowModelDetails((v) => !v)}
              >
                {showModelDetails ? 'Hide model details' : 'Show model details'}
              </button>
            </div>
          </div>
        )}

        <div
          style={{
            padding: '10px 12px',
            borderRadius: 10,
            border: '1px solid var(--kim-border)',
            background: 'var(--kim-bg-2)',
            fontSize: 12.5,
            color: 'var(--kim-text-2)',
            lineHeight: 1.55,
            marginBottom: showModelDetails || ollamaPullLog.length > 0 || ollamaError ? 12 : 0,
          }}
        >
          <div>{ollamaStatus?.message ?? 'Checking Ollama…'}</div>
          <div>{settings.ollama.mode === 'local' ? 'Local models work without any Ollama account.' : (ollamaStatus?.cloud_message ?? 'Sign in to Ollama to use cloud models')}</div>
          <div>{settings.ollama.mode === 'local' ? 'Local: no API billing' : 'Cloud account usage is managed by Ollama. Kim can show token usage, not remaining account balance.'}</div>
        </div>

        {showModelDetails && (
          <div
            style={{
              padding: '10px 12px',
              borderRadius: 10,
              border: '1px solid var(--kim-border)',
              background: 'var(--kim-bg-2)',
              marginBottom: 12,
              fontSize: 12,
              color: 'var(--kim-text-2)',
              lineHeight: 1.6,
              fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
            }}
          >
            <div>selected: {selectedModel || 'none'}</div>
            <div>available: {ollamaStatus?.selected_model_available ? 'yes' : 'no'}</div>
            <div>version: {ollamaStatus?.version || 'unknown'}</div>
            <div>path: {ollamaStatus?.installed_path || 'not found'}</div>
            <div>family: {detailModel?.family || 'unknown'}</div>
            <div>params: {detailModel?.parameter_size || 'unknown'}</div>
            <div>quant: {detailModel?.quantization_level || 'unknown'}</div>
            <div>size: {detailModel?.size ? `${(detailModel.size / (1024 ** 3)).toFixed(2)} GB` : 'unknown'}</div>
          </div>
        )}

        {ollamaPullLog.length > 0 && (
          <div
            style={{
              padding: '10px 12px',
              borderRadius: 10,
              border: '1px solid var(--kim-border)',
              background: 'var(--kim-bg-2)',
              marginBottom: 12,
              maxHeight: 180,
              overflowY: 'auto',
              fontSize: 11.5,
              color: 'var(--kim-text-2)',
              fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
              whiteSpace: 'pre-wrap',
            }}
          >
            {ollamaPullLog.join('\n')}
          </div>
        )}

        {ollamaError && (
          <div
            style={{
              padding: '10px 12px',
              borderRadius: 10,
              border: '1px solid var(--kim-red)',
              background: 'rgba(212,148,151,0.06)',
              color: 'var(--kim-red)',
              fontSize: 12.5,
            }}
          >
            {ollamaError}
          </div>
        )}
      </div>

      <SectionLabel>behavior</SectionLabel>
      <Row
        title="Queue messages while Kim is working"
        subtitle="Off: a new send interrupts the current task. On: sends queue and run in order."
      >
        <Toggle on={settings.allow_message_queue} onClick={() => update('allow_message_queue', !settings.allow_message_queue)} />
      </Row>
      <Row
        title="Keep browser visible while running"
        subtitle="Testing only — leaves the provider window on-screen so you can watch what Kim does."
      >
        <Toggle on={settings.keep_browser_visible} onClick={() => update('keep_browser_visible', !settings.keep_browser_visible)} />
      </Row>

      <SectionLabel>context budget</SectionLabel>
      <div
        style={{
          background: 'var(--kim-surface)',
          border: '1px solid var(--kim-border)',
          borderRadius: 12,
          padding: '14px 16px',
        }}
      >
        <div style={{ fontSize: 14, color: 'var(--kim-text)', marginBottom: 8 }}>Cumulative input tokens</div>
        <input
          type="number"
          min={10_000}
          step={1000}
          value={settings.context_budget_tokens ?? 200_000}
          onChange={(e) => {
            const n = Number.parseInt(e.target.value, 10);
            if (Number.isFinite(n) && n >= 10_000) update('context_budget_tokens', n);
          }}
          onBlur={(e) => {
            const n = Number.parseInt(e.target.value, 10);
            if (!Number.isFinite(n) || n < 10_000) update('context_budget_tokens', 200_000);
          }}
          className="kr-input"
        />
        <div style={{ fontSize: 12, color: 'var(--kim-text-3)', marginTop: 8, lineHeight: 1.5 }}>
          Kim warns when cumulative input crosses ~80% / ~95% of this budget, then offers to compact.
        </div>
      </div>
    </>
  );
}

function PaneVoice({ settings, onChange }: { settings: Settings; onChange: (s: Settings) => void }) {
  const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');
  const [voiceError, setVoiceError] = useState<string | null>(null);

  useEffect(() => {
    invoke<VoiceSettings>('read_voice_config', { projectRoot: settings.project_root || null })
      .then((cfg) => onChange({ ...settings, voice: { ...settings.voice, ...cfg } }))
      .catch((err) => setVoiceError(`Failed to read config.yaml: ${String(err)}`));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function updateVoice<K extends keyof VoiceSettings>(key: K, value: VoiceSettings[K]) {
    const next: VoiceSettings = { ...settings.voice, [key]: value };
    if (key === 'engine') {
      const vs = VOICES_BY_ENGINE[value as VoiceEngine];
      if (vs.length > 0) next.voice_id = vs[0].value;
    }
    onChange({ ...settings, voice: next });
    setSaveState('saving');
    setVoiceError(null);
    try {
      await invoke('write_voice_config', { config: next, projectRoot: settings.project_root || null });
      setSaveState('saved');
      setTimeout(() => setSaveState('idle'), 1500);
    } catch (err) {
      setSaveState('error');
      setVoiceError(String(err));
    }
  }

  const voices = VOICES_BY_ENGINE[settings.voice.engine] ?? [];

  return (
    <>
      <Row title="Enable voice" subtitle="Speak completions, errors, and confirmations aloud.">
        <Toggle on={settings.voice.enabled} onClick={() => updateVoice('enabled', !settings.voice.enabled)} />
      </Row>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginTop: 22 }}>
        <div>
          <SectionLabel>engine</SectionLabel>
          {VOICE_ENGINES.map((e) => (
            <button
              key={e.value}
              type="button"
              className={`kr-tile${settings.voice.engine === e.value ? ' kr-on' : ''}`}
              onClick={() => updateVoice('engine', e.value)}
              disabled={!settings.voice.enabled}
              style={{ marginBottom: 8, width: '100%' }}
            >
              <div style={{ fontSize: 13.5 }}>{e.title}</div>
              <div
                style={{
                  fontSize: 11.5,
                  color: 'var(--kim-text-3)',
                  fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
                  marginTop: 2,
                }}
              >
                {e.sub}
              </div>
            </button>
          ))}
        </div>
        <div>
          <SectionLabel>voice · {voices.length} available</SectionLabel>
          <div style={{ maxHeight: 380, overflowY: 'auto' }}>
            {voices.map((v) => (
              <button
                key={v.value}
                type="button"
                className={`kr-tile${settings.voice.voice_id === v.value ? ' kr-on' : ''}`}
                onClick={() => updateVoice('voice_id', v.value)}
                disabled={!settings.voice.enabled}
                style={{ marginBottom: 8, display: 'flex', alignItems: 'center', gap: 10, width: '100%' }}
              >
                <span
                  style={{
                    width: 28,
                    height: 28,
                    borderRadius: 8,
                    border: '1px solid var(--kim-border)',
                    background: 'var(--kim-bg-2)',
                    display: 'inline-flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                  }}
                >
                  <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
                    <path d="M3 2v6l5-3z" fill="currentColor" />
                  </svg>
                </span>
                <div style={{ flex: 1, textAlign: 'left' }}>
                  <div style={{ fontSize: 13.5 }}>{v.label}</div>
                </div>
                {settings.voice.voice_id === v.value && <span style={{ color: 'var(--kim-accent)', fontSize: 11 }}>✓</span>}
              </button>
            ))}
          </div>
        </div>
      </div>

      {voiceError && (
        <div
          style={{
            marginTop: 12,
            padding: '10px 14px',
            border: '1px solid var(--kim-red)',
            borderRadius: 10,
            color: 'var(--kim-red)',
            fontSize: 12.5,
            background: 'rgba(212,148,151,0.05)',
          }}
        >
          {voiceError}
        </div>
      )}
      {saveState === 'saving' && (
        <div style={{ marginTop: 12, fontSize: 12, color: 'var(--kim-text-3)' }}>Saving…</div>
      )}
      {saveState === 'saved' && (
        <div style={{ marginTop: 12, fontSize: 12, color: 'var(--kim-green)' }}>Saved</div>
      )}
    </>
  );
}

function PathRow({
  label,
  value,
  hint,
  onPick,
  onChange,
}: {
  label: string;
  value: string;
  hint: string;
  onPick: () => void;
  onChange: (v: string) => void;
}) {
  return (
    <div style={{ marginBottom: 18 }}>
      <SectionLabel>{label}</SectionLabel>
      <div style={{ display: 'flex', gap: 8 }}>
        <input
          className="kr-input"
          style={{ flex: 1 }}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="(default)"
        />
        <button type="button" className="kr-btn" onClick={onPick} title="Browse…">
          <svg width="15" height="15" viewBox="0 0 16 16" fill="none">
            <path
              d="M1.5 4.5a1 1 0 011-1h3l1.5 1.5h6a1 1 0 011 1v6a1 1 0 01-1 1h-10.5a1 1 0 01-1-1v-7.5z"
              stroke="currentColor"
              strokeWidth="1.3"
            />
          </svg>
        </button>
      </div>
      <div style={{ fontSize: 12, color: 'var(--kim-text-3)', marginTop: 6 }}>{hint}</div>
    </div>
  );
}

function PanePaths({ settings, onChange }: { settings: Settings; onChange: (s: Settings) => void }) {
  function update<K extends keyof Settings>(key: K, value: Settings[K]) {
    onChange({ ...settings, [key]: value });
  }
  async function pick(key: keyof Settings) {
    try {
      const selected = await openDialog({ directory: true, multiple: false, title: 'Select folder' });
      if (selected && typeof selected === 'string') update(key, selected as Settings[typeof key]);
    } catch {
      // cancelled
    }
  }
  return (
    <>
      <PathRow
        label="Kim sessions directory"
        value={settings.kim_sessions_dir}
        hint="Leave empty to use the default (~/.kim/sessions)."
        onPick={() => pick('kim_sessions_dir')}
        onChange={(v) => update('kim_sessions_dir', v)}
      />
      <PathRow
        label="Code sessions directory"
        value={settings.claw_sessions_dir}
        hint="Path where Claw stores its JSONL session files."
        onPick={() => pick('claw_sessions_dir')}
        onChange={(v) => update('claw_sessions_dir', v)}
      />
      <PathRow
        label="Project root"
        value={settings.project_root}
        hint="Root of your Kim installation (where orchestrator/ lives). Leave empty for auto-detect."
        onPick={() => pick('project_root')}
        onChange={(v) => update('project_root', v)}
      />
    </>
  );
}

function PaneData({ account }: { account: KimAccount }) {
  const [exportState, setExportState] = useState<'idle' | 'working' | 'done' | 'error'>('idle');
  const [gistState, setGistState] = useState<'idle' | 'working' | 'done' | 'error'>('idle');
  const [statusMsg, setStatusMsg] = useState('');
  const fileInputRef = useRef<HTMLInputElement>(null);
  const hasGitHub = !!account.github_token;

  async function handleExport(format: 'zip' | 'json' | 'markdown') {
    setExportState('working');
    setStatusMsg('');
    try {
      const path = await invoke<string>('export_data', { format });
      setExportState('done');
      setStatusMsg(`Saved to ${path}`);
      setTimeout(() => {
        setExportState('idle');
        setStatusMsg('');
      }, 3000);
    } catch (err) {
      setExportState('error');
      setStatusMsg(String(err));
    }
  }

  async function handleImport(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    const filePath = (file as File & { path?: string }).path ?? file.name;
    try {
      const count = await invoke<number>('import_data', { path: filePath });
      setStatusMsg(`Imported ${count} session${count !== 1 ? 's' : ''}`);
      setTimeout(() => setStatusMsg(''), 3000);
    } catch (err) {
      setStatusMsg(`Import failed: ${String(err)}`);
    }
    if (fileInputRef.current) fileInputRef.current.value = '';
  }

  async function handleGistBackup() {
    if (!account.github_token) return;
    setGistState('working');
    try {
      await invoke('backup_to_gist', { token: account.github_token, gistId: account.gist_id ?? null });
      setGistState('done');
      setTimeout(() => setGistState('idle'), 2000);
    } catch (err) {
      setGistState('error');
      setStatusMsg(`Backup failed: ${String(err)}`);
    }
  }

  async function handleGistRestore() {
    if (!account.github_token || !account.gist_id) return;
    setGistState('working');
    try {
      await invoke('restore_from_gist', { token: account.github_token, gistId: account.gist_id });
      setGistState('done');
      setTimeout(() => setGistState('idle'), 2000);
    } catch (err) {
      setGistState('error');
      setStatusMsg(`Restore failed: ${String(err)}`);
    }
  }

  return (
    <>
      <div
        style={{
          display: 'flex',
          gap: 12,
          padding: '12px 14px',
          borderRadius: 11,
          background: 'linear-gradient(90deg, var(--kim-accent-soft), transparent)',
          border: '1px solid var(--kim-accent-line)',
          borderLeft: '2px solid var(--kim-accent)',
          marginBottom: 22,
        }}
      >
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" style={{ color: 'var(--kim-accent)', flexShrink: 0, marginTop: 1 }}>
          <path
            d="M8 1.5l5.5 2.5v4.5c0 3-2.5 5.5-5.5 6-3-.5-5.5-3-5.5-6V4L8 1.5z"
            stroke="currentColor"
            strokeWidth="1.3"
          />
        </svg>
        <div style={{ fontSize: 13, color: 'var(--kim-text-2)', lineHeight: 1.5 }}>
          All data stays on this machine. Pick one of the methods below to sync or back it up.
        </div>
      </div>

      <SectionLabel>github gist sync</SectionLabel>
      {!hasGitHub ? (
        <div
          style={{
            fontSize: 12.5,
            color: 'var(--kim-text-3)',
            padding: '12px 14px',
            background: 'var(--kim-surface)',
            border: '1px dashed var(--kim-border)',
            borderRadius: 11,
            marginBottom: 22,
          }}
        >
          Connect a GitHub account in the <strong style={{ color: 'var(--kim-text-2)' }}>Account</strong> section to enable Gist sync.
        </div>
      ) : (
        <div style={{ display: 'flex', gap: 8, marginBottom: 22 }}>
          <button type="button" className="kr-btn" onClick={handleGistBackup} disabled={gistState === 'working'} style={{ flex: 1, justifyContent: 'center' }}>
            {gistState === 'working' ? 'Backing up…' : gistState === 'done' ? 'Backed up ✓' : 'Back up to Gist'}
          </button>
          <button
            type="button"
            className="kr-btn"
            onClick={handleGistRestore}
            disabled={gistState === 'working' || !account.gist_id}
            style={{ flex: 1, justifyContent: 'center' }}
            title={!account.gist_id ? 'No Gist backup found — run a backup first' : ''}
          >
            {gistState === 'working' ? 'Restoring…' : 'Restore from Gist'}
          </button>
        </div>
      )}

      <SectionLabel>export</SectionLabel>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 8, marginBottom: 6 }}>
        {(['zip', 'json', 'markdown'] as const).map((fmt) => (
          <button
            key={fmt}
            type="button"
            className="kr-btn"
            onClick={() => handleExport(fmt)}
            disabled={exportState === 'working'}
            style={{ padding: 12, justifyContent: 'center', fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace' }}
          >
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
              <path d="M6 1v8M3 6l3 3 3-3M1.5 11h9" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            .{fmt === 'markdown' ? 'md' : fmt}
          </button>
        ))}
      </div>
      <div style={{ fontSize: 11.5, color: 'var(--kim-text-3)', marginBottom: 22 }}>
        ZIP = raw JSONL · JSON = structured index · MD = human-readable.
      </div>

      <SectionLabel>import</SectionLabel>
      <button type="button" className="kr-btn" onClick={() => fileInputRef.current?.click()} style={{ width: '100%', justifyContent: 'center' }}>
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
          <path d="M6 11V3M3 6l3-3 3 3M1.5 1h9" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        Import from file (.zip or .json)
      </button>
      <input ref={fileInputRef} type="file" accept=".zip,.json" style={{ display: 'none' }} onChange={handleImport} />

      {statusMsg && (
        <div
          style={{
            marginTop: 12,
            padding: '10px 14px',
            border: `1px solid ${exportState === 'error' || gistState === 'error' ? 'var(--kim-red)' : 'var(--kim-accent-line)'}`,
            borderRadius: 10,
            color: exportState === 'error' || gistState === 'error' ? 'var(--kim-red)' : 'var(--kim-text-2)',
            fontSize: 12.5,
          }}
        >
          {statusMsg}
        </div>
      )}
    </>
  );
}

function PaneAccount({
  account,
  onAccountChange,
}: {
  account: KimAccount;
  onAccountChange: (a: KimAccount) => Promise<void>;
}) {
  const [nameVal, setNameVal] = useState(account.display_name);
  const [editingName, setEditingName] = useState(false);
  const [token, setToken] = useState('');
  const [verifying, setVerifying] = useState(false);
  const [tokenError, setTokenError] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setNameVal(account.display_name);
  }, [account.display_name]);

  async function saveName() {
    if (nameVal.trim() && nameVal !== account.display_name) {
      setSaving(true);
      try {
        await onAccountChange({ ...account, display_name: nameVal.trim() });
        toast('Name updated', 'success', 1800);
      } finally {
        setSaving(false);
      }
    }
    setEditingName(false);
  }

  async function verifyAndConnect() {
    if (!token.trim()) return;
    setVerifying(true);
    setTokenError('');
    try {
      const user = await invoke<{ login: string; name: string | null; avatar_url: string }>('verify_github_token', {
        token: token.trim(),
      });
      await onAccountChange({
        ...account,
        github_token: token.trim(),
        github_username: user.login,
        github_avatar_url: user.avatar_url,
      });
      setToken('');
      toast(`Connected as ${user.login}`, 'success', 2500);
    } catch (err) {
      setTokenError(String(err));
    } finally {
      setVerifying(false);
    }
  }

  async function disconnectGitHub() {
    if (!confirm('Disconnect GitHub? Gist sync will stop working.')) return;
    await onAccountChange({
      ...account,
      github_token: undefined,
      github_username: undefined,
      github_avatar_url: undefined,
      gist_id: undefined,
    });
    toast('GitHub disconnected', 'success', 2000);
  }

  async function resetOnboarding() {
    if (!confirm('Reset onboarding? Kim will forget your account and re-run the welcome flow next launch.')) return;
    try {
      await invoke('reset_onboarding');
      toast('Onboarding reset — restart Kim to see the welcome flow.', 'success', 4000);
    } catch (err) {
      toast(`Reset failed: ${String(err)}`, 'error', 4000);
    }
  }

  async function deleteAllSessions() {
    if (!confirm('Permanently delete ALL local session files? This cannot be undone.')) return;
    try {
      await invoke('delete_all_sessions');
      toast('All sessions deleted.', 'success', 3000);
    } catch (err) {
      toast(`Delete failed: ${String(err)}`, 'error', 4000);
    }
  }

  const initial = (account.display_name?.trim()[0] || 'A').toUpperCase();

  return (
    <>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 18,
          padding: 18,
          background: 'var(--kim-surface)',
          border: '1px solid var(--kim-border)',
          borderRadius: 12,
          marginBottom: 22,
        }}
      >
        <div
          style={{
            width: 56,
            height: 56,
            borderRadius: 999,
            background: 'linear-gradient(135deg, var(--kim-accent), #c98968)',
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: 'var(--kim-on-accent)',
            fontWeight: 700,
            fontSize: 22,
            overflow: 'hidden',
          }}
        >
          {account.github_avatar_url ? (
            <img src={account.github_avatar_url} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
          ) : (
            initial
          )}
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          {editingName ? (
            <input
              autoFocus
              value={nameVal}
              onChange={(e) => setNameVal(e.target.value)}
              onBlur={() => void saveName()}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void saveName();
                if (e.key === 'Escape') {
                  setNameVal(account.display_name);
                  setEditingName(false);
                }
              }}
              className="kr-input"
              style={{ fontSize: 15 }}
            />
          ) : (
            <div style={{ fontSize: 16, marginBottom: 3 }}>{account.display_name}</div>
          )}
          <div style={{ fontSize: 12.5, color: 'var(--kim-text-3)', fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace' }}>
            {account.github_username ? `github · ${account.github_username}` : 'local'}
          </div>
        </div>
        <button type="button" className="kr-btn" onClick={() => setEditingName(true)} disabled={saving}>
          {editingName ? 'Saving…' : 'Edit name'}
        </button>
      </div>

      <SectionLabel>integrations</SectionLabel>
      <Row
        title="GitHub"
        subtitle="Required for Gist backup sync. Use a personal access token with gist and read:user scopes."
      >
        {account.github_token ? (
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            <span
              style={{
                fontSize: 12,
                color: 'var(--kim-green)',
                fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
              }}
            >
              ● connected
            </span>
            <button type="button" className="kr-btn" onClick={disconnectGitHub}>
              Disconnect
            </button>
          </div>
        ) : (
          <button type="button" className="kr-btn kr-btn-primary" onClick={() => setToken('show')}>
            Connect
          </button>
        )}
      </Row>

      {!account.github_token && token === 'show' && (
        <div
          style={{
            background: 'var(--kim-surface)',
            border: '1px solid var(--kim-border)',
            borderRadius: 12,
            padding: '14px 16px',
            marginBottom: 10,
          }}
        >
          <div style={{ fontSize: 12.5, color: 'var(--kim-text-2)', marginBottom: 10, lineHeight: 1.5 }}>
            Paste a GitHub personal access token (scopes: <code>gist</code>, <code>read:user</code>). Create one at{' '}
            <span style={{ color: 'var(--kim-accent)' }}>github.com/settings/tokens</span>.
          </div>
          <input
            type="password"
            className="kr-input"
            placeholder="ghp_…"
            value={token === 'show' ? '' : token}
            onChange={(e) => setToken(e.target.value)}
            style={{ marginBottom: 10 }}
          />
          {tokenError && (
            <div style={{ fontSize: 12, color: 'var(--kim-red)', marginBottom: 10 }}>{tokenError}</div>
          )}
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <button type="button" className="kr-btn" onClick={() => { setToken(''); setTokenError(''); }}>
              Cancel
            </button>
            <button type="button" className="kr-btn kr-btn-primary" onClick={verifyAndConnect} disabled={verifying || !token.trim() || token === 'show'}>
              {verifying ? 'Verifying…' : 'Connect'}
            </button>
          </div>
        </div>
      )}

      <SectionLabel>danger zone</SectionLabel>
      <Row danger title="Reset onboarding" subtitle="Forget your account and re-run the welcome flow on next launch.">
        <button
          type="button"
          className="kr-btn"
          onClick={resetOnboarding}
          style={{ color: 'var(--kim-red)', borderColor: 'rgba(212,148,151,0.3)' }}
        >
          Reset…
        </button>
      </Row>
      <Row danger title="Delete all sessions" subtitle="Permanently wipe local JSONL files. This cannot be undone.">
        <button
          type="button"
          className="kr-btn"
          onClick={deleteAllSessions}
          style={{ color: 'var(--kim-red)', borderColor: 'rgba(212,148,151,0.3)' }}
        >
          Delete…
        </button>
      </Row>
    </>
  );
}

function PaneMCP() {
  return (
    <>
      <div
        style={{
          display: 'flex',
          gap: 14,
          marginBottom: 22,
          padding: 16,
          background: 'var(--kim-surface)',
          border: '1px solid var(--kim-border)',
          borderRadius: 12,
        }}
      >
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 14, marginBottom: 4 }}>What is MCP?</div>
          <div style={{ fontSize: 12.5, color: 'var(--kim-text-3)', lineHeight: 1.55 }}>
            Model Context Protocol — a standard for AI agents to talk to external tools. Each server is a plugin that gives Kim new abilities.
          </div>
        </div>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
        <SectionLabel>built-in tools · {BUILT_IN_TOOLS.length}</SectionLabel>
        <span
          style={{
            fontSize: 11,
            color: 'var(--kim-green)',
            fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
          }}
        >
          ● all connected
        </span>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, marginBottom: 22 }}>
        {BUILT_IN_TOOLS.map(([n, d]) => (
          <div
            key={n}
            style={{
              background: 'var(--kim-bg-2)',
              border: '1px solid var(--kim-border)',
              borderRadius: 9,
              padding: '10px 13px',
            }}
          >
            <div style={{ fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace', fontSize: 12.5, color: 'var(--kim-accent)' }}>
              {n}
            </div>
            <div style={{ fontSize: 11.5, color: 'var(--kim-text-3)', marginTop: 2 }}>{d}</div>
          </div>
        ))}
      </div>

      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
        <SectionLabel>custom servers</SectionLabel>
        <button type="button" className="kr-btn">
          <svg width="11" height="11" viewBox="0 0 12 12" fill="none">
            <path d="M6 1v10M1 6h10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
          Add server
        </button>
      </div>
      <div
        style={{
          fontSize: 12.5,
          color: 'var(--kim-text-3)',
          padding: '12px 14px',
          background: 'var(--kim-surface)',
          border: '1px dashed var(--kim-border)',
          borderRadius: 11,
        }}
      >
        No custom MCP servers configured. Add one to expose external tools to Kim.
      </div>
    </>
  );
}

function PaneFeedback() {
  const [kind, setKind] = useState<'bug' | 'feature' | 'general' | 'praise' | 'other'>('bug');
  const [message, setMessage] = useState('');
  const [contact, setContact] = useState('');
  const [sending, setSending] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!message.trim()) return;
    setSending(true);
    try {
      await invoke('submit_feedback', { kind, message: message.trim(), contact: contact.trim() || null });
      toast('Feedback sent. Thank you!', 'success', 3000);
      setMessage('');
      setContact('');
    } catch (err) {
      toast(`Send failed: ${String(err)}`, 'error', 4000);
    } finally {
      setSending(false);
    }
  }

  const fbIcon = (id: string, active: boolean) => {
    const sw = {
      stroke: 'currentColor',
      strokeWidth: 1.3,
      fill: 'none',
      strokeLinecap: 'round' as const,
      strokeLinejoin: 'round' as const,
    };
    const color = active ? 'var(--kim-accent)' : 'var(--kim-text-2)';
    switch (id) {
      case 'bug':
        return (
          <svg width="20" height="20" viewBox="0 0 20 20" style={{ color }}>
            <rect {...sw} x="6" y="6" width="8" height="9" rx="3.5" />
            <path {...sw} d="M10 6V4M8 5l-1.5-1.5M12 5l1.5-1.5M6 10H3.5M14 10h2.5M7 14l-2 2M13 14l2 2" />
          </svg>
        );
      case 'feature':
        return (
          <svg width="20" height="20" viewBox="0 0 20 20" style={{ color }}>
            <path {...sw} d="M10 2.5l1.6 4.2 4.4.5-3.3 2.9 1 4.4L10 12.3 6.3 14.5l1-4.4L4 7.2l4.4-.5L10 2.5z" />
          </svg>
        );
      case 'general':
        return (
          <svg width="20" height="20" viewBox="0 0 20 20" style={{ color }}>
            <path {...sw} d="M3.5 6a2 2 0 012-2h9a2 2 0 012 2v6a2 2 0 01-2 2H8l-3.5 3v-3a2 2 0 01-2-2V6z" />
            <path {...sw} d="M7 9h6M7 11.5h4" />
          </svg>
        );
      case 'praise':
        return (
          <svg width="20" height="20" viewBox="0 0 20 20" style={{ color }}>
            <path {...sw} d="M10 16.5c-3-2-7-4.5-7-8a3.5 3.5 0 016-2.4 3.5 3.5 0 016 2.4c0 3.5-4 6-7 8z" />
          </svg>
        );
      case 'other':
        return (
          <svg width="20" height="20" viewBox="0 0 20 20" style={{ color }}>
            <circle {...sw} cx="5" cy="10" r="1" />
            <circle {...sw} cx="10" cy="10" r="1" />
            <circle {...sw} cx="15" cy="10" r="1" />
          </svg>
        );
      default:
        return null;
    }
  };

  return (
    <form onSubmit={submit}>
      <SectionLabel>what kind of feedback?</SectionLabel>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 8, marginBottom: 22 }}>
        {(['bug', 'feature', 'general', 'praise', 'other'] as const).map((id) => (
          <button
            key={id}
            type="button"
            onClick={() => setKind(id)}
            className={`kr-tile${kind === id ? ' kr-on' : ''}`}
            style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 10, padding: '16px 6px' }}
          >
            {fbIcon(id, kind === id)}
            <span style={{ fontSize: 12, color: kind === id ? 'var(--kim-text)' : 'var(--kim-text-2)' }}>
              {id[0].toUpperCase() + id.slice(1)}
            </span>
          </button>
        ))}
      </div>

      <SectionLabel>your message</SectionLabel>
      <textarea
        value={message}
        onChange={(e) => setMessage(e.target.value)}
        placeholder="Tell us what's on your mind…"
        rows={6}
        className="kr-input"
        style={{
          fontFamily: 'inherit',
          minHeight: 140,
          resize: 'vertical',
          marginBottom: 12,
        }}
      />

      <SectionLabel>email (optional)</SectionLabel>
      <input
        type="email"
        className="kr-input"
        placeholder="you@example.com"
        value={contact}
        onChange={(e) => setContact(e.target.value)}
        style={{ marginBottom: 16 }}
      />

      <button
        type="submit"
        className="kr-btn kr-btn-primary"
        disabled={!message.trim() || sending}
        style={{ width: '100%', justifyContent: 'center', padding: 12 }}
      >
        {sending ? 'Sending…' : 'Send feedback'}
      </button>
    </form>
  );
}

function PaneAbout({ appVersion, onCheckUpdate }: { appVersion: string; onCheckUpdate: () => void }) {
  return (
    <>
      <div
        style={{
          display: 'flex',
          gap: 18,
          padding: 22,
          background: 'linear-gradient(135deg, var(--kim-surface), var(--kim-bg-2))',
          border: '1px solid var(--kim-border)',
          borderRadius: 14,
          marginBottom: 22,
        }}
      >
        <div
          style={{
            width: 64,
            height: 64,
            borderRadius: 14,
            background: 'linear-gradient(135deg, var(--kim-accent), #c98968)',
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: 'var(--kim-on-accent)',
            fontWeight: 700,
            fontSize: 32,
          }}
        >
          K
        </div>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 18, marginBottom: 4 }}>Kim Desktop</div>
          <div
            style={{
              fontSize: 12.5,
              color: 'var(--kim-text-3)',
              fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
              marginBottom: 8,
            }}
          >
            v{appVersion} · current
          </div>
          <div style={{ fontSize: 13, color: 'var(--kim-text-2)', lineHeight: 1.55, maxWidth: 460 }}>
            Kim is a local AI agent that runs entirely on your machine. No telemetry, no cloud accounts required.
          </div>
        </div>
        <button type="button" className="kr-btn" onClick={onCheckUpdate} style={{ alignSelf: 'flex-start' }}>
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
            <path
              d="M2 6a4 4 0 017-2.5M10 6a4 4 0 01-7 2.5M9 1v3h-3M3 11V8h3"
              stroke="currentColor"
              strokeWidth="1.3"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
          Check for updates
        </button>
      </div>

      <SectionLabel>about kim</SectionLabel>
      <div
        style={{
          background: 'var(--kim-surface)',
          border: '1px solid var(--kim-border)',
          borderRadius: 12,
          padding: '14px 16px',
          fontSize: 13,
          color: 'var(--kim-text-2)',
          lineHeight: 1.6,
        }}
      >
        Kim is built on the Model Context Protocol and integrates with browsers, files, and shells. Configure providers and tools in the
        sections to the left.
      </div>
    </>
  );
}

// ── Main shell ────────────────────────────────────────────────────────────────

const PANE_META: Record<PaneId, { title: string; subtitle: string }> = {
  appearance: { title: 'Appearance', subtitle: 'How Kim looks on your machine.' },
  ai: { title: 'AI', subtitle: 'Which model runs Kim and how it behaves while working.' },
  voice: { title: 'Voice', subtitle: 'Kim speaks task completions, stuck detection, and tool announcements.' },
  paths: { title: 'Paths', subtitle: 'Where Kim reads and writes on disk.' },
  data: { title: 'Data', subtitle: 'Your sessions live on your machine. Nothing leaves unless you sync.' },
  account: { title: 'Account', subtitle: 'How you show up across Kim and your linked services.' },
  mcp: { title: 'MCP', subtitle: 'Tool packs Kim can call — built-in and your own.' },
  feedback: { title: 'Feedback', subtitle: "Tell us what's on your mind. We read every one." },
  about: { title: 'About', subtitle: "What's running, and what's new." },
};

export function RevampSettings(props: Props) {
  const { settings, onChange, onClose, appVersion, onCheckUpdate, account, onAccountChange, initialPane } = props;
  const [active, setActive] = useState<PaneId>(initialPane ?? 'appearance');

  // Close on Escape
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const meta = PANE_META[active];

  return (
    <div
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 1000,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: 'var(--bg-overlay)',
        backdropFilter: 'blur(4px)',
        padding: 28,
      }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="kim-revamp-settings-title"
    >
      <div
        className="kr-glass"
        style={{
          width: 'min(1100px, 100%)',
          height: 'min(760px, calc(100vh - 56px))',
          border: '1px solid var(--kim-border)',
          borderRadius: 16,
          display: 'flex',
          overflow: 'hidden',
          boxShadow: '0 30px 80px rgba(0,0,0,0.5)',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Left nav */}
        <div
          style={{
            width: 240,
            flexShrink: 0,
            borderRight: '1px solid var(--kim-border)',
            padding: '22px 14px',
            display: 'flex',
            flexDirection: 'column',
            gap: 2,
            background: 'var(--kim-bg-2)',
          }}
        >
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 10,
              padding: '8px 10px 16px',
              borderBottom: '1px solid var(--kim-border)',
              marginBottom: 10,
            }}
            id="kim-revamp-settings-title"
          >
            <svg width="18" height="18" viewBox="0 0 16 16" fill="none" style={{ color: 'var(--kim-accent)' }}>
              <path d="M8 1v14M1 8h14M3 3l10 10M13 3L3 13" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round" />
            </svg>
            <span style={{ fontWeight: 600, fontSize: 15, letterSpacing: '-0.01em' }}>Settings</span>
          </div>
          {NAV.map((n) => (
            <div
              key={n.id}
              className="kr-row-hover"
              onClick={() => setActive(n.id)}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 11,
                padding: '9px 11px',
                borderRadius: 9,
                cursor: 'pointer',
                background: active === n.id ? 'var(--kim-surface)' : 'transparent',
                color: active === n.id ? 'var(--kim-text)' : 'var(--kim-text-2)',
                borderLeft: active === n.id ? '2px solid var(--kim-accent)' : '2px solid transparent',
                marginLeft: -2,
                fontSize: 13.5,
              }}
            >
              <NavIcon name={n.icon} />
              <span>{n.label}</span>
            </div>
          ))}
        </div>

        {/* Body */}
        <div style={{ flex: 1, overflow: 'auto', padding: '28px 36px' }}>
          <PaneHeader title={meta.title} subtitle={meta.subtitle} onClose={onClose} />
          {active === 'appearance' && <PaneAppearance settings={settings} onChange={onChange} />}
          {active === 'ai' && <PaneAI settings={settings} onChange={onChange} />}
          {active === 'voice' && <PaneVoice settings={settings} onChange={onChange} />}
          {active === 'paths' && <PanePaths settings={settings} onChange={onChange} />}
          {active === 'data' && <PaneData account={account} />}
          {active === 'account' && <PaneAccount account={account} onAccountChange={onAccountChange} />}
          {active === 'mcp' && <PaneMCP />}
          {active === 'feedback' && <PaneFeedback />}
          {active === 'about' && <PaneAbout appVersion={appVersion} onCheckUpdate={onCheckUpdate} />}
        </div>
      </div>
    </div>
  );
}

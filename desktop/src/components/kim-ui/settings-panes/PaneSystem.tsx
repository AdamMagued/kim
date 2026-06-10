import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { open as openDialog } from '@tauri-apps/plugin-dialog';
import type { Settings, Theme, VoiceEngine, VoiceSettings, AccentTheme, TypingAnimation, KimAccount } from '../../../types';
import { VOICES_BY_ENGINE } from '../../../types';
import { SectionLabel, Row, Toggle } from './primitives';

const ACCENTS: { value: AccentTheme; label: string; color: string }[] = [
  { value: 'indigo', label: 'Terracotta', color: '#e8b89a' },
  { value: 'ocean', label: 'Mist', color: '#a9c8e8' },
  { value: 'ember', label: 'Sienna', color: '#e4a37a' },
  { value: 'teal', label: 'Sage', color: '#a8c5a3' },
  { value: 'jade', label: 'Rose', color: '#d4a0a0' },
  { value: 'mono', label: 'Mono', color: '#e8e0d2' },
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
        value={settings.codex_sessions_dir}
        hint="Path where Codex stores its session files."
        onPick={() => pick('codex_sessions_dir')}
        onChange={(v) => update('codex_sessions_dir', v)}
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

function PaneData({
  account,
  onAccountChange,
}: {
  account: KimAccount;
  onAccountChange: (a: KimAccount) => Promise<void>;
}) {
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
      const result = await invoke<string>('import_data', { filePath });
      setStatusMsg(result);
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
      const gistId = await invoke<string>('backup_to_gist', {
        token: account.github_token,
        existingGistId: account.gist_id ?? null,
      });
      if (gistId && gistId !== account.gist_id) {
        await onAccountChange({ ...account, gist_id: gistId });
      }
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
      const restored = await invoke<KimAccount>('restore_from_gist', { token: account.github_token, gistId: account.gist_id });
      await onAccountChange({ ...restored, github_token: account.github_token });
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

export { PaneAppearance, PaneVoice, PanePaths, PaneData };

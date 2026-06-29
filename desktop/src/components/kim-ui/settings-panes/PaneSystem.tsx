import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { open as openDialog } from '@tauri-apps/plugin-dialog';
import type { Settings, Theme, AccentTheme, TypingAnimation, KimAccount } from '../../../types';
import { SectionLabel } from './primitives';

const ACCENTS: { value: AccentTheme; label: string; color: string }[] = [
  { value: 'indigo', label: 'Terracotta', color: '#e8b89a' },
  { value: 'ocean', label: 'Mist', color: '#a9c8e8' },
  { value: 'ember', label: 'Sienna', color: '#e4a37a' },
  { value: 'teal', label: 'Sage', color: '#a8c5a3' },
  { value: 'jade', label: 'Rose', color: '#d4a0a0' },
  { value: 'mono', label: 'Mono', color: '#e8e0d2' },
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
  const inputId = `path-row-${label.toLowerCase().replace(/\s+/g, '-')}`;
  return (
    <div style={{ marginBottom: 18 }}>
      <label htmlFor={inputId} className="kr-eyebrow" style={{ display: 'block', marginBottom: 12 }}>
        {label}
      </label>
      <div style={{ display: 'flex', gap: 8 }}>
        <input
          id={inputId}
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
  const hasGitHub = !!account.github_token;

  // Timer refs — finding #2: clear on unmount to prevent setState-after-unmount.
  const exportTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const gistTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const statusTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    return () => {
      if (exportTimerRef.current) clearTimeout(exportTimerRef.current);
      if (gistTimerRef.current) clearTimeout(gistTimerRef.current);
      if (statusTimerRef.current) clearTimeout(statusTimerRef.current);
    };
  }, []);

  async function handleExport(format: 'zip' | 'json' | 'markdown') {
    setExportState('working');
    setStatusMsg('');
    try {
      const path = await invoke<string>('export_data', { format });
      setExportState('done');
      setStatusMsg(`Saved to ${path}`);
      if (exportTimerRef.current) clearTimeout(exportTimerRef.current);
      exportTimerRef.current = setTimeout(() => {
        setExportState('idle');
        setStatusMsg('');
      }, 3000);
    } catch (err) {
      setExportState('error');
      setStatusMsg(String(err));
    }
  }

  // finding #5: File.path is non-standard and absent in the Tauri webview.
  // Use openDialog() to obtain a real filesystem path instead.
  async function handleImport() {
    try {
      const selected = await openDialog({
        multiple: false,
        title: 'Select import file',
        filters: [{ name: 'Backup', extensions: ['zip', 'json'] }],
      });
      if (!selected || typeof selected !== 'string') return;
      const result = await invoke<string>('import_data', { filePath: selected });
      setStatusMsg(result);
      if (statusTimerRef.current) clearTimeout(statusTimerRef.current);
      statusTimerRef.current = setTimeout(() => setStatusMsg(''), 3000);
    } catch (err) {
      setStatusMsg(`Import failed: ${String(err)}`);
    }
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
      if (gistTimerRef.current) clearTimeout(gistTimerRef.current);
      gistTimerRef.current = setTimeout(() => setGistState('idle'), 2000);
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
      if (gistTimerRef.current) clearTimeout(gistTimerRef.current);
      gistTimerRef.current = setTimeout(() => setGistState('idle'), 2000);
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
      <button type="button" className="kr-btn" onClick={() => void handleImport()} style={{ width: '100%', justifyContent: 'center' }}>
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
          <path d="M6 11V3M3 6l3-3 3 3M1.5 1h9" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        Import from file (.zip or .json)
      </button>

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

export { PaneAppearance, PanePaths, PaneData };

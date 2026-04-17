import { useEffect, useState, useRef, type ReactElement } from 'react';
import { invoke } from '@tauri-apps/api/core';
import type { Settings, Provider, Theme, VoiceEngine, VoiceSettings, AccentTheme, KimAccount } from '../types';
import { VOICES_BY_ENGINE } from '../types';

const PROVIDERS: { value: Provider; label: string }[] = [
  { value: 'browser', label: 'Browser (no API key)' },
  { value: 'claude', label: 'Claude (Anthropic)' },
  { value: 'openai', label: 'GPT-4o (OpenAI)' },
  { value: 'gemini', label: 'Gemini (Google)' },
  { value: 'deepseek', label: 'DeepSeek' },
];

const VOICE_ENGINES: { value: VoiceEngine; label: string }[] = [
  { value: 'kokoro', label: 'Kokoro (local, fast)' },
  { value: 'maya1', label: 'Maya-1 (local, expressive)' },
  { value: 'http', label: 'HTTP (OpenAI-compatible)' },
  { value: 'hume', label: 'Hume (cloud)' },
];

const ACCENTS: { value: AccentTheme; label: string; light: string; dark: string }[] = [
  { value: 'indigo', label: 'Indigo', light: '#6366f1', dark: '#818cf8' },
  { value: 'ocean',  label: 'Ocean',  light: '#2563eb', dark: '#60a5fa' },
  { value: 'ember',  label: 'Ember',  light: '#ea6c0a', dark: '#fb923c' },
  { value: 'teal',   label: 'Teal',   light: '#0891b2', dark: '#22d3ee' },
  { value: 'jade',   label: 'Jade',   light: '#059669', dark: '#34d399' },
  { value: 'mono',   label: 'Mono',   light: '#18181b', dark: '#e4e4e7' },
];

type NavSection = 'appearance' | 'ai' | 'voice' | 'paths' | 'data' | 'account' | 'about';

interface NavItem {
  id: NavSection;
  label: string;
  icon: ReactElement;
}

function SunIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
    </svg>
  );
}
function SystemIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="4" width="18" height="12" rx="2" />
      <path d="M8 20h8M12 16v4" />
    </svg>
  );
}
function MoonIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
    </svg>
  );
}
function PaintIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="13.5" cy="6.5" r=".5" fill="currentColor" /><circle cx="17.5" cy="10.5" r=".5" fill="currentColor" /><circle cx="8.5" cy="7.5" r=".5" fill="currentColor" /><circle cx="6.5" cy="12.5" r=".5" fill="currentColor" />
      <path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.554C21.965 6.012 17.461 2 12 2z" />
    </svg>
  );
}
function SparkleIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 2l2.4 6.4L21 11l-6.6 2.6L12 20l-2.4-6.4L3 11l6.6-2.6L12 2z" />
    </svg>
  );
}
function MicIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="9" y="2" width="6" height="11" rx="3" />
      <path d="M5 10a7 7 0 0 0 14 0M12 19v3M9 22h6" />
    </svg>
  );
}
function FolderIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />
    </svg>
  );
}
function DatabaseIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <ellipse cx="12" cy="5" rx="9" ry="3" />
      <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
    </svg>
  );
}
function UserIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="8" r="4" />
      <path d="M4 20c0-4 3.6-7 8-7s8 3 8 7" />
    </svg>
  );
}
function InfoIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10" />
      <path d="M12 16v-4M12 8h.01" />
    </svg>
  );
}
function GitHubIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor">
      <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0 0 24 12c0-6.63-5.37-12-12-12z" />
    </svg>
  );
}
function CloudUpIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="16 16 12 12 8 16" />
      <line x1="12" y1="12" x2="12" y2="21" />
      <path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3" />
    </svg>
  );
}
function CloudDownIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="8 17 12 21 16 17" />
      <line x1="12" y1="12" x2="12" y2="21" />
      <path d="M20.88 18.09A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.29" />
    </svg>
  );
}
function DownloadIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="7 10 12 15 17 10" />
      <line x1="12" y1="15" x2="12" y2="3" />
    </svg>
  );
}
function UploadIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="17 8 12 3 7 8" />
      <line x1="12" y1="3" x2="12" y2="15" />
    </svg>
  );
}
function ShieldIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
    </svg>
  );
}
function RefreshIcon() {
  return (
    <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 8a6 6 0 0 1 10.5-4L14 2v4h-4l1.5-1.5A4 4 0 1 0 12 10" />
    </svg>
  );
}

const THEMES: { value: Theme; label: string; icon: ReactElement }[] = [
  { value: 'light', label: 'Light', icon: <SunIcon /> },
  { value: 'system', label: 'System', icon: <SystemIcon /> },
  { value: 'dark', label: 'Dark', icon: <MoonIcon /> },
];

const NAV_ITEMS: NavItem[] = [
  { id: 'appearance', label: 'Appearance', icon: <PaintIcon /> },
  { id: 'ai',         label: 'AI',         icon: <SparkleIcon /> },
  { id: 'voice',      label: 'Voice',      icon: <MicIcon /> },
  { id: 'paths',      label: 'Paths',      icon: <FolderIcon /> },
  { id: 'data',       label: 'Data',       icon: <DatabaseIcon /> },
  { id: 'account',    label: 'Account',    icon: <UserIcon /> },
  { id: 'about',      label: 'About',      icon: <InfoIcon /> },
];

interface Props {
  settings: Settings;
  onChange: (settings: Settings) => void;
  onClose: () => void;
  appVersion: string;
  onCheckUpdate: () => void;
  account: KimAccount;
  onAccountChange: (account: KimAccount) => Promise<void>;
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="kim-field">
      <label className="kim-field__label">{label}</label>
      {children}
      {hint && <div className="kim-field__hint">{hint}</div>}
    </div>
  );
}

// ── Section components ─────────────────────────────────────────────────────────

function AppearanceSection({ settings, onChange }: { settings: Settings; onChange: (s: Settings) => void }) {
  function update<K extends keyof Settings>(key: K, value: Settings[K]) {
    onChange({ ...settings, [key]: value });
  }

  const isDark = document.documentElement.classList.contains('dark');

  return (
    <div className="kim-settings-content">
      <div className="kim-settings-content__title">Appearance</div>

      <Field label="Theme">
        <div className="kim-theme-chooser">
          {THEMES.map(t => (
            <button
              key={t.value}
              onClick={() => update('theme', t.value)}
              className={`kim-theme-chooser__opt${settings.theme === t.value ? ' kim-theme-chooser__opt--active' : ''}`}
            >
              <span className="kim-theme-chooser__opt-icon">{t.icon}</span>
              <span>{t.label}</span>
            </button>
          ))}
        </div>
      </Field>

      <Field label="Accent color">
        <div className="kim-accent-picker">
          {ACCENTS.map(a => (
            <button
              key={a.value}
              title={a.label}
              onClick={() => update('accent', a.value)}
              className={`kim-accent-swatch${settings.accent === a.value ? ' kim-accent-swatch--active' : ''}`}
              style={{ '--swatch-color': isDark ? a.dark : a.light } as React.CSSProperties}
            >
              {settings.accent === a.value && (
                <svg viewBox="0 0 10 10" width="10" height="10" fill="none" stroke="white" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M2 5.5l2 2 4-4" />
                </svg>
              )}
            </button>
          ))}
        </div>
        <div className="kim-accent-label">{ACCENTS.find(a => a.value === settings.accent)?.label}</div>
      </Field>
    </div>
  );
}

function AISection({ settings, onChange }: { settings: Settings; onChange: (s: Settings) => void }) {
  function update<K extends keyof Settings>(key: K, value: Settings[K]) {
    onChange({ ...settings, [key]: value });
  }
  return (
    <div className="kim-settings-content">
      <div className="kim-settings-content__title">AI</div>
      <Field label="Default provider" hint="Browser mode uses your logged-in AI chat tabs — no API keys needed.">
        <select
          value={settings.provider}
          onChange={e => update('provider', e.target.value as Provider)}
          className="kim-select"
        >
          {PROVIDERS.map(p => (
            <option key={p.value} value={p.value}>{p.label}</option>
          ))}
        </select>
      </Field>
    </div>
  );
}

function VoiceSection({ settings, onChange }: { settings: Settings; onChange: (s: Settings) => void }) {
  const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');
  const [voiceError, setVoiceError] = useState<string | null>(null);

  useEffect(() => {
    invoke<VoiceSettings>('read_voice_config', { projectRoot: settings.project_root || null })
      .then(cfg => onChange({ ...settings, voice: { ...settings.voice, ...cfg } }))
      .catch(err => setVoiceError(`Failed to read config.yaml: ${String(err)}`));
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
    <div className="kim-settings-content">
      <div className="kim-settings-content__title">
        Voice
        {saveState === 'saving' && <span className="kim-save-status kim-save-status--saving">Saving…</span>}
        {saveState === 'saved'  && <span className="kim-save-status kim-save-status--saved">Saved</span>}
        {saveState === 'error'  && <span className="kim-save-status kim-save-status--error">Save failed</span>}
      </div>

      {voiceError && <div className="kim-inline-error">{voiceError}</div>}

      <div className="kim-toggle-row">
        <div>
          <div className="kim-toggle-row__label">Enable voice</div>
          <div className="kim-toggle-row__hint">Kim speaks task completions, stuck detection, and tool-call announcements aloud.</div>
        </div>
        <button
          role="switch"
          aria-checked={settings.voice.enabled}
          onClick={() => updateVoice('enabled', !settings.voice.enabled)}
          className={`kim-switch${settings.voice.enabled ? ' kim-switch--on' : ''}`}
        />
      </div>

      <Field label="Voice engine">
        <select
          value={settings.voice.engine}
          onChange={e => updateVoice('engine', e.target.value as VoiceEngine)}
          disabled={!settings.voice.enabled}
          className="kim-select"
        >
          {VOICE_ENGINES.map(e => <option key={e.value} value={e.value}>{e.label}</option>)}
        </select>
      </Field>

      <Field label="Voice" hint={settings.voice.engine === 'maya1' ? "Maya-1 uses 'speaker_description' from config.yaml — set it there." : undefined}>
        <select
          value={settings.voice.voice_id}
          onChange={e => updateVoice('voice_id', e.target.value)}
          disabled={!settings.voice.enabled || voices.length === 0}
          className="kim-select"
        >
          {voices.map(v => <option key={v.value} value={v.value}>{v.label}</option>)}
        </select>
      </Field>
    </div>
  );
}

function PathsSection({ settings, onChange }: { settings: Settings; onChange: (s: Settings) => void }) {
  function update<K extends keyof Settings>(key: K, value: Settings[K]) {
    onChange({ ...settings, [key]: value });
  }
  return (
    <div className="kim-settings-content">
      <div className="kim-settings-content__title">Paths</div>
      <Field label="Kim sessions directory" hint="Leave empty to use the default (~/Desktop/kim/kim_sessions or ~/.kim/sessions)">
        <input type="text" value={settings.kim_sessions_dir} onChange={e => update('kim_sessions_dir', e.target.value)} placeholder="/path/to/kim_sessions" className="kim-input" />
      </Field>
      <Field label="Code sessions directory" hint="Path where Claude Code stores its JSONL session files">
        <input type="text" value={settings.claw_sessions_dir} onChange={e => update('claw_sessions_dir', e.target.value)} placeholder="/path/to/claude/sessions" className="kim-input" />
      </Field>
      <Field label="Project root" hint="Root of your Kim installation (where orchestrator/ lives). Leave empty for auto-detect.">
        <input type="text" value={settings.project_root} onChange={e => update('project_root', e.target.value)} placeholder="/path/to/kim" className="kim-input" />
      </Field>
    </div>
  );
}

function DataSection({ account }: { account: KimAccount }) {
  const [exportState, setExportState] = useState<'idle' | 'working' | 'done' | 'error'>('idle');
  const [gistState, setGistState] = useState<'idle' | 'working' | 'done' | 'error'>('idle');
  const [statusMsg, setStatusMsg] = useState('');
  const fileInputRef = useRef<HTMLInputElement>(null);

  async function handleExport(format: 'zip' | 'json' | 'markdown') {
    setExportState('working');
    setStatusMsg('');
    try {
      const path = await invoke<string>('export_data', { format });
      setExportState('done');
      setStatusMsg(`Saved to ${path}`);
      setTimeout(() => { setExportState('idle'); setStatusMsg(''); }, 3000);
    } catch (err) {
      setExportState('error');
      setStatusMsg(String(err));
    }
  }

  async function handleImport(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    // In Tauri the webview file object has a non-standard `path` property at runtime
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

  const hasGitHub = !!account.github_token;

  return (
    <div className="kim-settings-content">
      <div className="kim-settings-content__title">Data</div>

      {/* Privacy note */}
      <div className="kim-data-privacy-note">
        <ShieldIcon />
        <span>All data is stored locally on your machine. Nothing leaves your device unless you explicitly use one of the sync options below.</span>
      </div>

      {/* Two-method explainer */}
      <div className="kim-settings-section__header" style={{ marginTop: 24, marginBottom: 12 }}>
        <span className="kim-settings-section__title" style={{ fontSize: 13 }}>Backup methods</span>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 12, marginBottom: 24 }}>
        <div className="kim-data-method">
          <div className="kim-data-method__icon"><GitHubIcon /></div>
          <div className="kim-data-method__body">
            <div className="kim-data-method__title">GitHub Gist sync</div>
            <div className="kim-data-method__desc">
              Your session index is backed up to a private Gist in your GitHub account. Requires a GitHub personal access token with <code>gist</code> scope. Restores on any machine where you sign in.
            </div>
          </div>
        </div>
        <div className="kim-data-method">
          <div className="kim-data-method__icon"><DownloadIcon /></div>
          <div className="kim-data-method__body">
            <div className="kim-data-method__title">File export / import</div>
            <div className="kim-data-method__desc">
              Export everything as a ZIP, JSON, or Markdown file — no internet required. Great for offline backups, moving to a new machine, or just keeping your own archive.
            </div>
          </div>
        </div>
      </div>

      {/* Gist sync */}
      <div className="kim-settings-section__header" style={{ marginBottom: 12 }}>
        <span className="kim-settings-section__title" style={{ fontSize: 13 }}>GitHub Gist</span>
      </div>
      {!hasGitHub ? (
        <div className="kim-field__hint" style={{ marginBottom: 16 }}>
          Connect a GitHub account in the <strong>Account</strong> section to enable Gist sync.
        </div>
      ) : (
        <div style={{ display: 'flex', gap: 8, marginBottom: 24 }}>
          <button
            className="kim-btn kim-btn--secondary"
            onClick={handleGistBackup}
            disabled={gistState === 'working'}
            style={{ flex: 1 }}
          >
            <CloudUpIcon />
            <span>{gistState === 'working' ? 'Backing up…' : gistState === 'done' ? 'Backed up' : 'Back up to Gist'}</span>
          </button>
          <button
            className="kim-btn kim-btn--secondary"
            onClick={handleGistRestore}
            disabled={gistState === 'working' || !account.gist_id}
            style={{ flex: 1 }}
            title={!account.gist_id ? 'No Gist backup found — run a backup first' : ''}
          >
            <CloudDownIcon />
            <span>{gistState === 'working' ? 'Restoring…' : 'Restore from Gist'}</span>
          </button>
        </div>
      )}

      {/* File export */}
      <div className="kim-settings-section__header" style={{ marginBottom: 12 }}>
        <span className="kim-settings-section__title" style={{ fontSize: 13 }}>Export</span>
      </div>
      <div style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
        {(['zip', 'json', 'markdown'] as const).map(fmt => (
          <button
            key={fmt}
            className="kim-btn kim-btn--secondary"
            onClick={() => handleExport(fmt)}
            disabled={exportState === 'working'}
            style={{ flex: 1 }}
          >
            <DownloadIcon />
            <span>.{fmt === 'markdown' ? 'md' : fmt}</span>
          </button>
        ))}
      </div>
      <div className="kim-field__hint" style={{ marginBottom: 24 }}>
        ZIP includes all raw JSONL session files. JSON is a structured index. Markdown is human-readable.
      </div>

      {/* File import */}
      <div className="kim-settings-section__header" style={{ marginBottom: 12 }}>
        <span className="kim-settings-section__title" style={{ fontSize: 13 }}>Import</span>
      </div>
      <button
        className="kim-btn kim-btn--secondary"
        onClick={() => fileInputRef.current?.click()}
        style={{ width: '100%' }}
      >
        <UploadIcon />
        <span>Import from file (.zip or .json)</span>
      </button>
      <input
        ref={fileInputRef}
        type="file"
        accept=".zip,.json"
        style={{ display: 'none' }}
        onChange={handleImport}
      />

      {statusMsg && (
        <div className={`kim-inline-${exportState === 'error' || gistState === 'error' ? 'error' : 'success'}`} style={{ marginTop: 12 }}>
          {statusMsg}
        </div>
      )}
    </div>
  );
}

function AccountSection({ account, onAccountChange }: { account: KimAccount; onAccountChange: (a: KimAccount) => Promise<void> }) {
  const [editingName, setEditingName] = useState(false);
  const [nameVal, setNameVal] = useState(account.display_name);
  const [token, setToken] = useState('');
  const [verifying, setVerifying] = useState(false);
  const [tokenError, setTokenError] = useState('');
  const [githubUser, setGithubUser] = useState<{ login: string; name: string | null; avatar_url: string } | null>(null);
  const [saving, setSaving] = useState(false);

  async function saveName() {
    if (!nameVal.trim()) return;
    setSaving(true);
    await onAccountChange({ ...account, display_name: nameVal.trim() });
    setSaving(false);
    setEditingName(false);
  }

  async function verifyAndLink() {
    if (!token.trim()) return;
    setVerifying(true);
    setTokenError('');
    try {
      const user = await invoke<{ login: string; name: string | null; avatar_url: string }>(
        'verify_github_pat', { token: token.trim() }
      );
      setGithubUser(user);
    } catch (err) {
      setTokenError(String(err));
    } finally {
      setVerifying(false);
    }
  }

  async function linkGitHub() {
    if (!githubUser) return;
    setSaving(true);
    await onAccountChange({
      ...account,
      github_username: githubUser.login,
      github_token: token.trim(),
      github_avatar_url: githubUser.avatar_url,
    });
    setSaving(false);
    setToken('');
    setGithubUser(null);
  }

  async function unlinkGitHub() {
    setSaving(true);
    await onAccountChange({
      ...account,
      github_username: undefined,
      github_token: undefined,
      github_avatar_url: undefined,
      gist_id: undefined,
    });
    setSaving(false);
  }

  return (
    <div className="kim-settings-content">
      <div className="kim-settings-content__title">Account</div>

      {/* Display name */}
      <Field label="Display name">
        {editingName ? (
          <div style={{ display: 'flex', gap: 8 }}>
            <input
              type="text"
              className="kim-input"
              value={nameVal}
              onChange={e => setNameVal(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') saveName(); if (e.key === 'Escape') setEditingName(false); }}
              autoFocus
              style={{ flex: 1 }}
            />
            <button className="kim-btn kim-btn--primary" onClick={saveName} disabled={saving || !nameVal.trim()}>
              {saving ? 'Saving…' : 'Save'}
            </button>
            <button className="kim-btn kim-btn--secondary" onClick={() => { setEditingName(false); setNameVal(account.display_name); }}>
              Cancel
            </button>
          </div>
        ) : (
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <span style={{ flex: 1, fontSize: 14, color: 'var(--text)' }}>{account.display_name}</span>
            <button className="kim-btn kim-btn--secondary" onClick={() => setEditingName(true)} style={{ fontSize: 12 }}>
              Edit
            </button>
          </div>
        )}
      </Field>

      {/* GitHub connection */}
      <div className="kim-settings-section__header" style={{ marginTop: 20, marginBottom: 12 }}>
        <span className="kim-settings-section__title" style={{ fontSize: 13 }}>GitHub</span>
      </div>

      {account.github_username ? (
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 12px', background: 'var(--surface-raised)', borderRadius: 8, border: '1px solid var(--border)', marginBottom: 12 }}>
            {account.github_avatar_url ? (
              <img src={account.github_avatar_url} alt="" style={{ width: 28, height: 28, borderRadius: '50%' }} />
            ) : (
              <div style={{ width: 28, height: 28, borderRadius: '50%', background: 'var(--accent)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'white', fontSize: 12, fontWeight: 600 }}>
                {account.github_username[0].toUpperCase()}
              </div>
            )}
            <div>
              <div style={{ fontSize: 13, fontWeight: 500, color: 'var(--text)' }}>@{account.github_username}</div>
              <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>Connected</div>
            </div>
            <button className="kim-btn kim-btn--secondary" onClick={unlinkGitHub} disabled={saving} style={{ marginLeft: 'auto', fontSize: 12 }}>
              Disconnect
            </button>
          </div>
        </div>
      ) : (
        <div>
          <div className="kim-field__hint" style={{ marginBottom: 12 }}>
            Connect GitHub to enable Gist backup sync. Create a token at <strong style={{ color: 'var(--text)' }}>github.com/settings/tokens</strong> with <code>gist</code> and <code>read:user</code> scopes.
          </div>
          <div style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
            <input
              type="password"
              className="kim-input"
              placeholder="ghp_..."
              value={token}
              onChange={e => { setToken(e.target.value); setGithubUser(null); setTokenError(''); }}
              style={{ flex: 1 }}
            />
            <button className="kim-btn kim-btn--secondary" onClick={verifyAndLink} disabled={!token.trim() || verifying || !!githubUser}>
              {verifying ? 'Checking…' : githubUser ? 'Verified' : 'Verify'}
            </button>
          </div>
          {tokenError && <div className="kim-inline-error" style={{ marginBottom: 8 }}>{tokenError}</div>}
          {githubUser && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
              <img src={githubUser.avatar_url} alt="" style={{ width: 20, height: 20, borderRadius: '50%' }} />
              <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>Connected as <strong style={{ color: 'var(--text)' }}>{githubUser.name ?? githubUser.login}</strong></span>
              <button className="kim-btn kim-btn--primary" onClick={linkGitHub} disabled={saving} style={{ marginLeft: 'auto' }}>
                {saving ? 'Linking…' : 'Link account'}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function AboutSection({ appVersion, onCheckUpdate }: { appVersion: string; onCheckUpdate: () => void }) {
  return (
    <div className="kim-settings-content">
      <div className="kim-settings-content__title">About</div>
      <div className="kim-about">
        <div>
          <div className="kim-about__title">Kim Desktop</div>
          <div className="kim-about__version">v{appVersion}</div>
        </div>
        <button onClick={onCheckUpdate} className="kim-btn kim-btn--secondary">
          <RefreshIcon />
          <span>Check for updates</span>
        </button>
      </div>
      <div className="kim-field__hint" style={{ marginTop: 16, lineHeight: 1.6 }}>
        Kim is a local AI agent that runs entirely on your machine. No telemetry, no cloud accounts required.
        Your sessions are stored in <code>~/.config/kim/</code>.
      </div>
    </div>
  );
}

// ── Main panel ─────────────────────────────────────────────────────────────────

export function SettingsPanel({ settings, onChange, onClose, appVersion, onCheckUpdate, account, onAccountChange }: Props) {
  const [activeSection, setActiveSection] = useState<NavSection>('appearance');

  return (
    <div
      className="kim-modal-backdrop"
      onClick={e => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="kim-modal kim-modal--settings-panel" role="dialog" aria-labelledby="settings-title">
        {/* Left nav */}
        <nav className="kim-settings-nav">
          <div className="kim-settings-nav__header" id="settings-title">Settings</div>
          <ul className="kim-settings-nav__list">
            {NAV_ITEMS.map(item => (
              <li key={item.id}>
                <button
                  className={`kim-settings-nav__item${activeSection === item.id ? ' kim-settings-nav__item--active' : ''}`}
                  onClick={() => setActiveSection(item.id)}
                >
                  <span className="kim-settings-nav__item-icon">{item.icon}</span>
                  <span>{item.label}</span>
                </button>
              </li>
            ))}
          </ul>
        </nav>

        {/* Right content */}
        <div className="kim-settings-body">
          <button onClick={onClose} className="kim-modal__close" aria-label="Close settings">
            <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M4 4l8 8M12 4l-8 8" />
            </svg>
          </button>

          {activeSection === 'appearance' && <AppearanceSection settings={settings} onChange={onChange} />}
          {activeSection === 'ai'         && <AISection settings={settings} onChange={onChange} />}
          {activeSection === 'voice'      && <VoiceSection settings={settings} onChange={onChange} />}
          {activeSection === 'paths'      && <PathsSection settings={settings} onChange={onChange} />}
          {activeSection === 'data'       && <DataSection account={account} />}
          {activeSection === 'account'    && <AccountSection account={account} onAccountChange={onAccountChange} />}
          {activeSection === 'about'      && <AboutSection appVersion={appVersion} onCheckUpdate={onCheckUpdate} />}
        </div>
      </div>
    </div>
  );
}

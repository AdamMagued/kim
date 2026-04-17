import { useEffect, useState, type ReactElement } from 'react';
import { invoke } from '@tauri-apps/api/core';
import type { Settings, Provider, Theme, VoiceEngine, VoiceSettings } from '../types';
import { VOICES_BY_ENGINE } from '../types';

const PROVIDERS: { value: Provider; label: string }[] = [
  { value: 'browser', label: 'Browser (no API key)' },
  { value: 'claude', label: 'Claude (Anthropic)' },
  { value: 'openai', label: 'GPT-4o (OpenAI)' },
  { value: 'gemini', label: 'Gemini (Google)' },
  { value: 'deepseek', label: 'DeepSeek' },
];

function SunIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
    </svg>
  );
}
function SystemIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="4" width="18" height="12" rx="2" />
      <path d="M8 20h8M12 16v4" />
    </svg>
  );
}
function MoonIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
    </svg>
  );
}

const THEMES: { value: Theme; label: string; icon: ReactElement }[] = [
  { value: 'light', label: 'Light', icon: <SunIcon /> },
  { value: 'system', label: 'System', icon: <SystemIcon /> },
  { value: 'dark', label: 'Dark', icon: <MoonIcon /> },
];

const VOICE_ENGINES: { value: VoiceEngine; label: string }[] = [
  { value: 'kokoro', label: 'Kokoro (local, fast)' },
  { value: 'maya1', label: 'Maya-1 (local, expressive)' },
  { value: 'http', label: 'HTTP (OpenAI-compatible)' },
  { value: 'hume', label: 'Hume (cloud)' },
];

interface Props {
  settings: Settings;
  onChange: (settings: Settings) => void;
  onClose: () => void;
  appVersion: string;
  onCheckUpdate: () => void;
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

export function SettingsPanel({ settings, onChange, onClose, appVersion, onCheckUpdate }: Props) {
  function update<K extends keyof Settings>(key: K, value: Settings[K]) {
    onChange({ ...settings, [key]: value });
  }

  const [voiceSaveState, setVoiceSaveState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');
  const [voiceError, setVoiceError] = useState<string | null>(null);

  useEffect(() => {
    invoke<VoiceSettings>('read_voice_config', {
      projectRoot: settings.project_root || null,
    })
      .then(cfg => {
        onChange({ ...settings, voice: { ...settings.voice, ...cfg } });
      })
      .catch(err => setVoiceError(`Failed to read config.yaml: ${String(err)}`));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function updateVoice<K extends keyof VoiceSettings>(key: K, value: VoiceSettings[K]) {
    const next: VoiceSettings = { ...settings.voice, [key]: value };

    if (key === 'engine') {
      const voices = VOICES_BY_ENGINE[value as VoiceEngine];
      if (voices.length > 0) next.voice_id = voices[0].value;
    }

    onChange({ ...settings, voice: next });
    setVoiceSaveState('saving');
    setVoiceError(null);
    try {
      await invoke('write_voice_config', {
        config: next,
        projectRoot: settings.project_root || null,
      });
      setVoiceSaveState('saved');
      setTimeout(() => setVoiceSaveState('idle'), 1500);
    } catch (err) {
      setVoiceSaveState('error');
      setVoiceError(String(err));
    }
  }

  const voices = VOICES_BY_ENGINE[settings.voice.engine] ?? [];

  return (
    <div
      className="kim-modal-backdrop"
      onClick={e => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="kim-modal kim-modal--settings"
        role="dialog"
        aria-labelledby="settings-title"
      >
        {/* Header */}
        <div className="kim-modal__header">
          <div id="settings-title" className="kim-modal__title">
            Settings
          </div>
          <button
            onClick={onClose}
            className="kim-modal__close"
            aria-label="Close settings"
          >
            <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M4 4l8 8M12 4l-4 4-4 4" />
            </svg>
          </button>
        </div>

        {/* Body */}
        <div className="kim-modal__body">
          {/* Appearance */}
          <div className="kim-settings-section">
            <div className="kim-settings-section__header">
              <h3 className="kim-settings-section__title">Appearance</h3>
            </div>

            <Field label="Theme">
              <div className="kim-theme-chooser">
                {THEMES.map(t => (
                  <button
                    key={t.value}
                    onClick={() => update('theme', t.value)}
                    className={`kim-theme-chooser__opt${
                      settings.theme === t.value ? ' kim-theme-chooser__opt--active' : ''
                    }`}
                  >
                    <span className="kim-theme-chooser__opt-icon">{t.icon}</span>
                    <span>{t.label}</span>
                  </button>
                ))}
              </div>
            </Field>
          </div>

          {/* AI */}
          <div className="kim-settings-section">
            <div className="kim-settings-section__header">
              <h3 className="kim-settings-section__title">AI provider</h3>
            </div>

            <Field label="Default provider" hint="Browser mode uses your logged-in AI chat tabs — no API keys.">
              <select
                value={settings.provider}
                onChange={e => update('provider', e.target.value as Provider)}
                className="kim-select"
              >
                {PROVIDERS.map(p => (
                  <option key={p.value} value={p.value}>
                    {p.label}
                  </option>
                ))}
              </select>
            </Field>
          </div>

          {/* Voice */}
          <div className="kim-settings-section">
            <div className="kim-settings-section__header">
              <h3 className="kim-settings-section__title">Voice</h3>
              {voiceSaveState === 'saving' && (
                <span className="kim-save-status kim-save-status--saving">Saving…</span>
              )}
              {voiceSaveState === 'saved' && (
                <span className="kim-save-status kim-save-status--saved">Saved ✓</span>
              )}
              {voiceSaveState === 'error' && (
                <span className="kim-save-status kim-save-status--error">Save failed</span>
              )}
            </div>

            {voiceError && <div className="kim-inline-error">{voiceError}</div>}

            <div className="kim-toggle-row">
              <div>
                <div className="kim-toggle-row__label">Enable voice</div>
                <div className="kim-toggle-row__hint">
                  Kim speaks task completion, stuck detection, and tool-call announcements aloud.
                </div>
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
                {VOICE_ENGINES.map(e => (
                  <option key={e.value} value={e.value}>
                    {e.label}
                  </option>
                ))}
              </select>
            </Field>

            <Field
              label="Voice"
              hint={
                settings.voice.engine === 'maya1'
                  ? "Maya-1 uses the 'speaker_description' from config.yaml — set it there."
                  : undefined
              }
            >
              <select
                value={settings.voice.voice_id}
                onChange={e => updateVoice('voice_id', e.target.value)}
                disabled={!settings.voice.enabled || voices.length === 0}
                className="kim-select"
              >
                {voices.map(v => (
                  <option key={v.value} value={v.value}>
                    {v.label}
                  </option>
                ))}
              </select>
            </Field>
          </div>

          {/* Paths */}
          <div className="kim-settings-section">
            <div className="kim-settings-section__header">
              <h3 className="kim-settings-section__title">Paths</h3>
            </div>

            <Field
              label="Kim sessions directory"
              hint="Leave empty to use the default (~/Desktop/kim/kim_sessions or ~/.kim/sessions)"
            >
              <input
                type="text"
                value={settings.kim_sessions_dir}
                onChange={e => update('kim_sessions_dir', e.target.value)}
                placeholder="/path/to/kim_sessions"
                className="kim-input"
              />
            </Field>

            <Field
              label="Claw Code sessions directory"
              hint="Path where Claw stores its JSONL session files"
            >
              <input
                type="text"
                value={settings.claw_sessions_dir}
                onChange={e => update('claw_sessions_dir', e.target.value)}
                placeholder="/path/to/claw/sessions"
                className="kim-input"
              />
            </Field>

            <Field
              label="Project root"
              hint="Root of your Kim installation (where orchestrator/ lives). Leave empty for auto-detect."
            >
              <input
                type="text"
                value={settings.project_root}
                onChange={e => update('project_root', e.target.value)}
                placeholder="/path/to/kim"
                className="kim-input"
              />
            </Field>
          </div>

          {/* About */}
          <div className="kim-about">
            <div>
              <div className="kim-about__title">Kim Desktop</div>
              <div className="kim-about__version">v{appVersion}</div>
            </div>
            <button onClick={onCheckUpdate} className="kim-btn kim-btn--secondary">
              <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M2 8a6 6 0 0 1 10.5-4L14 2v4h-4l1.5-1.5A4 4 0 1 0 12 10" />
              </svg>
              <span>Check for updates</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

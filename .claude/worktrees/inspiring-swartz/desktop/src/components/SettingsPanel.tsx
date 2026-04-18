import { useEffect, useState } from 'react';
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

const THEMES: { value: Theme; label: string; icon: string }[] = [
  { value: 'light', label: 'Light', icon: '☀️' },
  { value: 'system', label: 'System', icon: '💻' },
  { value: 'dark', label: 'Dark', icon: '🌙' },
];

const VOICE_ENGINES: { value: VoiceEngine; label: string }[] = [
  { value: 'kokoro', label: 'Kokoro (local, fast)' },
  { value: 'maya1',  label: 'Maya-1 (local, expressive)' },
  { value: 'http',   label: 'HTTP (OpenAI-compatible)' },
  { value: 'hume',   label: 'Hume (cloud)' },
];

interface Props {
  settings: Settings;
  onChange: (settings: Settings) => void;
  onClose: () => void;
  appVersion: string;
  onCheckUpdate: () => void;
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: '20px' }}>
      <label
        style={{
          display: 'block',
          fontSize: '13px',
          fontWeight: 600,
          color: 'var(--text)',
          marginBottom: '6px',
        }}
      >
        {label}
      </label>
      {children}
      {hint && (
        <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '4px' }}>
          {hint}
        </div>
      )}
    </div>
  );
}

const inputStyle: React.CSSProperties = {
  width: '100%',
  padding: '9px 12px',
  borderRadius: '8px',
  border: '1px solid var(--border)',
  background: 'var(--bg-input)',
  color: 'var(--text)',
  fontSize: '13px',
  outline: 'none',
  fontFamily: 'inherit',
};

export function SettingsPanel({ settings, onChange, onClose, appVersion, onCheckUpdate }: Props) {
  function update<K extends keyof Settings>(key: K, value: Settings[K]) {
    onChange({ ...settings, [key]: value });
  }

  // ── Voice: load from config.yaml on mount so the UI reflects disk state ──
  const [voiceSaveState, setVoiceSaveState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');
  const [voiceError, setVoiceError] = useState<string | null>(null);

  useEffect(() => {
    invoke<VoiceSettings>('read_voice_config', {
      projectRoot: settings.project_root || null,
    })
      .then(cfg => {
        // Only sync from disk if this is the first read (avoids stomping on
        // unsaved UI edits when the user reopens the panel).
        onChange({ ...settings, voice: { ...settings.voice, ...cfg } });
      })
      .catch(err => setVoiceError(`Failed to read config.yaml: ${String(err)}`));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Save voice settings immediately on change.
  async function updateVoice<K extends keyof VoiceSettings>(key: K, value: VoiceSettings[K]) {
    const next: VoiceSettings = { ...settings.voice, [key]: value };

    // When the engine changes, reset voice_id to the first voice for that engine.
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
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.5)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 900,
      }}
      onClick={e => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div
        style={{
          background: 'var(--bg)',
          border: '1px solid var(--border)',
          borderRadius: '16px',
          width: '520px',
          maxWidth: '92vw',
          maxHeight: '85vh',
          display: 'flex',
          flexDirection: 'column',
          boxShadow: '0 20px 60px rgba(0,0,0,0.3)',
          overflow: 'hidden',
        }}
      >
        {/* Header */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '20px 24px 16px',
            borderBottom: '1px solid var(--border)',
            flexShrink: 0,
          }}
        >
          <div style={{ fontWeight: 700, fontSize: '16px', color: 'var(--text)' }}>
            Settings
          </div>
          <button
            onClick={onClose}
            style={{
              width: '28px',
              height: '28px',
              borderRadius: '8px',
              border: 'none',
              background: 'var(--bg-card)',
              cursor: 'pointer',
              color: 'var(--text-muted)',
              fontSize: '16px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            ✕
          </button>
        </div>

        {/* Body */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '20px 24px' }}>

          {/* Theme */}
          <Field label="Theme">
            <div style={{ display: 'flex', gap: '6px' }}>
              {THEMES.map(t => (
                <button
                  key={t.value}
                  onClick={() => update('theme', t.value)}
                  style={{
                    flex: 1,
                    padding: '8px',
                    borderRadius: '8px',
                    border: `2px solid ${settings.theme === t.value ? 'var(--accent)' : 'var(--border)'}`,
                    background: settings.theme === t.value ? 'var(--accent-muted)' : 'transparent',
                    cursor: 'pointer',
                    color: settings.theme === t.value ? 'var(--accent)' : 'var(--text-muted)',
                    fontSize: '13px',
                    fontWeight: settings.theme === t.value ? 600 : 400,
                    display: 'flex',
                    flexDirection: 'column',
                    alignItems: 'center',
                    gap: '4px',
                  }}
                >
                  <span style={{ fontSize: '18px' }}>{t.icon}</span>
                  <span>{t.label}</span>
                </button>
              ))}
            </div>
          </Field>

          {/* Provider */}
          <Field label="Default AI provider">
            <select
              value={settings.provider}
              onChange={e => update('provider', e.target.value as Provider)}
              style={{ ...inputStyle, cursor: 'pointer' }}
            >
              {PROVIDERS.map(p => (
                <option key={p.value} value={p.value}>
                  {p.label}
                </option>
              ))}
            </select>
          </Field>

          {/* Voice */}
          <div
            style={{
              borderTop: '1px solid var(--border)',
              paddingTop: '16px',
              marginTop: '4px',
              marginBottom: '4px',
            }}
          >
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                marginBottom: '12px',
              }}
            >
              <div style={{ fontSize: '13px', fontWeight: 700, color: 'var(--text)' }}>
                🔊 Voice
              </div>
              {voiceSaveState === 'saving' && (
                <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>Saving…</span>
              )}
              {voiceSaveState === 'saved' && (
                <span style={{ fontSize: '11px', color: 'var(--accent)' }}>Saved ✓</span>
              )}
              {voiceSaveState === 'error' && (
                <span style={{ fontSize: '11px', color: '#dc2626' }}>Save failed</span>
              )}
            </div>

            {voiceError && (
              <div
                style={{
                  fontSize: '11px',
                  color: '#991b1b',
                  background: '#fee2e2',
                  padding: '6px 10px',
                  borderRadius: '6px',
                  marginBottom: '10px',
                }}
              >
                {voiceError}
              </div>
            )}

            {/* Voice toggle */}
            <label
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '10px',
                padding: '10px 12px',
                borderRadius: '8px',
                border: '1px solid var(--border)',
                cursor: 'pointer',
                marginBottom: '14px',
                background: 'var(--bg-input)',
              }}
            >
              <input
                type="checkbox"
                checked={settings.voice.enabled}
                onChange={e => updateVoice('enabled', e.target.checked)}
                style={{ width: '16px', height: '16px', cursor: 'pointer' }}
              />
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: '13px', fontWeight: 500, color: 'var(--text)' }}>
                  Enable voice
                </div>
                <div style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                  Kim speaks task completion, stuck detection, and tool-call announcements aloud.
                </div>
              </div>
            </label>

            <Field label="Voice engine">
              <select
                value={settings.voice.engine}
                onChange={e => updateVoice('engine', e.target.value as VoiceEngine)}
                disabled={!settings.voice.enabled}
                style={{
                  ...inputStyle,
                  cursor: settings.voice.enabled ? 'pointer' : 'not-allowed',
                  opacity: settings.voice.enabled ? 1 : 0.5,
                }}
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
                style={{
                  ...inputStyle,
                  cursor: settings.voice.enabled && voices.length > 0 ? 'pointer' : 'not-allowed',
                  opacity: settings.voice.enabled ? 1 : 0.5,
                }}
              >
                {voices.map(v => (
                  <option key={v.value} value={v.value}>
                    {v.label}
                  </option>
                ))}
              </select>
            </Field>
          </div>

          {/* Session paths */}
          <Field
            label="Kim sessions directory"
            hint="Leave empty to use the default (~/Desktop/kim/kim_sessions or ~/.kim/sessions)"
          >
            <input
              type="text"
              value={settings.kim_sessions_dir}
              onChange={e => update('kim_sessions_dir', e.target.value)}
              placeholder="/path/to/kim_sessions"
              style={inputStyle}
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
              style={inputStyle}
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
              style={inputStyle}
            />
          </Field>

          {/* About */}
          <div
            style={{
              borderTop: '1px solid var(--border)',
              paddingTop: '20px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
            }}
          >
            <div>
              <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text)' }}>
                Kim Desktop
              </div>
              <div style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
                v{appVersion}
              </div>
            </div>
            <button
              onClick={onCheckUpdate}
              style={{
                padding: '7px 14px',
                borderRadius: '8px',
                border: '1px solid var(--border)',
                background: 'transparent',
                color: 'var(--text)',
                cursor: 'pointer',
                fontSize: '13px',
              }}
            >
              Check for updates
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

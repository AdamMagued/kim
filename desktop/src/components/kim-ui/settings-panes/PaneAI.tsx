import { useEffect, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { openUrl as openExternal } from '@tauri-apps/plugin-opener';
import type { Settings, Provider } from '../../../types';
import { toast } from '../../Toast';
import { SectionLabel, Row, Toggle } from './primitives';

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
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
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
              <button
                type="button"
                className="kr-btn"
                style={{ width: '100%', justifyContent: 'center' }}
                onClick={() => void openExternal('https://www.ollama.com/usage')}
                disabled={ollamaBusy !== 'idle'}
              >
                Open usage dashboard
              </button>
            </div>
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
            <input
              className="kr-input"
              style={{ marginTop: 8 }}
              value={selectedModel}
              onChange={(e) => {
                if (settings.ollama.mode === 'cloud') {
                  updateOllama({ cloud_model: e.target.value });
                } else {
                  updateOllama({ local_model: e.target.value });
                }
              }}
              placeholder={settings.ollama.mode === 'cloud' ? 'Type a cloud model name (e.g. deepseek-v4-pro)…' : 'Type a model name to pull (e.g. qwen2.5vl)…'}
            />
            <div style={{ fontSize: 11.5, color: 'var(--kim-text-3)', marginTop: 6, lineHeight: 1.35 }}>
              {settings.ollama.mode === 'cloud'
                ? 'Kim can’t auto-discover every cloud model. Type any model name from the Ollama cloud catalog.'
                : 'Local mode only lists installed models. Type a model name then click “Pull model”.'}
            </div>
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

export { PaneAI };

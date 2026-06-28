import { useState, useRef, useEffect, useMemo, useCallback } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { listen, type UnlistenFn } from '@tauri-apps/api/event';
import { openUrl } from '@tauri-apps/plugin-opener';
import { useAuthStatus } from '../hooks/useAuthStatus';
import { toast } from './Toast';
import { BROWSER_PROVIDERS, API_PROVIDERS, OLLAMA_CLOUD_DEFAULT_MODEL } from '../types';

// ── Types ────────────────────────────────────────────────────────────────────
// BROWSER_PROVIDERS and API_PROVIDERS are imported from ../types (single source of truth).

interface OllamaModelInfo {
  name: string;
  size: number;
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
  local_models: OllamaModelInfo[];
  cloud_models: OllamaModelInfo[];
  cloud_connected: boolean;
  cloud_message?: string | null;
}

interface ProviderPickerProps {
  /** Resolved provider id, e.g. `browser:chatgpt`, `claude`, `ollama`. */
  resolvedProvider: string;
  /** Current Ollama settings — base_url, mode, currently-selected model. */
  ollama: {
    base_url: string;
    mode: 'local' | 'cloud';
    local_model: string;
    cloud_model: string;
  };
  /** Called when the user picks a different provider. */
  onChangeProvider: (next: string) => void | Promise<void>;
  /** Called when the user switches Ollama between local/cloud mode. */
  onChangeOllamaMode: (mode: 'local' | 'cloud') => void | Promise<void>;
  /** Called when the user picks a different Ollama model. */
  onChangeOllamaModel: (mode: 'local' | 'cloud', model: string) => void | Promise<void>;
  /** Opens the full Ollama settings pane for local daemon details / pulls. */
  onOpenOllamaSettings?: () => void;
  /** Disabled while a task is running. */
  disabled?: boolean;
}


/**
 * Unified provider/model picker. Replaces the older AuthIndicator chip with a
 * single control that lets the user:
 *   - Switch between Ollama / Browser / API providers.
 *   - Sign in to a browser provider inline.
 *   - Pick Ollama mode/model inline under the composer.
 *
 * The pill shows the CURRENT selection plus a status badge — e.g.
 * "Browser: ChatGPT · signed in", "Ollama · cloud", or "Claude API".
 */
export function ProviderPicker({
  resolvedProvider,
  ollama,
  onChangeProvider,
  onChangeOllamaMode,
  onChangeOllamaModel,
  onOpenOllamaSettings,
  disabled = false,
}: ProviderPickerProps) {
  const [open, setOpen] = useState(false);
  const [ollamaStatus, setOllamaStatus] = useState<OllamaStatus | null>(null);
  const [ollamaLoading, setOllamaLoading] = useState(false);
  const [ollamaCustomModel, setOllamaCustomModel] = useState('');
  const [customModelMode, setCustomModelMode] = useState(false);
  const customInputRef = useRef<HTMLInputElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  // Timer ref for the post-signin Ollama refresh (finding #2: clear on unmount).
  const refreshTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current);
    };
  }, []);

  // Currently active provider category — derived from resolvedProvider.
  const isBrowser = resolvedProvider.startsWith('browser:');
  const isOllama  = resolvedProvider === 'ollama';
  const currentBrowserSite = isBrowser ? resolvedProvider.slice('browser:'.length) : '';
  const currentApiProvider = !isBrowser && !isOllama ? resolvedProvider : '';

  // Browser-provider auth status — only for the currently selected one
  // (probing all 5 at once would create unnecessary popups / requests).
  const authForCurrent = useAuthStatus(isBrowser ? currentBrowserSite : '');

  // Fetch Ollama status when the panel opens, when the picker mounts in an
  // Ollama-selected state, and on `kim-ollama-changed` events.
  const refreshOllama = useCallback(async () => {
    setOllamaLoading(true);
    try {
      const result = await invoke<OllamaStatus>('ollama_get_status', {
        baseUrl: ollama.base_url || null,
        selectedModel:
          (ollama.mode === 'cloud' ? ollama.cloud_model : ollama.local_model) || null,
        mode: ollama.mode,
        contextLimitOverride: null,
      });
      setOllamaStatus(result);
    } catch {
      // Treat as not installed if the command errors entirely (older builds).
      setOllamaStatus(null);
    } finally {
      setOllamaLoading(false);
    }
  }, [ollama.base_url, ollama.mode, ollama.local_model, ollama.cloud_model]);

  useEffect(() => {
    if (open || isOllama) {
      void refreshOllama();
    }
  }, [open, isOllama, refreshOllama]);

  useEffect(() => {
    let unlisten: UnlistenFn | null = null;
    let cancelled = false;
    void (async () => {
      try {
        unlisten = await listen('kim-ollama-changed', () => {
          if (!cancelled) void refreshOllama();
        });
      } catch {
        // listen() can fail in non-Tauri test environments — harmless.
      }
    })();
    return () => {
      cancelled = true;
      if (unlisten) unlisten();
    };
  }, [refreshOllama]);

  // Close on outside click.
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);

  // Pill content ---------------------------------------------------------
  // For browser providers, append the signed-in email so the user can see at a
  // glance which Google/Anthropic/etc. account Kim is talking through. If we
  // probed and got a "signed_in: true" but no email (some providers don't
  // expose one), fall back to "signed in".
  const currentBrowserLabel = BROWSER_PROVIDERS.find(p => p.id === currentBrowserSite)?.label
    ?? currentBrowserSite;
  const currentEmail = authForCurrent.status?.email ?? '';
  const pillLabel = (() => {
    if (isBrowser) {
      if (authForCurrent.status?.signed_in) {
        // Email when we have it (ChatGPT / Claude / Gemini probes succeeded),
        // otherwise an explicit "signed in" so the user knows the green dot
        // means "ready" and not "unknown".
        return currentEmail
          ? `${currentBrowserLabel} · ${currentEmail}`
          : `${currentBrowserLabel} · signed in`;
      }
      return `Browser: ${currentBrowserLabel}`;
    }
    if (isOllama) {
      return `Ollama · ${ollama.mode}`;
    }
    const label = API_PROVIDERS.find(p => p.id === resolvedProvider)?.label
      ?? resolvedProvider;
    return label;
  })();

  // Status dot — green = ready, amber = needs attention, gray = unknown/disabled.
  const status: 'ok' | 'warn' | 'idle' | 'loading' = (() => {
    if (isBrowser) {
      if (authForCurrent.loading && !authForCurrent.probed) return 'loading';
      if (authForCurrent.status?.signed_in) return 'ok';
      return 'warn';
    }
    if (isOllama) {
      if (ollamaLoading && !ollamaStatus) return 'loading';
      if (!ollamaStatus?.running) return 'warn';
      const selectedModel =
        ollama.mode === 'cloud' ? ollama.cloud_model : ollama.local_model;
      if (!selectedModel) return 'warn';
      return 'ok';
    }
    // API providers — no auth probe; we'd need an API-key check, defer for now.
    return 'idle';
  })();

  const dotColor =
    status === 'ok'   ? '#22c55e' :
    status === 'warn' ? '#eab308' :
                        'var(--kim-text-3)';

  const inlineOllamaModels = useMemo(() => {
    const base = ollama.mode === 'cloud'
      ? (ollamaStatus?.cloud_models ?? [])
      : (ollamaStatus?.local_models ?? []);
    const selected = ollama.mode === 'cloud' ? ollama.cloud_model : ollama.local_model;
    if (!selected || base.some(m => m.name === selected)) return base;
    return [
      ...base,
      {
        name: selected,
        size: 0,
        family: null,
        parameter_size: null,
        quantization_level: null,
        cloud: ollama.mode === 'cloud',
        installed: false,
      },
    ];
  }, [ollama.mode, ollama.cloud_model, ollama.local_model, ollamaStatus]);
  const selectedOllamaModel = ollama.mode === 'cloud'
    ? (ollama.cloud_model || OLLAMA_CLOUD_DEFAULT_MODEL)
    : ollama.local_model;
  const selectedOllamaModelInfo = inlineOllamaModels.find(m => m.name === selectedOllamaModel);
  const selectedOllamaMeta = [
    selectedOllamaModelInfo?.parameter_size,
    selectedOllamaModelInfo?.quantization_level,
    selectedOllamaModelInfo?.family,
  ].filter(Boolean).join(' · ');

  useEffect(() => {
    setOllamaCustomModel(selectedOllamaModel);
    // Custom mode only valid in cloud; collapse it when switching to local
    if (ollama.mode === 'local') setCustomModelMode(false);
  }, [selectedOllamaModel, ollama.mode]);

  useEffect(() => {
    if (customModelMode && customInputRef.current) {
      customInputRef.current.focus();
    }
  }, [customModelMode]);

  // Action handlers ------------------------------------------------------
  const pick = (next: string) => {
    if (disabled) return;
    void onChangeProvider(next);
    setOpen(false);
  };

  const switchOllamaMode = (mode: 'local' | 'cloud') => {
    if (disabled) return;
    void onChangeOllamaMode(mode);
    if (resolvedProvider !== 'ollama') void onChangeProvider('ollama');
    setCustomModelMode(false);
  };

  const confirmCustomModel = () => {
    const next = ollamaCustomModel.trim();
    if (next) {
      void onChangeOllamaModel(ollama.mode, next);
      setCustomModelMode(false);
    }
  };

  const signInCurrent = async () => {
    if (!isBrowser || disabled) return;
    try {
      await authForCurrent.signIn();
      toast(`Opening ${currentBrowserLabel} sign-in…`, 'info', 2500);
    } catch (e) {
      toast(`Could not open sign-in: ${e}`, 'error', 5000);
    }
  };

  const signOutCurrent = async () => {
    if (!isBrowser || disabled) return;
    // Confirm so a stray click on the inline button doesn't nuke a session the
    // user wanted to keep — sign-out invalidates cookies and forces a full
    // re-auth on the next task.
    const who = currentEmail ? ` (${currentEmail})` : '';
    if (!window.confirm(`Sign out of ${currentBrowserLabel}${who}? Kim will need a fresh sign-in for the next task.`)) {
      return;
    }
    try {
      await authForCurrent.signOut();
      toast(`Signed out of ${currentBrowserLabel}.`, 'info', 3000);
    } catch (e) {
      toast(`Sign out failed: ${e}`, 'error', 4000);
    }
  };

  // Render ---------------------------------------------------------------
  const showInlineSignIn =
    isBrowser && authForCurrent.probed && !authForCurrent.status?.signed_in;
  const showInlineSignOut =
    isBrowser && !!authForCurrent.status?.signed_in;

  return (
    <div className="kim-provider-picker" ref={panelRef}>
      <button
        type="button"
        className={
          'kim-provider-picker__pill' +
          (status === 'ok'   ? ' kim-provider-picker__pill--ok'   : '') +
          (status === 'warn' ? ' kim-provider-picker__pill--warn' : '')
        }
        onClick={() => !disabled && setOpen(v => !v)}
        disabled={disabled}
        title="Change provider"
      >
        <span
          className="kim-provider-picker__dot"
          style={{ background: dotColor }}
          aria-hidden="true"
        />
        <span className="kim-provider-picker__label">{pillLabel}</span>
        <svg
          className="kim-provider-picker__chev"
          viewBox="0 0 10 10" width="9" height="9" fill="none"
          stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M2 4l3 3 3-3" />
        </svg>
      </button>

      {/* Inline sign-in button — only when current pick is a browser provider
          that we've probed and confirmed is signed out. Keeps the affordance
          right next to the pill so the user can act without opening the menu. */}
      {showInlineSignIn && !disabled && (
        <button
          type="button"
          className="kim-provider-picker__signin-btn"
          onClick={signInCurrent}
        >
          Sign in
        </button>
      )}
      {/* Mirror affordance for the signed-in case — lets the user swap accounts
          (sign out → sign back in with a different one) without digging through
          the dropdown menu. */}
      {showInlineSignOut && !disabled && (
        <button
          type="button"
          className="kim-provider-picker__signout-btn"
          onClick={signOutCurrent}
          title={currentEmail ? `Sign out of ${currentEmail}` : 'Sign out'}
        >
          Sign out
        </button>
      )}

      {open && (
        <div className="kim-provider-picker__panel" role="menu">
          {isOllama && (
            <div className="kim-provider-picker__ollama-section" aria-label="Ollama quick settings">
          <div className="kim-provider-picker__mode-switch" role="group" aria-label="Ollama mode">
            <button
              type="button"
              className={'kim-provider-picker__mode-option' + (ollama.mode === 'cloud' ? ' is-active' : '')}
              onClick={() => switchOllamaMode('cloud')}
              disabled={disabled}
            >
              Cloud
            </button>
            <button
              type="button"
              className={'kim-provider-picker__mode-option' + (ollama.mode === 'local' ? ' is-active' : '')}
              onClick={() => switchOllamaMode('local')}
              disabled={disabled}
            >
              Local
            </button>
          </div>

          <div className="kim-provider-picker__model-field">
            <span className="kim-provider-picker__model-field-label">Model</span>

            {customModelMode ? (
              <div className="kim-provider-picker__custom-row">
                <input
                  ref={customInputRef}
                  className="kim-provider-picker__model-input"
                  value={ollamaCustomModel}
                  placeholder={ollama.mode === 'cloud' ? 'e.g. deepseek-v3:685b-cloud' : 'e.g. llama3.2:3b'}
                  disabled={disabled}
                  onChange={e => setOllamaCustomModel(e.target.value)}
                  onKeyDown={e => {
                    if (e.key === 'Enter') confirmCustomModel();
                    if (e.key === 'Escape') setCustomModelMode(false);
                  }}
                />
                <button
                  type="button"
                  className="kim-provider-picker__custom-confirm"
                  onClick={confirmCustomModel}
                  title="Use this model"
                  aria-label="Confirm model name"
                >
                  <svg viewBox="0 0 12 12" width="11" height="11" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M2 6l3 3 5-5" />
                  </svg>
                </button>
                <button
                  type="button"
                  className="kim-provider-picker__custom-cancel"
                  onClick={() => { setCustomModelMode(false); setOllamaCustomModel(selectedOllamaModel); }}
                  title="Cancel"
                  aria-label="Cancel"
                >
                  <svg viewBox="0 0 12 12" width="11" height="11" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M2 2l8 8M10 2l-8 8" />
                  </svg>
                </button>
              </div>
            ) : (
              <>
                <span className="kim-provider-picker__select-wrap">
                  <select
                    value={selectedOllamaModel}
                    disabled={disabled || !ollamaStatus?.running || inlineOllamaModels.length === 0}
                    onChange={e => {
                      const model = e.target.value;
                      if (model) void onChangeOllamaModel(ollama.mode, model);
                    }}
                  >
                    {ollama.mode === 'local' && inlineOllamaModels.length === 0 && (
                      <option value="">No local models</option>
                    )}
                    {ollama.mode === 'cloud' && inlineOllamaModels.length === 0 && (
                      <option value={OLLAMA_CLOUD_DEFAULT_MODEL}>{OLLAMA_CLOUD_DEFAULT_MODEL}</option>
                    )}
                    {inlineOllamaModels.map(model => (
                      <option key={`${ollama.mode}-${model.name}`} value={model.name}>
                        {model.name}
                        {model.parameter_size ? ` · ${model.parameter_size}` : ''}
                        {model.quantization_level ? ` · ${model.quantization_level}` : ''}
                      </option>
                    ))}
                  </select>
                  <svg viewBox="0 0 10 10" width="9" height="9" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                    <path d="M2 4l3 3 3-3" />
                  </svg>
                </span>
                {ollama.mode === 'cloud' && (
                  <button
                    type="button"
                    className="kim-provider-picker__enter-name-btn"
                    onClick={() => { setOllamaCustomModel(selectedOllamaModel); setCustomModelMode(true); }}
                    disabled={disabled}
                    title="Type an exact model name"
                  >
                    Enter model name
                  </button>
                )}
              </>
            )}

            {!customModelMode && selectedOllamaMeta && (
              <span className="kim-provider-picker__model-meta">{selectedOllamaMeta}</span>
            )}
          </div>

          {ollama.mode === 'local' ? (
            <button
              type="button"
              className="kim-provider-picker__mini-settings"
              onClick={onOpenOllamaSettings}
              disabled={disabled || !onOpenOllamaSettings}
              title="Configure local Ollama"
              aria-label="Configure local Ollama"
            >
              <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="8" cy="8" r="2.2" />
                <path d="M8 1.8v1.5M8 12.7v1.5M3.6 3.6l1 1M11.4 11.4l1 1M1.8 8h1.5M12.7 8h1.5M3.6 12.4l1-1M11.4 4.6l1-1" />
              </svg>
              <span>Configure local</span>
            </button>
          ) : (
            ollamaStatus?.running && (
              <div className="kim-provider-picker__cloud-actions">
                {!ollamaStatus.cloud_connected && (
                  <button
                    type="button"
                    className="kim-provider-picker__signin-btn"
                    onClick={async () => {
                      try {
                        await invoke('ollama_signin');
                        toast('Opening Ollama sign-in...', 'info', 2500);
                        if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current);
                        refreshTimerRef.current = setTimeout(() => { void refreshOllama(); }, 800);
                      } catch (err) {
                        toast(`Could not open Ollama sign-in: ${err}`, 'error', 4000);
                      }
                    }}
                  >
                    Sign in
                  </button>
                )}
                <button
                  type="button"
                  className="kim-provider-picker__usage-link"
                  title="View cloud usage on ollama.com"
                  onClick={() => void openUrl('https://ollama.com/settings')}
                >
                  View usage ↗
                </button>
              </div>
            )
          )}
            </div>
          )}

          {/* ── Model provider ──────────────────────────────────────── */}
          <div className="kim-provider-picker__section-label">
            Model <span>local, cloud, browser, or API</span>
          </div>
          <button
            type="button"
            className={'kim-provider-picker__item' + (isOllama ? ' is-active' : '')}
            onClick={() => pick('ollama')}
          >
            <span className="kim-provider-picker__item-name">Ollama</span>
            <span className="kim-provider-picker__badge">
              no API key
            </span>
          </button>

          {/* ── Browser providers ───────────────────────────────────── */}
          <div className="kim-provider-picker__section-label">
            Browser <span>uses your existing sign-in</span>
          </div>
          {BROWSER_PROVIDERS.map(p => {
            const selected = isBrowser && currentBrowserSite === p.id;
            const showSignedIn = selected && authForCurrent.status?.signed_in;
            const showSignIn = selected && authForCurrent.probed && !authForCurrent.status?.signed_in;
            return (
              <div key={`b-${p.id}`} className={'kim-provider-picker__row' + (selected ? ' is-active' : '')}>
                <button
                  type="button"
                  className={'kim-provider-picker__item' + (selected ? ' is-active' : '')}
                  onClick={() => pick(`browser:${p.id}`)}
                >
                  <span className="kim-provider-picker__item-name">{p.label}</span>
                  {showSignedIn && (
                    <span className="kim-provider-picker__badge kim-provider-picker__badge--ok">
                      {authForCurrent.status?.email
                        ? `${authForCurrent.status.email}`
                        : 'signed in'}
                    </span>
                  )}
                  {showSignIn && (
                    <span className="kim-provider-picker__badge kim-provider-picker__badge--warn">
                      not signed in
                    </span>
                  )}
                </button>
                {/* Per-row action: sign in / sign out for the SELECTED browser
                 *  provider only. Showing this for every row would mean probing
                 *  every provider on mount (5 background fetches), which the
                 *  app deliberately avoids. */}
                {selected && showSignedIn && (
                  <button
                    type="button"
                    className="kim-provider-picker__row-action kim-provider-picker__row-action--danger"
                    onClick={(e) => { e.stopPropagation(); void signOutCurrent(); }}
                    title="Sign out (you'll need to sign in again to use this provider)"
                  >
                    Sign out
                  </button>
                )}
                {selected && showSignIn && (
                  <button
                    type="button"
                    className="kim-provider-picker__row-action"
                    onClick={(e) => { e.stopPropagation(); void signInCurrent(); }}
                  >
                    Sign in
                  </button>
                )}
              </div>
            );
          })}

          {/* ── API providers ───────────────────────────────────────── */}
          <div className="kim-provider-picker__section-label">
            API <span>requires key in settings</span>
          </div>
          {API_PROVIDERS.map(p => {
            const selected = currentApiProvider === p.id;
            return (
              <button
                key={`a-${p.id}`}
                type="button"
                className={'kim-provider-picker__item' + (selected ? ' is-active' : '')}
                onClick={() => pick(p.id)}
              >
                <span className="kim-provider-picker__item-name">{p.label}</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

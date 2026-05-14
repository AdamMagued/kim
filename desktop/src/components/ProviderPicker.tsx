import { useState, useRef, useEffect, useMemo, useCallback } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { listen, type UnlistenFn } from '@tauri-apps/api/event';
import { useAuthStatus } from '../hooks/useAuthStatus';
import { toast } from './Toast';

// ── Types (mirror RevampSettings.tsx / src-tauri OllamaStatus) ────────────
interface OllamaModelInfo {
  name: string;
  size: number;
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
  /** Called when the user picks a different Ollama model. */
  onChangeOllamaModel: (mode: 'local' | 'cloud', model: string) => void | Promise<void>;
  /** Disabled while a task is running. */
  disabled?: boolean;
}

const BROWSER_PROVIDERS: { id: string; label: string }[] = [
  { id: 'claude',   label: 'Claude' },
  { id: 'chatgpt',  label: 'ChatGPT' },
  { id: 'gemini',   label: 'Gemini' },
  { id: 'grok',     label: 'Grok' },
  { id: 'deepseek', label: 'DeepSeek' },
];

const API_PROVIDERS: { id: string; label: string }[] = [
  { id: 'claude',   label: 'Claude API' },
  { id: 'openai',   label: 'OpenAI API' },
  { id: 'gemini',   label: 'Gemini API' },
  { id: 'deepseek', label: 'DeepSeek API' },
];

function bytesToHuman(n: number): string {
  if (!n) return '';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

/**
 * Unified provider/model picker. Replaces the older AuthIndicator chip with a
 * single control that lets the user:
 *   - Switch between Browser / API / Local (Ollama) providers.
 *   - Sign in to a browser provider inline.
 *   - Pick from available Ollama models (local + cloud) with search.
 *
 * The pill shows the CURRENT selection plus a status badge — e.g.
 * "Browser: ChatGPT · signed in" or "Ollama · gpt-oss:120b-cloud · connected"
 * or "Claude API". Click to open the picker panel.
 */
export function ProviderPicker({
  resolvedProvider,
  ollama,
  onChangeProvider,
  onChangeOllamaModel,
  disabled = false,
}: ProviderPickerProps) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState('');
  const [ollamaStatus, setOllamaStatus] = useState<OllamaStatus | null>(null);
  const [ollamaLoading, setOllamaLoading] = useState(false);
  // Mode tab inside the Ollama section — defaults to whatever the user last
  // saved in settings, but the user can flip it inside the dropdown to browse
  // the other side's models without committing yet (no save until they click
  // a model).
  const [ollamaModeTab, setOllamaModeTab] = useState<'local' | 'cloud'>(ollama.mode);
  const panelRef = useRef<HTMLDivElement | null>(null);

  // Currently active provider category — derived from resolvedProvider.
  const isBrowser = resolvedProvider.startsWith('browser:');
  const isOllama  = resolvedProvider === 'ollama';
  const currentBrowserSite = isBrowser ? resolvedProvider.slice('browser:'.length) : '';
  const currentApiProvider = !isBrowser && !isOllama ? resolvedProvider : '';

  // Keep the in-dropdown tab in sync with the persisted Ollama mode when it
  // changes from elsewhere (e.g. user switched via Settings panel).
  useEffect(() => {
    setOllamaModeTab(ollama.mode);
  }, [ollama.mode]);

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
      const model =
        ollama.mode === 'cloud' ? ollama.cloud_model : ollama.local_model;
      return model ? `Ollama · ${model}` : 'Ollama';
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

  // Models shown for the currently-selected Ollama mode tab. Local and cloud
  // are two distinct catalogues from the user's perspective — the user picks
  // the bucket first ("Local" or "Cloud") and only then sees the models that
  // live in it. Search filters within the active bucket.
  const activeOllamaModels = useMemo(() => {
    const list = ollamaModeTab === 'cloud'
      ? (ollamaStatus?.cloud_models ?? [])
      : (ollamaStatus?.local_models ?? []);
    const q = search.trim().toLowerCase();
    return q ? list.filter(m => m.name.toLowerCase().includes(q)) : list;
  }, [ollamaStatus, ollamaModeTab, search]);

  // Action handlers ------------------------------------------------------
  const pick = (next: string) => {
    if (disabled) return;
    void onChangeProvider(next);
    setOpen(false);
  };

  const pickOllamaModel = (mode: 'local' | 'cloud', model: string) => {
    if (disabled) return;
    void onChangeOllamaModel(mode, model);
    if (resolvedProvider !== 'ollama') void onChangeProvider('ollama');
    setOpen(false);
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
        title="Change model or sign in"
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
          {/* ── Local (Ollama) ──────────────────────────────────────── */}
          <div className="kim-provider-picker__section-label">
            Ollama <span>local or cloud · no API key</span>
          </div>

          {/* Connection state row — shown regardless of which tab is active,
           *  because if Ollama itself isn't running you can't reach either
           *  mode. (Cloud models still go THROUGH the local ollama daemon.) */}
          {(() => {
            if (ollamaLoading && !ollamaStatus) {
              return (
                <div className="kim-provider-picker__status">
                  Checking Ollama…
                </div>
              );
            }
            if (!ollamaStatus || !ollamaStatus.installed) {
              return (
                <div className="kim-provider-picker__status kim-provider-picker__status--warn">
                  <span>Ollama not installed</span>
                  <a href="https://ollama.com/download" target="_blank" rel="noreferrer">
                    Get it →
                  </a>
                </div>
              );
            }
            if (!ollamaStatus.running) {
              return (
                <div className="kim-provider-picker__status kim-provider-picker__status--warn">
                  Ollama installed but not running
                </div>
              );
            }
            return (
              <div className="kim-provider-picker__status kim-provider-picker__status--ok">
                Connected{ollamaStatus.version ? ` · v${ollamaStatus.version}` : ''}
              </div>
            );
          })()}

          {/* Local/Cloud mode tabs — only meaningful when the daemon is up. */}
          {ollamaStatus?.running && (
            <div className="kim-provider-picker__ollama-tabs" role="tablist">
              <button
                type="button"
                role="tab"
                aria-selected={ollamaModeTab === 'local'}
                className={
                  'kim-provider-picker__ollama-tab' +
                  (ollamaModeTab === 'local' ? ' is-active' : '')
                }
                onClick={() => { setOllamaModeTab('local'); setSearch(''); }}
              >
                Local
                <span className="kim-provider-picker__ollama-tab-count">
                  {ollamaStatus.local_models.length}
                </span>
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={ollamaModeTab === 'cloud'}
                className={
                  'kim-provider-picker__ollama-tab' +
                  (ollamaModeTab === 'cloud' ? ' is-active' : '')
                }
                onClick={() => { setOllamaModeTab('cloud'); setSearch(''); }}
              >
                Cloud
                <span className="kim-provider-picker__ollama-tab-count">
                  {ollamaStatus.cloud_models.length}
                </span>
              </button>
            </div>
          )}

          {/* Cloud-specific status (signed in to ollama.com or not). Only when
           *  Cloud tab is active and the daemon is reachable. */}
          {ollamaStatus?.running && ollamaModeTab === 'cloud' && (
            ollamaStatus.cloud_connected ? (
              <div className="kim-provider-picker__status kim-provider-picker__status--ok">
                Cloud signed in
              </div>
            ) : (
              <div className="kim-provider-picker__status kim-provider-picker__status--warn">
                <span>{ollamaStatus.cloud_message ?? 'Not signed in to Ollama Cloud'}</span>
                <button
                  type="button"
                  className="kim-provider-picker__row-action"
                  onClick={async (e) => {
                    e.stopPropagation();
                    try {
                      await invoke('ollama_signin');
                      toast('Opening Ollama sign-in…', 'info', 2500);
                      setTimeout(() => { void refreshOllama(); }, 800);
                    } catch (err) {
                      toast(`Could not open Ollama sign-in: ${err}`, 'error', 4000);
                    }
                  }}
                >
                  Sign in
                </button>
              </div>
            )
          )}

          {/* Search field appears once the active bucket has enough entries
           *  to justify it (≥6). Below that it's just clutter. */}
          {ollamaStatus?.running &&
            (ollamaModeTab === 'cloud' ? ollamaStatus.cloud_models : ollamaStatus.local_models).length > 6 && (
              <input
                type="text"
                className="kim-provider-picker__search"
                placeholder={`Search ${ollamaModeTab} models…`}
                value={search}
                onChange={e => setSearch(e.target.value)}
              />
            )}

          {/* Active-bucket model list. */}
          {ollamaStatus?.running && activeOllamaModels.length > 0 && (
            <div className="kim-provider-picker__model-list">
              {activeOllamaModels.map(m => {
                const selected = isOllama && (
                  ollamaModeTab === 'cloud'
                    ? ollama.cloud_model === m.name
                    : ollama.local_model === m.name
                );
                return (
                  <button
                    key={`${ollamaModeTab}-${m.name}`}
                    type="button"
                    className={'kim-provider-picker__item' + (selected ? ' is-active' : '')}
                    onClick={() => pickOllamaModel(ollamaModeTab, m.name)}
                  >
                    <span className="kim-provider-picker__item-name">{m.name}</span>
                    <span className="kim-provider-picker__badge">
                      {ollamaModeTab === 'cloud'
                        ? (m.installed ? 'pulled' : 'cloud')
                        : (bytesToHuman(m.size) || 'local')}
                    </span>
                  </button>
                );
              })}
            </div>
          )}

          {/* Empty states for the active bucket. */}
          {ollamaStatus?.running && activeOllamaModels.length === 0 && search && (
            <div className="kim-provider-picker__status">
              No {ollamaModeTab} models match "{search}"
            </div>
          )}
          {ollamaStatus?.running && activeOllamaModels.length === 0 && !search && ollamaModeTab === 'local' && (
            <div className="kim-provider-picker__status">
              No local models yet. Pull one in Settings → AI → Ollama.
            </div>
          )}

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

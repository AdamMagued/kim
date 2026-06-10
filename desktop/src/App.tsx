import { useState, useEffect, useCallback } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { getCurrentWindow } from '@tauri-apps/api/window';
import './index.css';

import { useTheme } from './hooks/useTheme';
import { useSessions } from './hooks/useSessions';
import { useAccount } from './hooks/useAccount';

import { RevampSidebar, RevampSettings } from './components/kim-ui';
import { ChatView } from './components/ChatView';
import { UpdateModal } from './components/UpdateModal';
import { OnboardingFlow } from './components/OnboardingFlow';
import { ToastProvider, toast } from './components/Toast';

import type { SessionInfo, Settings, AccentTheme, KimAccount } from './types';
import { DEFAULT_SETTINGS } from './types';

// ── Helpers ──────────────────────────────────────────────────────────────────

function loadSettings(): Settings {
  try {
    const raw = localStorage.getItem('kim-settings');
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<Settings>;
      return {
        ...DEFAULT_SETTINGS,
        ...parsed,
        voice: {
          ...DEFAULT_SETTINGS.voice,
          ...(parsed.voice ?? {}),
        },
        schedule_timer: {
          ...DEFAULT_SETTINGS.schedule_timer,
          ...(parsed.schedule_timer ?? {}),
        },
        ollama: {
          ...DEFAULT_SETTINGS.ollama,
          ...(parsed.ollama ?? {}),
        },
      };
    }
  } catch {
    // ignore
  }
  return DEFAULT_SETTINGS;
}

function saveSettings(s: Settings) {
  localStorage.setItem('kim-settings', JSON.stringify(s));
}

interface GithubRelease {
  tag_name: string;
  body: string;
  html_url: string;
}

interface ScheduleTimerTickEvent {
  tick_count: number;
  result?: string | null;
  error?: string | null;
}

interface ScheduleRunDueResult {
  ok?: boolean;
  launched?: boolean;
  skipped?: boolean;
  task?: string;
  task_id?: string;
  error?: string;
}

function compareSemver(a: string, b: string): number {
  const pa = a.split('.').map(n => parseInt(n, 10) || 0);
  const pb = b.split('.').map(n => parseInt(n, 10) || 0);
  const len = Math.max(pa.length, pb.length);
  for (let i = 0; i < len; i++) {
    const x = pa[i] ?? 0;
    const y = pb[i] ?? 0;
    if (x > y) return 1;
    if (x < y) return -1;
  }
  return 0;
}

function applyAccent(accent: AccentTheme) {
  document.documentElement.setAttribute('data-accent', accent);
}

function parseScheduleRunDueResult(json: string | null | undefined): ScheduleRunDueResult | null {
  if (!json) return null;
  try {
    const parsed = JSON.parse(json);
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) return null;
    return parsed as ScheduleRunDueResult;
  } catch {
    return null;
  }
}

function isNoDragTarget(target: EventTarget | null): boolean {
  return target instanceof Element && Boolean(target.closest(
    'button, input, select, textarea, a, [role="button"], .kim-no-drag'
  ));
}

function sessionKey(session: SessionInfo): string {
  return session.session_key ?? `${session.session_type}:${session.date}:${session.session_id}`;
}

// ── App ───────────────────────────────────────────────────────────────────────

export default function App() {
  const [settings, setSettings] = useState<Settings>(loadSettings);
  const { setTheme } = useTheme(settings.theme);
  const { account, loading: accountLoading, setAccount } = useAccount();

  const { kimSessions, codexSessions, refresh } = useSessions(settings);

  const [activeSession, setActiveSession] = useState<SessionInfo | null>(null);
  const [newChatMode, setNewChatMode] = useState(false);
  // When a task completes in newChatMode, ChatView tells us the session ID.
  // We store it here and auto-select the session once kimSessions refreshes.
  const [pendingSelectSessionId, setPendingSelectSessionId] = useState<string | null>(null);
  const [sessionRefreshNonce, setSessionRefreshNonce] = useState(0);
  // Incremented every time the user presses New Chat — used as ChatView's key
  // so the component fully remounts (clearing all transient state) each time.
  const [chatSerial, setChatSerial] = useState(0);
  const [activeTab, setActiveTab] = useState<'chat' | 'code'>('chat');
  const [activeProjectPath, setActiveProjectPath] = useState<string | null>(null);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [settingsInitialPane, setSettingsInitialPane] = useState<
    'appearance' | 'ai' | 'voice' | 'paths' | 'data' | 'schedule' | 'account' | 'mcp' | 'feedback' | 'about' | undefined
  >(undefined);

  const [appVersion, setAppVersion] = useState('0.1.0');
  const [updateInfo, setUpdateInfo] = useState<GithubRelease | null>(null);
  const [showUpdate, setShowUpdate] = useState(false);

  // Globally suppress the WebView's native context menu. Right-click should
  // never show "Inspect Element", "Reload", "Back" etc. on our app. Components
  // that want a custom context menu (e.g. session items) attach their own
  // onContextMenu and call e.stopPropagation() before our handler runs.
  useEffect(() => {
    const onContextMenu = (e: MouseEvent) => {
      // Allow text inputs/textareas to keep their context menu so users can
      // paste/copy. Everywhere else, swallow the event.
      const target = e.target as HTMLElement | null;
      if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)) {
        return;
      }
      e.preventDefault();
    };
    // Block the F12 / Cmd+Opt+I devtools shortcuts as a belt-and-braces
    // measure on top of Tauri's release-build devtools-off default.
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'F12') {
        e.preventDefault();
        return;
      }
      if ((e.metaKey || e.ctrlKey) && e.altKey && (e.key === 'I' || e.key === 'i' || e.key === 'J' || e.key === 'j' || e.key === 'C' || e.key === 'c')) {
        e.preventDefault();
        return;
      }
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && (e.key === 'I' || e.key === 'i' || e.key === 'J' || e.key === 'j' || e.key === 'C' || e.key === 'c')) {
        e.preventDefault();
      }
    };
    document.addEventListener('contextmenu', onContextMenu);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('contextmenu', onContextMenu);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, []);

  useEffect(() => {
    invoke<string>('get_app_version')
      .then(v => {
        setAppVersion(v);
        // Silently check for updates on startup — show a banner if one exists
        silentUpdateCheck(v);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    let unlisten: (() => void) | undefined;
    listen<ScheduleTimerTickEvent>('schedule-timer-tick', (event) => {
      const payload = event.payload;
      if (payload.error) {
        toast(`Schedule timer error: ${payload.error}`, 'error', 7000);
        return;
      }
      const result = parseScheduleRunDueResult(payload.result);
      if (result?.error) {
        toast(`Scheduled task failed: ${result.error}`, 'error', 7000);
      } else if (result?.launched) {
        toast(`Scheduled task launched: ${result.task || result.task_id || 'task'}`, 'success', 4500);
      }
    }).then((fn) => { unlisten = fn; }).catch(() => {});
    return () => { unlisten?.(); };
  }, []);

  useEffect(() => {
    if (!settings.schedule_timer.enabled) return;
    const intervalSeconds = settings.schedule_timer.interval_seconds || DEFAULT_SETTINGS.schedule_timer.interval_seconds;
    invoke('start_schedule_timer', { intervalSeconds })
      .catch((e) => {
        toast(`Could not start schedule timer: ${String(e)}`, 'error', 7000);
      });
  }, [settings.schedule_timer.enabled, settings.schedule_timer.interval_seconds]);

  async function silentUpdateCheck(currentVersion: string) {
    try {
      const resp = await fetch(
        'https://api.github.com/repos/AdamMagued/kim/releases/latest',
        { headers: { Accept: 'application/vnd.github+json' } }
      );
      if (!resp.ok) return; // fail silently on startup
      const data = (await resp.json()) as GithubRelease;
      const latest = data.tag_name.replace(/^v/, '');
      if (compareSemver(latest, currentVersion) > 0) {
        setUpdateInfo(data);
        // Delay slightly so the app has time to finish loading
        setTimeout(() => {
          toast(`Kim ${latest} is available — you're on ${currentVersion}. Click to update.`, 'info', 8000);
          setShowUpdate(true);
        }, 2000);
      }
    } catch {
      // Network unavailable on startup — that's fine, ignore silently
    }
  }

  useEffect(() => { applyAccent(settings.accent ?? 'indigo'); }, [settings.accent]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key.toLowerCase() === 'n') { e.preventDefault(); handleNewChat(); }
      else if (mod && e.key === ',') { e.preventDefault(); setSettingsInitialPane(undefined); setShowSettings(true); }
      else if (mod && e.key.toLowerCase() === 'b') { e.preventDefault(); setSidebarCollapsed(v => !v); }
      else if (e.key === 'Escape') {
        if (showSettings) setShowSettings(false);
        else if (showUpdate) setShowUpdate(false);
      }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [showSettings, showUpdate]);

  function handleSettingsChange(next: Settings) {
    setSettings(next);
    saveSettings(next);
    if (next.theme !== settings.theme) setTheme(next.theme);
  }

  function handleSelectSession(session: SessionInfo) {
    setActiveSession(session);
    setNewChatMode(false);
    setChatSerial(s => s + 1);   // user-initiated select → remount for clean slate
  }

  function handleNewChat() {
    setActiveSession(null);
    setNewChatMode(true);
    setChatSerial(s => s + 1);   // force ChatView remount → clean slate
  }

  function handleTabChange(tab: 'chat' | 'code') {
    setActiveTab(tab);
    setActiveSession(null);
    setNewChatMode(false);
    // A pending Chat-session selection must not fire after the user moves to
    // Code — it would snap the UI back to the last Chat conversation.
    setPendingSelectSessionId(null);
  }

  function handleSelectProject(path: string) {
    setActiveTab('code');
    setActiveProjectPath(path);
    // Selecting a project should not auto-start a new session; it only sets the
    // active project context for subsequent "New session" actions.
  }

  function handleNewChatInProject(path: string) {
    setActiveTab('code');
    setActiveProjectPath(path);
    setActiveSession(null);
    setNewChatMode(true);
    setChatSerial(s => s + 1);
  }

  function handleRemoveProject(path: string) {
    if (activeProjectPath === path) {
      setActiveProjectPath(null);
      setActiveSession(null);
      setNewChatMode(false);
    }
  }

  function handleHeaderMouseDown(e: React.MouseEvent<HTMLElement>) {
    if (e.button !== 0 || isNoDragTarget(e.target)) return;
    void getCurrentWindow().startDragging();
  }

  const handleTaskDone = useCallback((sessionId?: string, completedSession?: SessionInfo) => {
    // Auto-navigate to the just-completed session even from newChatMode so the
    // sidebar highlights it and the user can clearly see "this chat is saved"
    // (issue #3 §4: chats appearing to vanish after task completion). The
    // ChatView's key continues to use the same id so transitioning from
    // newChatMode → loaded does not remount.
    if (completedSession) {
      // Don't navigate when a Codex session from a different project completes —
      // switching would replace the current chat with only the latest turn.
      if (
        activeTab === 'code' &&
        completedSession.session_type === 'codex' &&
        completedSession.project_path !== activeProjectPath
      ) {
        setSessionRefreshNonce(n => n + 1);
        refresh();
        return;
      }
      // Code-tab (Codex) completion: Codex always creates a NEW session file
      // (it doesn't support --resume). If the user was already viewing an
      // OLD session, navigating to the new session replaces the old messages
      // with just the latest turn — causing the "chat reset" bug. Fix: stay
      // on the current session so old history + liveHistory remain visible;
      // the new session still appears in the sidebar for later access.
      setActiveSession(prev => {
        if (prev && prev.session_id !== completedSession.session_id) {
          // Already viewing a different (old) session — don't navigate away.
          return prev;
        }
        // First task from newChatMode or same session — navigate normally.
        setNewChatMode(false);
        return completedSession;
      });
    } else if (sessionId && activeTab === 'chat') {
      // Only trigger the pending-select flow when the session isn't already
      // active. On follow-up tasks the session is already selected — calling
      // setPendingSelectSessionId again causes the useEffect to fire with a
      // new session object reference, which triggers a message reload cycle
      // and makes the UI briefly flash/reset ("chat reset" bug).
      setActiveSession(prev => {
        if (prev && prev.session_id === sessionId) {
          // Already showing this session — skip the re-select dance.
          return prev;
        }
        setPendingSelectSessionId(sessionId);
        return prev;
      });
    }
    setSessionRefreshNonce(n => n + 1);
    refresh();
    // Poll a few times — Python flushes the last JSONL line right before
    // exit, and the OS may take a beat to make the file visible to readdir.
    setTimeout(() => { refresh(); }, 400);
    setTimeout(() => { refresh(); }, 1200);
    setTimeout(() => { refresh(); }, 2400);
  }, [activeTab, activeProjectPath, refresh]);

  // Auto-select the just-completed session once it appears in kimSessions.
  // Only applies to the Chat tab — Code tab manages its own session navigation.
  useEffect(() => {
    if (!pendingSelectSessionId) return;
    if (activeTab !== 'chat') {
      setPendingSelectSessionId(null);
      return;
    }
    const session = kimSessions.find(s => s.session_id === pendingSelectSessionId);
    if (session) {
      setActiveSession(session);
      setNewChatMode(false);
      setPendingSelectSessionId(null);
    }
  }, [kimSessions, pendingSelectSessionId, activeTab]);

  async function checkForUpdates() {
    toast('Checking for updates…', 'info', 2000);
    try {
      const resp = await fetch(
        'https://api.github.com/repos/AdamMagued/kim/releases/latest',
        { headers: { Accept: 'application/vnd.github+json' } }
      );
      if (!resp.ok) {
        if (resp.status === 404) { toast('No published release found yet.', 'info'); return; }
        if (resp.status === 403) { toast('Rate-limited by GitHub — try again in a minute.', 'warning'); return; }
        toast(`Update check failed (HTTP ${resp.status}).`, 'error');
        return;
      }
      const data = (await resp.json()) as GithubRelease;
      const latest = data.tag_name.replace(/^v/, '');
      if (compareSemver(latest, appVersion) > 0) {
        setUpdateInfo(data);
        setShowUpdate(true);
      } else {
        toast(`You're on the latest version (v${appVersion}).`, 'success');
      }
    } catch {
      toast('Could not reach GitHub. Check your internet connection.', 'error');
    }
  }

  if (accountLoading) return <div className="kim-app" />;

  if (!account) {
    return (
      <OnboardingFlow
        onComplete={async (newAccount: KimAccount) => {
          await setAccount(newAccount);
        }}
      />
    );
  }

  return (
    <div className="kim-app kim-app--row">
      <RevampSidebar
        kimSessions={kimSessions}
        activeSessionId={activeSession ? sessionKey(activeSession) : null}
        onSelectSession={handleSelectSession}
        onNewChat={handleNewChat}
        collapsed={sidebarCollapsed}
        onToggle={() => setSidebarCollapsed(v => !v)}
        onOpenSettings={(pane) => {
          setSettingsInitialPane(pane);
          setShowSettings(true);
        }}
        account={account}
        onAccountChange={setAccount}
        activeTab={activeTab}
        onTabChange={handleTabChange}
        activeProjectPath={activeProjectPath}
        onSelectProject={handleSelectProject}
        onRemoveProject={handleRemoveProject}
        onNewChatInProject={handleNewChatInProject}
        sessionRefreshNonce={sessionRefreshNonce}
        appVersion={appVersion}
        theme={settings.theme}
        onCycleTheme={() => {
          const order: Array<typeof settings.theme> = ['light', 'system', 'dark'];
          const idx = order.indexOf(settings.theme);
          const next = order[(idx + 1) % order.length];
          handleSettingsChange({ ...settings, theme: next });
        }}
      />

      <main className="kim-main">
        <header className="kim-topbar" data-tauri-drag-region onMouseDown={handleHeaderMouseDown}>
          {(activeSession || newChatMode) && (
            <div className="kim-topbar__title" style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
              {(newChatMode ? activeTab === 'code' : activeSession?.session_type !== 'kim') && (
                <span
                  style={{
                    fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
                    fontSize: 10,
                    letterSpacing: '0.14em',
                    color: 'var(--kim-accent)',
                    border: '1px solid var(--kim-accent-line)',
                    padding: '4px 8px',
                    borderRadius: 6,
                  }}
                >
                  CODE
                </span>
              )}
              {newChatMode ? (
                <span style={{ color: 'var(--kim-text-2)', fontSize: 13, display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                  <span className="kr-pulse-dot" style={{ background: 'var(--kim-green)' }} />
                  {activeTab === 'code' ? 'New session' : 'New chat'}
                </span>
              ) : activeSession && (
                <>
                  <span style={{ color: 'var(--kim-text)', fontSize: 13.5 }}>
                    {activeSession.title?.trim() || activeSession.session_id}
                  </span>
                  <button
                    type="button"
                    className="kim-header__summarize-btn kim-no-drag"
                    title={activeSession.has_summary ? 'Refresh the summary' : 'Generate a summary for this conversation'}
                    onClick={async () => {
                      try {
                        await invoke('summarize_session', {
                          sessionId: activeSession.session_id,
                          sessionType: activeSession.session_type,
                          projectRoot: activeProjectPath ?? null,
                        });
                        toast('Summary generated', 'success');
                        await refresh();
                      } catch (e) {
                        toast(`Summarize failed: ${e}`, 'error');
                      }
                    }}
                  >
                    {activeSession.has_summary ? 'Refresh summary' : 'Generate summary'}
                  </button>
                </>
              )}
            </div>
          )}

          <div style={{ flex: 1 }} />

          {activeTab === 'code' && activeProjectPath && (
            <span
              className="kim-no-drag"
              style={{
                fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
                fontSize: 11,
                color: 'var(--kim-text-3)',
                marginRight: 8,
              }}
              title={activeProjectPath}
            >
              ~/{activeProjectPath.split('/').slice(-2).join('/')}
            </span>
          )}

          <button
            type="button"
            className="kim-topbar__connectors-btn kim-no-drag"
            onClick={() => window.dispatchEvent(new CustomEvent('kim-open-connectors'))}
            aria-label="Open connectors"
            title="Connectors"
          >
            <span className="kim-topbar__connectors-glyph" aria-hidden="true">
              <span />
              <span />
            </span>
          </button>
        </header>

        <ChatView
          key={`chat-${activeTab}-${chatSerial}`}
          session={activeSession}
          newChatMode={newChatMode}
          settings={settings}
          onSettingsChange={handleSettingsChange}
          onTaskDone={handleTaskDone}
          account={account}
          onAccountChange={setAccount}
          onOpenSettings={(pane) => {
            setSettingsInitialPane(pane);
            setShowSettings(true);
          }}
          activeTab={activeTab}
          activeProjectPath={activeProjectPath}
          reloadSessions={refresh}
          onNewChat={handleNewChat}
          onNewCodeSession={() => { setActiveTab('code'); handleNewChat(); }}
          onSelectProject={handleSelectProject}
          recentSessions={activeTab === 'code' ? codexSessions : kimSessions}
          onSelectSession={handleSelectSession}
        />
      </main>

      {showSettings && (
        <RevampSettings
          settings={settings}
          onChange={handleSettingsChange}
          onClose={() => { setShowSettings(false); setSettingsInitialPane(undefined); }}
          appVersion={appVersion}
          onCheckUpdate={checkForUpdates}
          account={account}
          onAccountChange={setAccount}
          initialPane={settingsInitialPane}
        />
      )}

      {showUpdate && updateInfo && (
        <UpdateModal
          currentVersion={appVersion}
          latestVersion={updateInfo.tag_name.replace(/^v/, '')}
          releaseNotes={updateInfo.body ?? ''}
          onDismiss={() => setShowUpdate(false)}
        />
      )}

      <ToastProvider />
    </div>
  );
}

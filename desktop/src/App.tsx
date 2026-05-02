import { useState, useEffect, useCallback } from 'react';
import { invoke } from '@tauri-apps/api/core';
import './index.css';

import { useTheme } from './hooks/useTheme';
import { useSessions } from './hooks/useSessions';
import { useAccount } from './hooks/useAccount';

import { Sidebar } from './components/Sidebar';
import { ChatView } from './components/ChatView';
import { SettingsPanel } from './components/SettingsPanel';
import { UpdateModal } from './components/UpdateModal';
import { ThemeToggle } from './components/ThemeToggle';
import { OnboardingFlow } from './components/OnboardingFlow';
import { ToastProvider, toast } from './components/Toast';

import type { SessionInfo, Settings, Theme, AccentTheme, KimAccount } from './types';
import { DEFAULT_SETTINGS } from './types';

// ── Helpers ──────────────────────────────────────────────────────────────────

function loadSettings(): Settings {
  try {
    const raw = localStorage.getItem('kim-settings');
    if (raw) return { ...DEFAULT_SETTINGS, ...(JSON.parse(raw) as Partial<Settings>) };
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

// ── App ───────────────────────────────────────────────────────────────────────

export default function App() {
  const [settings, setSettings] = useState<Settings>(loadSettings);
  const { setTheme } = useTheme(settings.theme);
  const { account, loading: accountLoading, setAccount } = useAccount();

  const { kimSessions, loading, refresh } = useSessions(settings);

  const [activeSession, setActiveSession] = useState<SessionInfo | null>(null);
  const [newChatMode, setNewChatMode] = useState(false);
  // When a task completes in newChatMode, ChatView tells us the session ID.
  // We store it here and auto-select the session once kimSessions refreshes.
  const [pendingSelectSessionId, setPendingSelectSessionId] = useState<string | null>(null);
  // Incremented every time the user presses New Chat — used as ChatView's key
  // so the component fully remounts (clearing all transient state) each time.
  const [chatSerial, setChatSerial] = useState(0);
  const [activeTab, setActiveTab] = useState<'chat' | 'code'>('chat');
  const [activeProjectPath, setActiveProjectPath] = useState<string | null>(null);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [showSettings, setShowSettings] = useState(false);

  const [appVersion, setAppVersion] = useState('0.1.0');
  const [updateInfo, setUpdateInfo] = useState<GithubRelease | null>(null);
  const [showUpdate, setShowUpdate] = useState(false);

  useEffect(() => {
    invoke<string>('get_app_version')
      .then(v => {
        setAppVersion(v);
        // Silently check for updates on startup — show a banner if one exists
        silentUpdateCheck(v);
      })
      .catch(() => {});
  }, []);

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

  useEffect(() => { setTheme(settings.theme); }, [settings.theme, setTheme]);
  useEffect(() => { applyAccent(settings.accent ?? 'indigo'); }, [settings.accent]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key.toLowerCase() === 'n') { e.preventDefault(); handleNewChat(); }
      else if (mod && e.key === ',') { e.preventDefault(); setShowSettings(true); }
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
  }

  function handleThemeChange(next: Theme) {
    handleSettingsChange({ ...settings, theme: next });
  }

  function handleSelectSession(session: SessionInfo) {
    setActiveSession(session);
    setNewChatMode(false);
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
    // When switching tabs, don't automatically clear the selected project,
    // but maybe we just leave it so if they go back to Code it's there.
  }

  function handleSelectProject(path: string) {
    setActiveTab('code');
    setActiveProjectPath(path);
    // Selecting a project opens a new chat contextualized to that project
    setActiveSession(null);
    setNewChatMode(true);
    setChatSerial(s => s + 1);
  }

  const handleTaskDone = useCallback((sessionId?: string) => {
    // Only auto-navigate to the session when NOT in newChatMode.
    // In newChatMode the liveHistory state already displays the conversation
    // inline — auto-selecting the session would change the ChatView key,
    // causing a full unmount/remount (the "refresh flash" bug).
    if (sessionId && !newChatMode) setPendingSelectSessionId(sessionId);
    refresh();
    setTimeout(() => { refresh(); }, 500);
  }, [refresh, newChatMode]);

  // Auto-select the just-completed session once it appears in kimSessions.
  useEffect(() => {
    if (!pendingSelectSessionId) return;
    const session = kimSessions.find(s => s.session_id === pendingSelectSessionId);
    if (session) {
      setActiveSession(session);
      setNewChatMode(false);
      setPendingSelectSessionId(null);
    }
  }, [kimSessions, pendingSelectSessionId]);

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
    <div className="kim-app">
      <header className="kim-header" data-tauri-drag-region>
        <div className="kim-header__traffic-lights-spacer" />

        <div className="kim-header__brand">
          <svg className="kim-logo-mark" viewBox="0 0 52 52" fill="none">
            <circle cx="26" cy="26" r="6" fill="currentColor" />
            <path d="M26 8 A18 18 0 0 1 44 26" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" fill="none" opacity="0.9" />
            <path d="M26 44 A18 18 0 0 1 8 26" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" fill="none" opacity="0.9" />
            <circle cx="44" cy="26" r="3" fill="currentColor" opacity="0.7" />
            <circle cx="8" cy="26" r="3" fill="currentColor" opacity="0.7" />
          </svg>
          <span className="kim-header__title">Kim</span>
          <span className="kim-header__version">v{appVersion}</span>
        </div>

        {activeSession && !newChatMode && (
          <div className="kim-header__breadcrumb">
            <span className="kim-header__slash">/</span>
            <span className={`kim-header__session-badge kim-header__session-badge--${activeSession.session_type}`}>
              {activeSession.session_type === 'kim' ? 'Kim' : 'Code'}
            </span>
            <span className="kim-header__session-id">{activeSession.title?.trim() || activeSession.session_id}</span>
          </div>
        )}
        {newChatMode && (
          <div className="kim-header__breadcrumb">
            <span className="kim-header__slash">/</span>
            <span className={`kim-header__session-badge kim-header__session-badge--${activeTab === 'chat' ? 'kim' : 'claw'}`}>
              {activeTab === 'chat' ? 'Kim' : 'Code'}
            </span>
            <span className="kim-header__new-chat-label" style={{ marginLeft: '8px' }}>
              <span className="kim-pulse-dot" /> New chat
            </span>
          </div>
        )}

        <div style={{ flex: 1 }} />
        <ThemeToggle theme={settings.theme} onChange={handleThemeChange} />
      </header>

      <div className="kim-body">
        <Sidebar
          kimSessions={kimSessions}
          activeSessionId={activeSession?.session_id ?? null}
          onSelectSession={handleSelectSession}
          onNewChat={handleNewChat}
          collapsed={sidebarCollapsed}
          onToggle={() => setSidebarCollapsed(v => !v)}
          onOpenSettings={() => setShowSettings(true)}
          loading={loading}
          account={account}
          onAccountChange={setAccount}
          activeTab={activeTab}
          onTabChange={handleTabChange}
          activeProjectPath={activeProjectPath}
          onSelectProject={handleSelectProject}
          onRefreshSessions={refresh}
          kimSessionsDir={settings.kim_sessions_dir || null}
          clawSessionsDir={settings.claw_sessions_dir || null}
        />

        <ChatView
          key={activeSession ? activeSession.session_id : `new-${chatSerial}`}
          session={activeSession}
          newChatMode={newChatMode}
          settings={settings}
          onTaskDone={handleTaskDone}
          account={account}
          activeTab={activeTab}
          activeProjectPath={activeProjectPath}
        />
      </div>

      {showSettings && (
        <SettingsPanel
          settings={settings}
          onChange={handleSettingsChange}
          onClose={() => setShowSettings(false)}
          appVersion={appVersion}
          onCheckUpdate={checkForUpdates}
          account={account}
          onAccountChange={setAccount}
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

import { useState, useEffect, useCallback } from 'react';
import { invoke } from '@tauri-apps/api/core';
import './index.css';

import { useTheme } from './hooks/useTheme';
import { useSessions } from './hooks/useSessions';

import { Sidebar } from './components/Sidebar';
import { ChatView } from './components/ChatView';
import { SettingsPanel } from './components/SettingsPanel';
import { UpdateModal } from './components/UpdateModal';
import { ThemeToggle } from './components/ThemeToggle';

import type { SessionInfo, Settings, Theme } from './types';
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

// Compare "1.10.2" vs "1.9.5" correctly by numeric parts, not string order.
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

// ── App ───────────────────────────────────────────────────────────────────────

export default function App() {
  const [settings, setSettings] = useState<Settings>(loadSettings);

  // Theme — useTheme applies `.dark` to document.documentElement; no need
  // to duplicate the class on our root div.
  const { setTheme } = useTheme(settings.theme);

  // Sessions
  const { kimSessions, clawSessions, loading, refresh } = useSessions(settings);

  // UI state
  const [activeSession, setActiveSession] = useState<SessionInfo | null>(null);
  const [newChatMode, setNewChatMode] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [showSettings, setShowSettings] = useState(false);

  // Update modal
  const [appVersion, setAppVersion] = useState('0.1.0');
  const [updateInfo, setUpdateInfo] = useState<GithubRelease | null>(null);
  const [showUpdate, setShowUpdate] = useState(false);

  // Load app version on mount
  useEffect(() => {
    invoke<string>('get_app_version')
      .then(v => setAppVersion(v))
      .catch(() => {});
  }, []);

  // Sync theme change from settings
  useEffect(() => {
    setTheme(settings.theme);
  }, [settings.theme, setTheme]);

  // ── Keyboard shortcuts ─────────────────────────────────────────────────────
  //   Cmd/Ctrl + N  → New chat
  //   Cmd/Ctrl + ,  → Settings
  //   Cmd/Ctrl + B  → Toggle sidebar
  //   Esc           → Close any open modal
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key.toLowerCase() === 'n') {
        e.preventDefault();
        handleNewChat();
      } else if (mod && e.key === ',') {
        e.preventDefault();
        setShowSettings(true);
      } else if (mod && e.key.toLowerCase() === 'b') {
        e.preventDefault();
        setSidebarCollapsed(v => !v);
      } else if (e.key === 'Escape') {
        if (showSettings) setShowSettings(false);
        else if (showUpdate) setShowUpdate(false);
      }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [showSettings, showUpdate]);

  // Persist settings
  function handleSettingsChange(next: Settings) {
    setSettings(next);
    saveSettings(next);
  }

  // Theme toggle in header (quick toggle)
  function handleThemeChange(next: Theme) {
    const updated = { ...settings, theme: next };
    handleSettingsChange(updated);
  }

  // Session selection
  function handleSelectSession(session: SessionInfo) {
    setActiveSession(session);
    setNewChatMode(false);
  }

  function handleNewChat() {
    setActiveSession(null);
    setNewChatMode(true);
  }

  const handleTaskDone = useCallback(() => {
    refresh();
    // After a task completes, switch to the newest session
    setTimeout(() => {
      refresh();
    }, 500);
  }, [refresh]);

  // Check GitHub releases for updates
  async function checkForUpdates() {
    try {
      const resp = await fetch(
        'https://api.github.com/repos/AdamMagued/kim/releases/latest',
        { headers: { Accept: 'application/vnd.github+json' } }
      );
      if (!resp.ok) {
        if (resp.status === 404) {
          alert('No published release yet.');
          return;
        }
        if (resp.status === 403) {
          alert('Rate-limited by GitHub. Try again later.');
          return;
        }
        alert(`Update check failed (HTTP ${resp.status}).`);
        return;
      }
      const data = (await resp.json()) as GithubRelease;
      const latest = data.tag_name.replace(/^v/, '');
      if (compareSemver(latest, appVersion) > 0) {
        setUpdateInfo(data);
        setShowUpdate(true);
      } else {
        alert('You are on the latest version!');
      }
    } catch {
      alert('Could not check for updates. Make sure you are connected to the internet.');
    }
  }

  return (
    <div className="kim-app">
      {/* ── Header ──────────────────────────────────────────────────────────── */}
      <header className="kim-header">
        {/* macOS traffic-lights spacer (titleBarStyle=Overlay + hiddenTitle) */}
        <div className="kim-header__traffic-lights-spacer" />

        {/* Logo + name */}
        <div className="kim-header__brand">
          <div className="kim-logo" aria-hidden>
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M5 4v16M5 12h6l8-8M11 12l8 8" />
            </svg>
          </div>
          <span className="kim-header__title">Kim</span>
          <span className="kim-header__version">v{appVersion}</span>
        </div>

        {/* Active session breadcrumb */}
        {activeSession && !newChatMode && (
          <div className="kim-header__breadcrumb">
            <span className="kim-header__slash">/</span>
            <span
              className={`kim-header__session-badge kim-header__session-badge--${activeSession.session_type}`}
            >
              {activeSession.session_type === 'kim' ? 'Kim' : 'Claw'}
            </span>
            <span className="kim-header__session-id">{activeSession.session_id}</span>
          </div>
        )}
        {newChatMode && (
          <div className="kim-header__breadcrumb">
            <span className="kim-header__slash">/</span>
            <span className="kim-header__new-chat-label">
              <span className="kim-pulse-dot" /> New chat
            </span>
          </div>
        )}

        <div style={{ flex: 1 }} />

        {/* Theme toggle */}
        <ThemeToggle theme={settings.theme} onChange={handleThemeChange} />
      </header>

      {/* ── Body ────────────────────────────────────────────────────────────── */}
      <div className="kim-body">
        <Sidebar
          kimSessions={kimSessions}
          clawSessions={clawSessions}
          activeSessionId={activeSession?.session_id ?? null}
          onSelectSession={handleSelectSession}
          onNewChat={handleNewChat}
          collapsed={sidebarCollapsed}
          onToggle={() => setSidebarCollapsed(v => !v)}
          onOpenSettings={() => setShowSettings(true)}
          loading={loading}
        />

        <ChatView
          session={activeSession}
          newChatMode={newChatMode}
          settings={settings}
          onTaskDone={handleTaskDone}
        />
      </div>

      {/* ── Overlays ─────────────────────────────────────────────────────────── */}
      {showSettings && (
        <SettingsPanel
          settings={settings}
          onChange={handleSettingsChange}
          onClose={() => setShowSettings(false)}
          appVersion={appVersion}
          onCheckUpdate={checkForUpdates}
        />
      )}

      {showUpdate && updateInfo && (
        <UpdateModal
          currentVersion={appVersion}
          latestVersion={updateInfo.tag_name.replace(/^v/, '')}
          releaseNotes={updateInfo.body ?? ''}
          downloadUrl={updateInfo.html_url}
          onDismiss={() => setShowUpdate(false)}
        />
      )}
    </div>
  );
}

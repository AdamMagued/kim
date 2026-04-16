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

// ── App ───────────────────────────────────────────────────────────────────────

export default function App() {
  const [settings, setSettings] = useState<Settings>(loadSettings);

  // Theme — driven by settings.theme
  const { theme, setTheme } = useTheme(settings.theme);

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
      if (!resp.ok) return;
      const data = (await resp.json()) as GithubRelease;
      const latest = data.tag_name.replace(/^v/, '');
      if (latest !== appVersion) {
        setUpdateInfo(data);
        setShowUpdate(true);
      } else {
        alert('You are on the latest version!');
      }
    } catch {
      alert('Could not check for updates. Make sure you are connected to the internet.');
    }
  }

  // Resolved theme for the outer class
  const darkClass = (theme === 'dark' || (theme === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches)) ? 'dark' : '';

  return (
    <div
      className={darkClass}
      style={{ height: '100vh', display: 'flex', flexDirection: 'column', background: 'var(--bg)', color: 'var(--text)' }}
    >
      {/* ── Header ──────────────────────────────────────────────────────────── */}
      <header
        style={{
          height: '48px',
          minHeight: '48px',
          display: 'flex',
          alignItems: 'center',
          padding: '0 16px',
          borderBottom: '1px solid var(--border)',
          background: 'var(--bg-sidebar)',
          gap: '12px',
          flexShrink: 0,
        }}
      >
        {/* Logo + name */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <div
            style={{
              width: '26px',
              height: '26px',
              borderRadius: '7px',
              background: 'var(--accent)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: '14px',
              color: '#fff',
              fontWeight: 700,
              flexShrink: 0,
            }}
          >
            K
          </div>
          <span style={{ fontWeight: 700, fontSize: '15px', letterSpacing: '-0.01em' }}>
            Kim
          </span>
        </div>

        {/* Active session breadcrumb */}
        {activeSession && !newChatMode && (
          <>
            <span style={{ color: 'var(--border)', fontSize: '16px' }}>/</span>
            <span
              style={{
                fontSize: '13px',
                color: 'var(--text-muted)',
                fontFamily: 'monospace',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                maxWidth: '200px',
              }}
            >
              {activeSession.session_id}
            </span>
          </>
        )}
        {newChatMode && (
          <>
            <span style={{ color: 'var(--border)', fontSize: '16px' }}>/</span>
            <span style={{ fontSize: '13px', color: 'var(--text-muted)' }}>New chat</span>
          </>
        )}

        <div style={{ flex: 1 }} />

        {/* Theme toggle */}
        <ThemeToggle theme={settings.theme} onChange={handleThemeChange} />
      </header>

      {/* ── Body ────────────────────────────────────────────────────────────── */}
      <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>
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

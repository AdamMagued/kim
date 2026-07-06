import { useEffect, useMemo, useRef, useState } from 'react';
import type { PointerEvent as ReactPointerEvent } from 'react';
import { createPortal } from 'react-dom';
import { invoke } from '@tauri-apps/api/core';
import { confirm, open as openDialog } from '@tauri-apps/plugin-dialog';
import type { SessionInfo, KimAccount, CodexProject, Theme } from '../../types';
import { toast } from '../Toast';

const SIDEBAR_MIN_WIDTH = 220;
const SIDEBAR_MAX_WIDTH = 520;
const SIDEBAR_DEFAULT_WIDTH = 280;

export type SettingsPane =
  | 'appearance'
  | 'ai'
  | 'paths'
  | 'data'
  | 'schedule'
  | 'account'
  | 'mcp'
  | 'feedback'
  | 'about';

interface Props {
  account: KimAccount;
  appVersion: string;
  kimSessions: SessionInfo[];
  activeSessionId: string | null;
  collapsed: boolean;
  onToggle: () => void;
  onNewChat: () => void;
  onSelectSession: (s: SessionInfo) => void;
  onOpenSettings: (pane?: SettingsPane) => void;
  activeTab: 'chat' | 'code';
  onTabChange: (tab: 'chat' | 'code') => void;
  showSessionMeta?: boolean;
  activeProjectPath?: string | null;
  onSelectProject?: (path: string) => void;
  onRemoveProject?: (path: string) => void;
  onNewChatInProject?: (path: string) => void;
  onAddProject?: () => void;
  /** Called when user adds a project via the dialog. */
  onAccountChange?: (a: KimAccount) => Promise<void>;
  /** Bumps when sessions refresh — triggers project re-load. */
  sessionRefreshNonce?: number;
  /** Current theme — used by account-menu theme toggle. */
  theme?: Theme;
  /** Cycle theme: light → system → dark → light. */
  onCycleTheme?: () => void;
  /** Open the Connectors panel explicitly — avoids global custom-event dispatch. */
  onOpenConnectors?: () => void;
}

export function sessionKey(s: SessionInfo): string {
  return s.session_key ?? `${s.session_type}:${s.date}:${s.session_id}`;
}

export function groupByDate(sessions: SessionInfo[]): { label: string; items: SessionInfo[] }[] {
  const today = new Date();
  const startOfToday = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
  const startOfYesterday = startOfToday - 86_400_000;
  const startOfWeek = startOfToday - 6 * 86_400_000;

  const groups: Record<string, SessionInfo[]> = {
    Today: [],
    Yesterday: [],
    'This week': [],
    Earlier: [],
  };

  for (const s of sessions) {
    // Parse date-only strings ('YYYY-MM-DD') as LOCAL midnight to match local bucket boundaries.
    // Date.parse('YYYY-MM-DD') yields UTC midnight, which shifts the day for negative-offset users.
    let ts: number;
    const localMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s.date);
    if (localMatch) {
      ts = new Date(Number(localMatch[1]), Number(localMatch[2]) - 1, Number(localMatch[3])).getTime();
    } else {
      ts = Date.parse(s.date);
    }
    if (Number.isNaN(ts)) {
      groups.Earlier.push(s);
      continue;
    }
    if (ts >= startOfToday) groups.Today.push(s);
    else if (ts >= startOfYesterday) groups.Yesterday.push(s);
    else if (ts >= startOfWeek) groups['This week'].push(s);
    else groups.Earlier.push(s);
  }

  return Object.entries(groups)
    .filter(([, items]) => items.length > 0)
    .map(([label, items]) => ({ label, items }));
}

export function formatTime(date: string): string {
  // Parse date-only strings as LOCAL midnight (same fix as groupByDate).
  let ts: number;
  const localMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
  if (localMatch) {
    ts = new Date(Number(localMatch[1]), Number(localMatch[2]) - 1, Number(localMatch[3])).getTime();
  } else {
    ts = Date.parse(date);
  }
  if (Number.isNaN(ts)) return '';
  const d = new Date(ts);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  if (sameDay) return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const diff = (today.getTime() - d.getTime()) / 86_400_000;
  if (diff < 7) return d.toLocaleDateString([], { weekday: 'short' });
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function codexSessionLabel(title?: string | null, sessionId?: string | null): string {
  const t = (title ?? '').trim();
  if (t) return t;
  const sid = (sessionId ?? '').trim().replace(/^session-/, '');
  if (sid) return `Session ${sid.slice(0, 10)}`;
  return 'Session';
}

function avatarInitial(account: KimAccount): string {
  return (account.display_name?.trim()[0] || 'A').toUpperCase();
}

export function RevampSidebar({
  account,
  appVersion,
  kimSessions,
  activeSessionId,
  collapsed,
  onToggle,
  onNewChat,
  onSelectSession,
  onOpenSettings,
  activeTab,
  onTabChange,
  showSessionMeta = true,
  activeProjectPath,
  onSelectProject,
  onRemoveProject,
  onNewChatInProject,
  onAddProject,
  onAccountChange,
  sessionRefreshNonce,
  theme,
  onCycleTheme,
  onOpenConnectors,
}: Props) {
  const [searchQuery, setSearchQuery] = useState('');
  const filteredSessions = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return kimSessions;
    return kimSessions.filter((s) => {
      const title = (s.title ?? '').toLowerCase();
      const id = (s.session_id ?? '').toLowerCase();
      return title.includes(q) || id.includes(q);
    });
  }, [kimSessions, searchQuery]);
  const grouped = useMemo(() => groupByDate(filteredSessions), [filteredSessions]);
  const [codexProjects, setCodexProjects] = useState<CodexProject[]>([]);
  const projectPaths = useMemo(() => account.code_projects ?? [], [account.code_projects]);
  const [openProjects, setOpenProjects] = useState<string[]>([]);
  const [acctMenuOpen, setAcctMenuOpen] = useState(false);
  const acctMenuRef = useRef<HTMLDivElement>(null);
  const sidebarRef = useRef<HTMLDivElement>(null);

  const [sidebarWidth, setSidebarWidth] = useState<number>(() => {
    try {
      const raw = localStorage.getItem('kim-revamp-sidebar-width');
      const v = raw ? parseInt(raw, 10) : NaN;
      if (Number.isFinite(v)) return Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, v));
    } catch {}
    return SIDEBAR_DEFAULT_WIDTH;
  });
  const [resizing, setResizing] = useState(false);
  const resizingRef = useRef(false);
  const resizeLeftRef = useRef(0);
  useEffect(() => {
    if (!resizing) return;
    function onMove(e: PointerEvent) {
      if (!resizingRef.current) return;
      const raw = e.clientX - resizeLeftRef.current;
      const next = Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, raw));
      setSidebarWidth(next);
    }
    function onUp() {
      resizingRef.current = false;
      setResizing(false);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    }
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    window.addEventListener('pointercancel', onUp);
    return () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      window.removeEventListener('pointercancel', onUp);
      // L13: unmounting mid-drag left document.body stuck in col-resize +
      // userSelect:none for the rest of the app session.
      if (resizingRef.current) {
        resizingRef.current = false;
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
      }
    };
  }, [resizing]);
  useEffect(() => {
    try { localStorage.setItem('kim-revamp-sidebar-width', String(sidebarWidth)); } catch {}
  }, [sidebarWidth]);
  function startResize(e: ReactPointerEvent<HTMLDivElement>) {
    e.preventDefault();
    resizingRef.current = true;
    resizeLeftRef.current = sidebarRef.current?.getBoundingClientRect().left ?? 0;
    setResizing(true);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  }

  const [projectMenu, setProjectMenu] = useState<{ x: number; y: number; path: string } | null>(null);
  const projectMenuRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!projectMenu) return;
    function onDocClick(e: MouseEvent) {
      if (projectMenuRef.current && !projectMenuRef.current.contains(e.target as Node)) {
        setProjectMenu(null);
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setProjectMenu(null);
    }
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [projectMenu]);

  useEffect(() => {
    if (!activeProjectPath) return;
    setOpenProjects(prev => (prev.includes(activeProjectPath) ? prev : [...prev, activeProjectPath]));
  }, [activeProjectPath]);

  useEffect(() => {
    if (!acctMenuOpen) return;
    function onDocClick(e: MouseEvent) {
      if (acctMenuRef.current && !acctMenuRef.current.contains(e.target as Node)) {
        setAcctMenuOpen(false);
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setAcctMenuOpen(false);
    }
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [acctMenuOpen]);

  function openSettings(pane?: SettingsPane) {
    setAcctMenuOpen(false);
    onOpenSettings(pane);
  }

  useEffect(() => {
    if (activeTab !== 'code') return;
    if (projectPaths.length === 0) {
      setCodexProjects([]);
      return;
    }
    // H8: cancelled-flag guard so a slow response from a PREVIOUS
    // projectPaths/nonce can't land after a newer one and overwrite it.
    // On error, keep the last good list (don't blank to "No projects yet")
    // and surface the failure.
    let cancelled = false;
    invoke<CodexProject[]>('list_codex_projects', { projectPaths })
      .then((p) => { if (!cancelled) setCodexProjects(p); })
      .catch((err) => {
        if (!cancelled) toast(`Could not load Code projects: ${String(err)}`, 'error', 4000);
      });
    return () => { cancelled = true; };
  }, [activeTab, projectPaths, sessionRefreshNonce]);

  async function handleAddProject() {
    if (onAddProject) {
      onAddProject();
      return;
    }
    try {
      const selected = await openDialog({ directory: true, multiple: false, title: 'Pick a Code project' });
      if (typeof selected !== 'string' || !selected) return;
      if (projectPaths.includes(selected)) return;
      const next = [...projectPaths, selected];
      if (onAccountChange) {
        await onAccountChange({ ...account, code_projects: next });
      }
    } catch (err) {
      // M12: a cancelled dialog returns null (handled above) — reaching here
      // means the picker or save_account really failed; tell the user instead
      // of silently showing nothing after they picked a folder.
      toast(`Could not add the project: ${String(err)}`, 'error', 4000);
    }
  }

  async function handleRemoveProject(path: string) {
    const ok = await confirm(
      'Remove this project from the sidebar? This will not delete the folder or any files.',
      { title: 'Remove project?', kind: 'warning' },
    );
    if (!ok) return;

    const next = projectPaths.filter(p => p !== path);
    try {
      if (onAccountChange) {
        await onAccountChange({ ...account, code_projects: next });
      }
      onRemoveProject?.(path);
    } catch (err) {
      // M12: previously a void-invoked unguarded await — the removal silently
      // didn't persist and the rejection was unhandled.
      toast(`Could not remove the project: ${String(err)}`, 'error', 4000);
    }
  }

  if (collapsed) {
    return (
      <div
        style={{
          width: 56,
          flexShrink: 0,
          background: 'var(--kim-sidebar-gradient)',
          borderRight: '1px solid var(--kim-border)',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          padding: '58px 8px 14px',
          gap: 12,
        }}
      >
        <button type="button" className="kr-icon-btn" onClick={onToggle} aria-label="Expand sidebar">
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
            <path d="M3 3v8M6 7l-3-3m0 6l3-3M9 3v8" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
          </svg>
        </button>
        <button
          type="button"
          aria-label="New chat"
          onClick={onNewChat}
          style={{
            width: 36,
            height: 36,
            borderRadius: 10,
            background: 'var(--kim-accent)',
            color: 'var(--kim-on-accent)',
            border: 0,
            cursor: 'pointer',
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          <svg width="14" height="14" viewBox="0 0 12 12" fill="none">
            <path d="M6 1v10M1 6h10" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          </svg>
        </button>
        <div style={{ flex: 1 }} />
        <button type="button" className="kr-icon-btn" onClick={() => onOpenSettings()} aria-label="Settings">
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
            <circle cx="7" cy="7" r="2" stroke="currentColor" strokeWidth="1.2" />
            <path
              d="M7 1v1.5m0 9V13M1 7h1.5m9 0H13M2.5 2.5l1 1m7 7l1 1m-9 0l1-1m7-7l1-1"
              stroke="currentColor"
              strokeWidth="1.2"
              strokeLinecap="round"
            />
          </svg>
        </button>
      </div>
    );
  }

  return (
    <div
      ref={sidebarRef}
      style={{
        width: sidebarWidth,
        flexShrink: 0,
        background: 'var(--kim-sidebar-gradient)',
        borderRight: '1px solid var(--kim-border)',
        display: 'flex',
        flexDirection: 'column',
        padding: '58px 14px 16px',
        position: 'relative',
      }}
    >
      {/* Logo + collapse */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: 14,
          paddingLeft: 4,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <div
            style={{
              width: 32,
              height: 32,
              borderRadius: 9,
              background: 'linear-gradient(135deg, var(--kim-accent) 0%, #c98968 100%)',
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontWeight: 700,
              color: 'var(--kim-on-accent)',
              fontSize: 18,
            }}
          >
            K
          </div>
          <span className="kr-kbd" style={{ fontSize: 10 }}>
            v{appVersion}
          </span>
        </div>
        <button type="button" className="kr-icon-btn" onClick={onToggle} aria-label="Collapse sidebar">
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
            <path d="M3 3v8M6 7l-3-3m0 6l3-3M9 3v8" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
          </svg>
        </button>
      </div>

      {/* Resize handle */}
      <div
        className={`kr-sidebar__resize-handle${resizing ? ' kr-sidebar__resize-handle--active' : ''}`}
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize sidebar"
        onPointerDown={startResize}
        onDoubleClick={() => setSidebarWidth(SIDEBAR_DEFAULT_WIDTH)}
        title="Drag to resize · double-click to reset"
      />

      {/* New chat */}
      <button
        type="button"
        onClick={onNewChat}
        style={{
          background: 'var(--kim-accent)',
          color: 'var(--kim-on-accent)',
          border: 0,
          borderRadius: 10,
          padding: '10px 14px',
          fontWeight: 600,
          fontSize: 13.5,
          cursor: 'pointer',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 8,
          marginBottom: 12,
        }}
      >
        <svg width="13" height="13" viewBox="0 0 12 12" fill="none">
          <path d="M6 1v10M1 6h10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
        New chat
      </button>

      {/* Chat | Code segmented control */}
      <div className="kr-seg" style={{ width: '100%', marginBottom: 14 }}>
        <button
          type="button"
          className={activeTab === 'chat' ? 'kr-on' : ''}
          onClick={() => onTabChange('chat')}
          aria-pressed={activeTab === 'chat'}
          style={{ flex: 1 }}
        >
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
              <path
                d="M2 3.5h8a1 1 0 011 1V8a1 1 0 01-1 1H5l-2.5 2v-2H2a1 1 0 01-1-1V4.5a1 1 0 011-1z"
                stroke="currentColor"
                strokeWidth="1.2"
              />
            </svg>
            Chat
          </span>
        </button>
        <button
          type="button"
          className={activeTab === 'code' ? 'kr-on' : ''}
          onClick={() => onTabChange('code')}
          aria-pressed={activeTab === 'code'}
          style={{ flex: 1 }}
        >
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
              <path
                d="M4 3l-3 3 3 3m4-6l3 3-3 3"
                stroke="currentColor"
                strokeWidth="1.3"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            Code
          </span>
        </button>
      </div>

      {/* Search */}
      <label
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '8px 12px',
          marginBottom: 10,
          background: 'var(--kim-bg-2)',
          border: '1px solid var(--kim-border)',
          borderRadius: 9,
          cursor: 'text',
        }}
      >
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none" style={{ color: 'var(--kim-text-3)', flexShrink: 0 }} aria-hidden="true">
          <circle cx="5" cy="5" r="3.5" stroke="currentColor" strokeWidth="1.2" />
          <path d="M8 8l2.5 2.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
        </svg>
        <input
          type="search"
          placeholder="Search sessions"
          aria-label="Search sessions"
          value={searchQuery}
          onChange={(e) => { setSearchQuery(e.target.value); }}
          style={{
            background: 'none',
            border: 'none',
            outline: 'none',
            color: 'var(--kim-text-3)',
            fontSize: 12.5,
            flex: 1,
            minWidth: 0,
            fontFamily: 'inherit',
          }}
        />
      </label>

      {/* Projects (Code tab only) */}
      {activeTab === 'code' && (
        <div
          className="kr-stream"
          style={{
            flex: 1,
            overflow: 'auto',
            paddingLeft: 8,
            display: 'flex',
            flexDirection: 'column',
            gap: 10,
            marginBottom: 10,
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '6px 8px' }}>
            <span className="kr-eyebrow">projects</span>
            <button type="button" className="kr-icon-btn" style={{ width: 22, height: 22 }} onClick={() => void handleAddProject()} aria-label="Add project">
              <svg width="11" height="11" viewBox="0 0 12 12" fill="none">
                <path d="M6 1v10M1 6h10" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
              </svg>
            </button>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
            {codexProjects.length === 0 ? (
              <div
                style={{
                  fontSize: 12,
                  color: 'var(--kim-text-3)',
                  padding: '10px 8px',
                  textAlign: 'center',
                  border: '1px dashed var(--kim-border)',
                  borderRadius: 8,
                }}
              >
                No projects yet
              </div>
            ) : (
              codexProjects.map((p) => {
                const open = openProjects.includes(p.path);
                return (
                  <div key={p.path} style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                    <div
                      className="kr-row-hover"
                      onClick={() => {
                        onSelectProject?.(p.path);
                        setOpenProjects(prev => (prev.includes(p.path) ? prev.filter(x => x !== p.path) : [...prev, p.path]));
                      }}
                      onContextMenu={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        setProjectMenu({ x: e.clientX, y: e.clientY, path: p.path });
                      }}
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 8,
                        padding: '6px 8px',
                        borderRadius: 7,
                        fontSize: 13,
                        color: 'var(--kim-text-2)',
                        background: activeProjectPath === p.path ? 'var(--kim-surface)' : 'transparent',
                        cursor: 'pointer',
                      }}
                    >
                      <svg
                        width="9"
                        height="9"
                        viewBox="0 0 10 10"
                        fill="none"
                        style={{ transform: open ? 'rotate(90deg)' : 'none', color: 'var(--kim-text-3)' }}
                      >
                        <path
                          d="M3.5 2.5L6 5L3.5 7.5"
                          stroke="currentColor"
                          strokeWidth="1.3"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                      <svg width="13" height="13" viewBox="0 0 14 14" fill="none" style={{ color: 'var(--kim-accent)' }}>
                        <path
                          d="M1.5 4a1 1 0 011-1h3l1 1.5h5a1 1 0 011 1v5a1 1 0 01-1 1h-9a1 1 0 01-1-1V4z"
                          stroke="currentColor"
                          strokeWidth="1.2"
                        />
                      </svg>
                      <span
                        style={{
                          flex: 1,
                          color: activeProjectPath === p.path ? 'var(--kim-text)' : 'var(--kim-text-2)',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                        }}
                        title={p.path}
                      >
                        {p.name}
                      </span>
                      <span
                        style={{
                          fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
                          fontSize: 10,
                          color: 'var(--kim-accent)',
                          border: '1px solid var(--kim-accent-line)',
                          padding: '1px 5px',
                          borderRadius: 4,
                          flexShrink: 0,
                        }}
                      >
                        {p.current_branch}
                      </span>
                      <button
                        type="button"
                        className="kr-icon-btn"
                        aria-label={`New chat in ${p.name}`}
                        title={`New chat in ${p.name}`}
                        onClick={(e) => {
                          e.stopPropagation();
                          onNewChatInProject?.(p.path);
                        }}
                        style={{
                          width: 22,
                          height: 22,
                          flexShrink: 0,
                          color: 'var(--kim-text-3)',
                        }}
                      >
                        <svg width="11" height="11" viewBox="0 0 12 12" fill="none">
                          <path d="M6 1v10M1 6h10" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
                        </svg>
                      </button>
                    </div>

                    {open && (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 1, marginLeft: 14 }}>
                        {p.branches.flatMap(b => b.sessions).length === 0 ? (
                          <div style={{ fontSize: 12, color: 'var(--kim-text-4)', padding: '6px 8px' }}>No sessions yet</div>
                        ) : (
                          (() => {
                            // M13: apply the sidebar search to Code-tab sessions too
                            // (the search box renders on both tabs but only filtered chats).
                            const q = searchQuery.trim().toLowerCase();
                            const flat = p.branches.flatMap(b => b.sessions).filter((s) => {
                              if (!q) return true;
                              return (
                                (s.title ?? '').toLowerCase().includes(q) ||
                                (s.summary ?? '').toLowerCase().includes(q) ||
                                s.session_id.toLowerCase().includes(q)
                              );
                            });
                            const updatedAtById = new Map(flat.map(s => [s.session_id, s.updated_at] as const));
                            const infos: SessionInfo[] = flat.map((s) => {
                              // L13: the contract is a YYYY-MM-DD bucket — a full
                              // ISO timestamp fallback broke the <bucket>/<id>.jsonl
                              // lookup, and an empty date made session_key "codex::id".
                              const dateBucket = s.date || new Date().toISOString().slice(0, 10);
                              return {
                                session_id: s.session_id,
                                session_type: 'codex',
                                // For Codex sessions, `date` must be the YYYY-MM-DD bucket so the
                                // message loader can locate <bucket>/<id>.jsonl on disk.
                                date: dateBucket,
                                message_count: s.message_count,
                                has_summary: !!s.summary,
                                summary: s.summary ?? undefined,
                                title: s.title,
                                session_key: `codex:${dateBucket}:${s.session_id}`,
                                project_path: p.path,
                              };
                            });
                            infos.sort((a, b) => {
                              const aTs = Date.parse(updatedAtById.get(a.session_id) || a.date);
                              const bTs = Date.parse(updatedAtById.get(b.session_id) || b.date);
                              return bTs - aTs;
                            });
                            const groups = groupByDate(infos);
                            return groups.map((grp) => (
                              <div key={`${p.path}:${grp.label}`}>
                                <div style={{ padding: '6px 8px 4px' }}>
                                  <span className="kr-eyebrow">{grp.label}</span>
                                </div>
                                <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
                                  {grp.items.map((info) => {
                                    const isActive = activeSessionId === sessionKey(info);
                                    const title = codexSessionLabel(info.title, info.session_id);
                                    return (
                                      <div
                                        key={sessionKey(info)}
                                        className="kr-row-hover"
                                        onClick={(e) => {
                                          e.stopPropagation();
                                          onSelectSession(info);
                                        }}
                                        style={{
                                          display: 'flex',
                                          alignItems: 'center',
                                          gap: 8,
                                          padding: '6px 10px',
                                          borderRadius: 7,
                                          cursor: 'pointer',
                                          background: isActive ? 'var(--kim-surface)' : 'transparent',
                                          borderLeft: isActive ? '2px solid var(--kim-accent)' : '2px solid transparent',
                                          marginLeft: -2,
                                        }}
                                      >
                                        <span
                                          style={{
                                            fontSize: 12.5,
                                            color: isActive ? 'var(--kim-text)' : 'var(--kim-text-2)',
                                            flex: 1,
                                            minWidth: 0,
                                            overflow: 'hidden',
                                            textOverflow: 'ellipsis',
                                            whiteSpace: 'nowrap',
                                          }}
                                          title={info.summary ?? info.session_id}
                                        >
                                          {title}
                                        </span>
                                        <span
                                          style={{
                                            fontSize: 10.5,
                                            color: 'var(--kim-text-4)',
                                            fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
                                            flexShrink: 0,
                                          }}
                                        >
                                          {formatTime(updatedAtById.get(info.session_id) || info.date)}
                                        </span>
                                      </div>
                                    );
                                  })}
                                </div>
                              </div>
                            ));
                          })()
                        )}
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </div>
      )}

      {projectMenu &&
        createPortal(
          <div
            ref={projectMenuRef}
            className="kim-context-menu"
            style={{ top: projectMenu.y, left: projectMenu.x }}
            onMouseDown={(e) => e.stopPropagation()}
            role="menu"
          >
            <button
              type="button"
              className="kim-context-menu__item"
              onClick={() => {
                const path = projectMenu.path;
                setProjectMenu(null);
                void invoke('open_in_finder', { path });
              }}
              role="menuitem"
            >
              Open in Finder
            </button>
            <button
              type="button"
              className="kim-context-menu__item"
              onClick={() => {
                const path = projectMenu.path;
                setProjectMenu(null);
                void handleRemoveProject(path);
              }}
              role="menuitem"
            >
              Remove from sidebar
            </button>
          </div>,
          document.body,
        )}

      {/* Session groups (Chat tab) */}
      {activeTab === 'chat' &&
        (grouped.length > 0 ? (
          <div
            className="kr-stream"
            style={{ flex: 1, overflow: 'auto', paddingLeft: 8, display: 'flex', flexDirection: 'column', gap: 14 }}
          >
            {grouped.map((grp) => (
              <div key={grp.label}>
                <div style={{ padding: '4px 4px 6px' }}>
                  <span className="kr-eyebrow">{grp.label}</span>
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
                  {grp.items.map((s) => {
                    const isActive = activeSessionId === sessionKey(s);
                    const title = s.title?.trim() || s.session_id;
                    return (
                      <button
                        key={sessionKey(s)}
                        type="button"
                        className="kr-row-hover"
                        onClick={() => onSelectSession(s)}
                        aria-current={isActive ? 'true' : undefined}
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          gap: 8,
                          padding: '7px 10px',
                          borderRadius: 8,
                          cursor: 'pointer',
                          background: isActive ? 'var(--kim-surface)' : 'transparent',
                          borderTop: 'none',
                          borderRight: 'none',
                          borderBottom: 'none',
                          borderLeft: isActive ? '2px solid var(--kim-accent)' : '2px solid transparent',
                          marginLeft: -2,
                          width: 'calc(100% + 2px)',
                          color: 'inherit',
                          font: 'inherit',
                          textAlign: 'left',
                        }}
                      >
                        <span
                          style={{
                            fontSize: 13,
                            color: isActive ? 'var(--kim-text)' : 'var(--kim-text-2)',
                            flex: 1,
                            minWidth: 0,
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                          }}
                        >
                          {title}
                        </span>
                        {showSessionMeta && (
                          <span
                            style={{
                              fontSize: 10.5,
                              color: 'var(--kim-text-4)',
                              fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
                              flexShrink: 0,
                            }}
                          >
                            {formatTime(s.date)}
                          </span>
                        )}
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div style={{ flex: 1 }} />
        ))}

      {/* Account footer */}
      <div
        ref={acctMenuRef}
        style={{ marginTop: 'auto', paddingTop: 10, borderTop: '1px solid var(--kim-border)', position: 'relative' }}
      >
        <button
          type="button"
          className="kr-row-hover"
          onClick={() => setAcctMenuOpen((v) => !v)}
          aria-haspopup="menu"
          aria-expanded={acctMenuOpen}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            padding: '8px 10px',
            borderRadius: 8,
            cursor: 'pointer',
            width: '100%',
            background: acctMenuOpen ? 'var(--kim-surface)' : 'transparent',
            border: 0,
            color: 'inherit',
            font: 'inherit',
            textAlign: 'left',
          }}
        >
          <div
            style={{
              width: 28,
              height: 28,
              borderRadius: 999,
              background: 'linear-gradient(135deg, var(--kim-accent), #c98968)',
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: 'var(--kim-on-accent)',
              fontWeight: 600,
              fontSize: 13,
              overflow: 'hidden',
              flexShrink: 0,
            }}
          >
            {account.github_avatar_url ? (
              <img src={account.github_avatar_url} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
            ) : (
              avatarInitial(account)
            )}
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div
              style={{
                fontSize: 13,
                color: 'var(--kim-text)',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {account.display_name || 'User'}
            </div>
            <div
              style={{
                fontSize: 10.5,
                color: 'var(--kim-text-3)',
                fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
              }}
            >
              {account.github_username ? `github · ${account.github_username}` : 'local · ready'}
            </div>
          </div>
          <svg
            width="11"
            height="11"
            viewBox="0 0 12 12"
            fill="none"
            style={{
              color: 'var(--kim-text-3)',
              transform: acctMenuOpen ? 'rotate(180deg)' : 'none',
              transition: 'transform .15s',
              flexShrink: 0,
            }}
            aria-hidden="true"
          >
            <path d="M3 4.5L6 7.5L9 4.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>

        {acctMenuOpen && (
          <div
            role="menu"
            style={{
              position: 'absolute',
              bottom: 'calc(100% + 8px)',
              left: 10,
              right: 10,
              background: 'var(--kim-surface)',
              border: '1px solid var(--kim-border)',
              borderRadius: 12,
              padding: 6,
              boxShadow: '0 18px 40px rgba(0,0,0,0.5)',
              zIndex: 60,
            }}
          >
            <AcctMenuItem
              icon={
                <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                  <circle cx="7" cy="7" r="2" stroke="currentColor" strokeWidth="1.2" />
                  <path
                    d="M7 1v1.5m0 9V13M1 7h1.5m9 0H13M2.5 2.5l1 1m7 7l1 1m-9 0l1-1m7-7l1-1"
                    stroke="currentColor"
                    strokeWidth="1.2"
                    strokeLinecap="round"
                  />
                </svg>
              }
              label="Settings"
              kbd="⌘,"
              onClick={() => openSettings()}
            />
            <AcctMenuItem
              icon={
                <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                  <circle cx="7" cy="7" r="2.5" stroke="currentColor" strokeWidth="1.2" />
                  <path
                    d="M2 5l-1 1 1 1M12 5l1 1-1 1M5 12l1 1 1-1M5 2l1-1 1 1"
                    stroke="currentColor"
                    strokeWidth="1.2"
                    strokeLinecap="round"
                  />
                </svg>
              }
              label="Connectors"
              onClick={() => {
                setAcctMenuOpen(false);
                onOpenConnectors?.();
              }}
            />
            {onCycleTheme && (
              <AcctMenuItem
                icon={
                  theme === 'light' ? (
                    <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
                      <circle cx="8" cy="8" r="3" stroke="currentColor" strokeWidth="1.3" />
                      <path
                        d="M8 1.5v1.5M8 13v1.5M1.5 8H3M13 8h1.5M3.5 3.5L4.5 4.5M11.5 11.5l1 1M3.5 12.5l1-1M11.5 4.5l1-1"
                        stroke="currentColor"
                        strokeWidth="1.3"
                        strokeLinecap="round"
                      />
                    </svg>
                  ) : theme === 'dark' ? (
                    <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
                      <path
                        d="M13 10.5A5.5 5.5 0 117.5 4.5a4.5 4.5 0 005.5 6z"
                        stroke="currentColor"
                        strokeWidth="1.3"
                        strokeLinejoin="round"
                      />
                    </svg>
                  ) : (
                    <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
                      <rect x="2.5" y="3" width="11" height="8" rx="1" stroke="currentColor" strokeWidth="1.3" />
                      <path d="M6 13.5h4M8 11v2.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
                    </svg>
                  )
                }
                label={`Theme: ${theme === 'system' ? 'System' : theme === 'light' ? 'Light' : 'Dark'}`}
                onClick={() => {
                  onCycleTheme();
                }}
              />
            )}
            <div style={{ height: 1, background: 'var(--kim-border)', margin: '6px 4px' }} />
            <AcctMenuItem
              icon={
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
                  <path
                    d="M2 4.5a2 2 0 012-2h8a2 2 0 012 2V10a2 2 0 01-2 2H6l-3 2.5V12h-1a1 1 0 010-2V4.5z"
                    stroke="currentColor"
                    strokeWidth="1.3"
                    strokeLinejoin="round"
                  />
                </svg>
              }
              label="Send feedback"
              onClick={() => openSettings('feedback')}
            />
            <AcctMenuItem
              icon={
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
                  <circle cx="8" cy="8" r="6.5" stroke="currentColor" strokeWidth="1.3" />
                  <path d="M8 7v4M8 5v.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
                </svg>
              }
              label="About"
              hint={`v${appVersion}`}
              onClick={() => openSettings('about')}
            />
          </div>
        )}
      </div>
    </div>
  );
}

function AcctMenuItem({
  icon,
  label,
  hint,
  kbd,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  hint?: string;
  kbd?: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onClick}
      className="kr-row-hover"
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        width: '100%',
        padding: '8px 10px',
        borderRadius: 8,
        cursor: 'pointer',
        background: 'transparent',
        border: 0,
        color: 'var(--kim-text-2)',
        font: 'inherit',
        fontSize: 13,
        textAlign: 'left',
      }}
    >
      <span style={{ display: 'inline-flex', color: 'var(--kim-text-3)', flexShrink: 0 }}>{icon}</span>
      <span style={{ flex: 1, color: 'var(--kim-text)' }}>{label}</span>
      {hint && (
        <span
          style={{
            fontSize: 10.5,
            color: 'var(--kim-text-4)',
            fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
          }}
        >
          {hint}
        </span>
      )}
      {kbd && <span className="kr-kbd">{kbd}</span>}
    </button>
  );
}

import { memo, useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { createPortal } from 'react-dom';
import { invoke } from '@tauri-apps/api/core';
import { open as openDialog } from '@tauri-apps/plugin-dialog';
import { KimLogo } from './KimLogo';
import type { SessionInfo, KimAccount, CodexProject } from '../types';

const SIDEBAR_MIN_WIDTH = 200;
const SIDEBAR_MAX_WIDTH = 420;
const SIDEBAR_DEFAULT_WIDTH = 240;

interface Props {
  kimSessions: SessionInfo[];
  activeSessionId: string | null;
  onSelectSession: (session: SessionInfo) => void;
  onNewChat: () => void;
  collapsed: boolean;
  onToggle: () => void;
  onOpenSettings: () => void;
  loading: boolean;
  account: KimAccount;
  onAccountChange: (a: KimAccount) => Promise<void>;
  activeTab: 'chat' | 'code';
  onTabChange: (tab: 'chat' | 'code') => void;
  activeProjectPath: string | null;
  onSelectProject: (path: string) => void;
  onNewChatInProject: (path: string) => void;
  onRefreshSessions: () => void;
  sessionRefreshNonce: number;
  kimSessionsDir: string | null;
  codexSessionsDir: string | null;
  appVersion: string;
}

// ── Icons ──────────────────────────────────────────────────────────────────────

function ChatBubbleIcon() {
  return (
    <svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6l-4 3V5z" />
    </svg>
  );
}
function CodeBracketIcon() {
  return (
    <svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M7 7l-4 3 4 3M13 7l4 3-4 3M11 4l-2 12" />
    </svg>
  );
}
function ComposeIcon() {
  return (
    <svg viewBox="0 0 20 20" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M14.5 2.5a2.121 2.121 0 0 1 3 3L6 17l-4 1 1-4 11.5-11.5z" />
    </svg>
  );
}
function FolderIcon() {
  return (
    <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M1 4.5A1.5 1.5 0 0 1 2.5 3h3l1.5 2H13.5A1.5 1.5 0 0 1 15 6.5v6A1.5 1.5 0 0 1 13.5 14h-11A1.5 1.5 0 0 1 1 12.5v-8z" />
    </svg>
  );
}
function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg viewBox="0 0 16 16" width="10" height="10" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"
      style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform 0.12s ease', flexShrink: 0 }}>
      <path d="M5 3l5 5-5 5" />
    </svg>
  );
}
function SettingsIcon() {
  return (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 4.6 9a1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9c.2.6.8 1 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
    </svg>
  );
}
function PlusIcon() {
  return (
    <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <path d="M8 3v10M3 8h10" />
    </svg>
  );
}
function XIcon() {
  return (
    <svg viewBox="0 0 16 16" width="10" height="10" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <path d="M4 4l8 8M12 4l-8 8" />
    </svg>
  );
}

// ── Session item ───────────────────────────────────────────────────────────────

function sessionKey(session: SessionInfo): string {
  return session.session_key ?? `${session.session_type}:${session.date}:${session.session_id}`;
}

function SessionItem({ session, active, onClick, editMode, selected, onToggleSelect, pinned, onTogglePin, onRequestDelete }: {
  session: SessionInfo; active: boolean; onClick: () => void;
  editMode?: boolean; selected?: boolean; onToggleSelect?: () => void;
  pinned?: boolean;
  onTogglePin?: () => void;
  onRequestDelete?: () => void;
}) {
  const chatTitle = session.title?.trim() || session.session_id;
  let summaryText = session.summary || '';
  if (summaryText) {
    const match = summaryText.match(/^Task:.*?(?:\.\s*Result:\s*|\nResult:\s*)([\s\S]*)$/i);
    if (match) {
      summaryText = match[1].trim();
    }
  }

  const preview = summaryText
    ? summaryText.slice(0, 60) + (summaryText.length > 60 ? '…' : '')
    : `${session.message_count} message${session.message_count !== 1 ? 's' : ''}`;

  const [menuPos, setMenuPos] = useState<{ x: number; y: number } | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!menuPos) return;
    function close() { setMenuPos(null); }
    document.addEventListener('mousedown', close);
    document.addEventListener('keydown', close);
    return () => {
      document.removeEventListener('mousedown', close);
      document.removeEventListener('keydown', close);
    };
  }, [menuPos]);

  function handleContextMenu(e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    setMenuPos({ x: e.clientX, y: e.clientY });
  }

  if (editMode) {
    return (
      <div className={`kim-session-item kim-session-item--edit${selected ? ' kim-session-item--selected' : ''}`} onClick={onToggleSelect} style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 8, paddingRight: 8 }}>
        <input type="checkbox" checked={selected} onChange={() => {}} style={{ cursor: 'pointer' }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="kim-session-item__title">{chatTitle}</div>
          <div className="kim-session-item__preview">{preview}</div>
        </div>
      </div>
    );
  }

  return (
    <>
      <button
        onClick={onClick}
        onContextMenu={handleContextMenu}
        title={session.summary ?? chatTitle}
        className={`kim-session-item${active ? ' kim-session-item--active' : ''}${pinned ? ' kim-session-item--pinned' : ''}`}
      >
        <div className="kim-session-item__title">
          {pinned && (
            <svg viewBox="0 0 16 16" width="10" height="10" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="kim-session-item__pin-icon">
              <path d="M8 1.5l2 4.5 4.5.5-3.5 3 1 4.5L8 11.5 4 14l1-4.5-3.5-3 4.5-.5 2-4.5z" />
            </svg>
          )}
          {chatTitle}
        </div>
        <div className="kim-session-item__preview">{preview}</div>
      </button>
      {menuPos && createPortal(
        <div
          ref={menuRef}
          className="kim-context-menu"
          style={{ top: menuPos.y, left: menuPos.x }}
          onMouseDown={e => e.stopPropagation()}
          role="menu"
        >
          <button
            type="button"
            className="kim-context-menu__item"
            onClick={() => { onTogglePin?.(); setMenuPos(null); }}
            role="menuitem"
          >
            {pinned ? 'Unpin' : 'Pin'}
          </button>
          <button
            type="button"
            className="kim-context-menu__item kim-context-menu__item--danger"
            onClick={() => { onRequestDelete?.(); setMenuPos(null); }}
            role="menuitem"
          >
            Delete
          </button>
        </div>,
        document.body,
      )}
    </>
  );
}

// ── Codex session item ────────────────────────────────────────────────────────

function CodexSessionItem({ session, onSelectSession }: { session: { session_id: string; message_count: number; summary?: string | null; date?: string }, onSelectSession: (s: SessionInfo) => void }) {
  const preview = session.summary
    ? session.summary.slice(0, 55) + (session.summary.length > 55 ? '…' : '')
    : `${session.message_count} message${session.message_count !== 1 ? 's' : ''}`;
  return (
    <button className="kim-session-item" title={session.summary ?? session.session_id} onClick={() => onSelectSession({ session_id: session.session_id, session_key: `codex:${session.date}:${session.session_id}`, session_type: 'codex', message_count: session.message_count, has_summary: !!session.summary, summary: session.summary ?? undefined, date: session.date || new Date().toISOString() })}>
      <div className="kim-session-item__title">{session.session_id.slice(0, 18)}</div>
      <div className="kim-session-item__preview">{preview}</div>
    </button>
  );
}

// ── Project tree in Code tab ───────────────────────────────────────────────────

function CodexProjectTree({ project, onRemove, isActive, onSelect, onNewChat, onSelectSession }: {
  project: CodexProject;
  onRemove: (path: string) => void;
  isActive: boolean;
  onSelect: () => void;
  onNewChat: () => void;
  onSelectSession: (s: SessionInfo) => void;
}) {
  const [open, setOpen] = useState(isActive);
  const totalSessions = project.branches.reduce((n, b) => n + b.sessions.length, 0);

  return (
    <div className={`kim-project-item${isActive ? ' kim-project-item--active' : ''}`}>
      <div className="kim-project-item__header" onClick={() => { onSelect(); setOpen(true); }} style={{ cursor: 'pointer' }}>
        <button className="kim-project-item__toggle" onClick={(e) => { e.stopPropagation(); setOpen(o => !o); }}>
          <ChevronIcon open={open} />
        </button>
        <span className="kim-project-item__icon"><FolderIcon /></span>
        <span className="kim-project-item__name" title={project.path}>{project.name}</span>
        <span className="kim-project-item__branch-pill">{project.current_branch}</span>
        <button
          className="kim-project-item__new-chat"
          onClick={(e) => { e.stopPropagation(); onNewChat(); }}
          title={`New chat in ${project.name}`}
          aria-label={`New chat in ${project.name}`}
        >
          <PlusIcon />
        </button>
        <button
          className="kim-project-item__remove"
          onClick={(e) => { e.stopPropagation(); onRemove(project.path); }}
          title="Remove project"
          aria-label="Remove project"
        >
          <XIcon />
        </button>
      </div>

      {open && (
        <div className="kim-project-item__sessions">
          {totalSessions === 0 ? (
            <div className="kim-empty-section" style={{ paddingLeft: 28 }}>
              No Codex sessions yet.
            </div>
          ) : (
            project.branches.map(branch => (
              <div key={branch.name}>
                {project.branches.length > 1 && (
                  <div className="kim-branch-label">{branch.name}</div>
                )}
                {branch.sessions.map(s => (
                  <div key={s.session_id} style={{ paddingLeft: 12 }}>
                    <CodexSessionItem session={s} onSelectSession={(session) => onSelectSession({ ...session, project_path: project.path })} />
                  </div>
                ))}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}

// ── Add project form ───────────────────────────────────────────────────────────

/** Opens the native folder picker and immediately adds the selected path. */
async function pickAndAddProject(onAdd: (path: string) => Promise<void>, setErr: (e: string) => void, setAdding: (b: boolean) => void) {
  try {
    const selected = await openDialog({
      directory: true,
      multiple: false,
      title: 'Select project folder',
    });
    if (typeof selected === 'string' && selected) {
      setAdding(true);
      setErr('');
      try {
        await onAdd(selected);
      } catch (e) {
        setErr(String(e));
      } finally {
        setAdding(false);
      }
    }
  } catch (e) {
    setErr(String(e));
  }
}

// ── Main Sidebar ───────────────────────────────────────────────────────────────

function SidebarImpl({
  kimSessions, activeSessionId,
  onSelectSession, onNewChat,
  collapsed, onToggle, onOpenSettings, loading,
  account, onAccountChange, activeTab, onTabChange,
  activeProjectPath, onSelectProject, onNewChatInProject, onRefreshSessions,
  sessionRefreshNonce, kimSessionsDir, codexSessionsDir, appVersion,
}: Props) {
  const [kimExpanded, setKimExpanded] = useState(true);
  const [codexProjects, setCodexProjects] = useState<CodexProject[]>([]);
  const [projectsLoading, setProjectsLoading] = useState(false);
  const [projectsAdding, setProjectsAdding] = useState(false);
  const [projectsErr, setProjectsErr] = useState('');

  const [editMode, setEditMode] = useState(false);
  const [selectedSessions, setSelectedSessions] = useState<Set<string>>(new Set());
  const [deleteConfirmStep, setDeleteConfirmStep] = useState<0 | 1 | 2>(0); // 0=hidden, 1=first confirm, 2=final confirm
  const [deleting, setDeleting] = useState(false);
  const [pinnedKeys, setPinnedKeys] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem('kim-pinned-sessions');
      return raw ? new Set(JSON.parse(raw) as string[]) : new Set();
    } catch { return new Set(); }
  });
  const togglePinned = useCallback((key: string) => {
    setPinnedKeys(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      try { localStorage.setItem('kim-pinned-sessions', JSON.stringify([...next])); } catch {}
      return next;
    });
  }, []);
  const requestSingleDelete = useCallback((key: string) => {
    setSelectedSessions(new Set([key]));
    setDeleteConfirmStep(1);
  }, []);
  const [accountMenuOpen, setAccountMenuOpen] = useState(false);
  const accountMenuRef = useRef<HTMLDivElement | null>(null);

  // Resizable sidebar width (persisted, only applied when not collapsed).
  const [sidebarWidth, setSidebarWidth] = useState<number>(() => {
    try {
      const raw = localStorage.getItem('kim-sidebar-width');
      const v = raw ? parseInt(raw, 10) : NaN;
      if (Number.isFinite(v)) return Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, v));
    } catch {}
    return SIDEBAR_DEFAULT_WIDTH;
  });
  const [resizing, setResizing] = useState(false);
  const resizingRef = useRef(false);
  useEffect(() => {
    if (!resizing) return;
    function onMove(e: MouseEvent) {
      if (!resizingRef.current) return;
      const next = Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, e.clientX));
      setSidebarWidth(next);
    }
    function onUp() {
      resizingRef.current = false;
      setResizing(false);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    }
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    return () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };
  }, [resizing]);
  useEffect(() => {
    try { localStorage.setItem('kim-sidebar-width', String(sidebarWidth)); } catch {}
  }, [sidebarWidth]);

  function startResize(e: React.MouseEvent) {
    if (collapsed) return;
    e.preventDefault();
    resizingRef.current = true;
    setResizing(true);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  }

  useEffect(() => {
    if (!accountMenuOpen) return;
    function onDocClick(e: MouseEvent) {
      if (!accountMenuRef.current) return;
      if (!accountMenuRef.current.contains(e.target as Node)) {
        setAccountMenuOpen(false);
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setAccountMenuOpen(false);
    }
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [accountMenuOpen]);

  const projectPaths = useMemo(() => account.code_projects ?? [], [account.code_projects]);
  const projectPathsKey = useMemo(() => JSON.stringify(projectPaths), [projectPaths]);

  const loadProjects = useCallback(() => {
    if (projectPaths.length === 0) {
      setCodexProjects([]);
      return;
    }
    setProjectsLoading(true);
    invoke<CodexProject[]>('list_codex_projects', { projectPaths })
      .then(p => setCodexProjects(p))
      .catch(() => setCodexProjects([]))
      .finally(() => setProjectsLoading(false));
  }, [projectPaths, projectPathsKey]);

  useEffect(() => {
    if (activeTab === 'code') loadProjects();
  }, [activeTab, loadProjects, sessionRefreshNonce]);

  async function handleAddProject(path: string) {
    const newPaths = await invoke<string[]>('add_code_project', { path });
    await onAccountChange({ ...account, code_projects: newPaths });
  }

  function handlePickProject() {
    pickAndAddProject(handleAddProject, setProjectsErr, setProjectsAdding);
  }

  async function handleRemoveProject(path: string) {
    const newPaths = await invoke<string[]>('remove_code_project', { path });
    await onAccountChange({ ...account, code_projects: newPaths });
    setCodexProjects(prev => prev.filter(p => p.path !== path));
  }

  const initials = account.display_name
    .split(' ').map((w: string) => w[0]).join('').slice(0, 2).toUpperCase();

  const sidebarStyle: React.CSSProperties = collapsed
    ? {}
    : { width: sidebarWidth, minWidth: sidebarWidth };

  const sortedKimSessions = useMemo(() => {
    return [...kimSessions].sort((a, b) => {
      const ap = pinnedKeys.has(sessionKey(a)) ? 1 : 0;
      const bp = pinnedKeys.has(sessionKey(b)) ? 1 : 0;
      return bp - ap;
    });
  }, [kimSessions, pinnedKeys]);

  const toggleSelectedSession = useCallback((key: string) => {
    setSelectedSessions(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  return (
    <>
    <aside
      className={`kim-sidebar${collapsed ? ' kim-sidebar--collapsed' : ''}${resizing ? ' kim-sidebar--resizing' : ''}`}
      data-tauri-drag-region
      style={sidebarStyle}
    >
      {/* Traffic-lights spacer + brand mark (no "Kim" wordmark, just the K logo + version) */}
      <div className="kim-sidebar__brand kim-no-drag">
        {!collapsed && (
          <div className="kim-sidebar__brand-mark">
            <KimLogo layout="icon" size={20} />
            <span className="kim-sidebar__version">v{appVersion}</span>
          </div>
        )}
      </div>

      {/* Top controls */}
      <div className="kim-sidebar__top">
        <button
          onClick={onToggle}
          title={collapsed ? 'Expand (Cmd+B)' : 'Collapse (Cmd+B)'}
          className="kim-sidebar__toggle"
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            {collapsed ? <path d="M6 3l5 5-5 5" /> : <path d="M10 3l-5 5 5 5" />}
          </svg>
        </button>
        {!collapsed && (
          <button onClick={onNewChat} className="kim-sidebar__new-chat" title="New chat (Cmd+N)">
            <ComposeIcon />
            <span>New chat</span>
          </button>
        )}
      </div>

      {/* Tab bar */}
      {!collapsed && (
        <div className="kim-sidebar__tabs">
          <div className="kim-tab-bar">
            <button
              className={`kim-tab${activeTab === 'chat' ? ' kim-tab--active' : ''}`}
              onClick={() => onTabChange('chat')}
            >
              <ChatBubbleIcon /><span>Chat</span>
            </button>
            <button
              className={`kim-tab${activeTab === 'code' ? ' kim-tab--active' : ''}`}
              onClick={() => onTabChange('code')}
            >
              <CodeBracketIcon /><span>Code</span>
            </button>
          </div>
        </div>
      )}

      {/* Scrollable content */}
      {!collapsed && (
        <div className="kim-sidebar__scroll">
          {loading ? (
            <div className="kim-sidebar__loading">
              {[32, 28, 36, 28].map((h, i) => (
                <div key={i} className="kim-skeleton" style={{ height: h, marginBottom: 4 }} />
              ))}
            </div>
          ) : activeTab === 'chat' ? (
            // ── Chat tab ──
            <div>
              <div className="kim-section-header" style={{ display: 'flex', alignItems: 'center' }}>
                <button style={{ flex: 1, display: 'flex', alignItems: 'center', background: 'none', border: 'none', color: 'inherit', padding: 0, font: 'inherit', cursor: 'pointer' }} onClick={() => setKimExpanded(v => !v)}>
                  <ChevronIcon open={kimExpanded} />
                  <span className="kim-section-header__label">Kim</span>
                  <span className="kim-section-header__count">{kimSessions.length}</span>
                </button>
                {kimSessions.length > 0 && (
                  <button 
                    className={editMode ? 'kim-action-btn' : 'kim-action-btn'}
                    style={{ 
                      fontSize: 11, 
                      padding: '3px 10px',
                      background: editMode ? 'var(--accent)' : 'var(--bg-card)',
                      color: editMode ? '#fff' : 'var(--text-muted)',
                      border: `1px solid ${editMode ? 'var(--accent)' : 'var(--border)'}`,
                      borderRadius: 6,
                    }} 
                    onClick={() => { setEditMode(!editMode); setSelectedSessions(new Set()); }}
                  >
                    {editMode ? 'Done' : 'Edit'}
                  </button>
                )}
              </div>
              
              {editMode && selectedSessions.size > 0 && (
                <div style={{ padding: '4px 8px 8px', display: 'flex', gap: 6 }}>
                  <button 
                    className="kim-action-btn kim-action-btn--danger"
                    style={{ flex: 1, fontSize: 12, padding: '7px 12px' }}
                    onClick={() => setDeleteConfirmStep(1)}
                  >
                    <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M2 4h12M5.333 4V2.667a1.333 1.333 0 0 1 1.334-1.334h2.666a1.333 1.333 0 0 1 1.334 1.334V4M13 4v9.333a1.333 1.333 0 0 1-1.333 1.334H4.333A1.333 1.333 0 0 1 3 13.333V4" />
                    </svg>
                    Delete {selectedSessions.size} chat{selectedSessions.size > 1 ? 's' : ''}
                  </button>
                </div>
              )}
              {kimExpanded && (
                <div style={{ marginTop: 1 }}>
                  {kimSessions.length === 0 ? (
                    <div className="kim-empty-section">No sessions yet</div>
                  ) : (
                    sortedKimSessions
                      .map(s => {
                        const key = sessionKey(s);
                        return (
                          <SessionItem
                            key={key}
                            session={s}
                            active={key === activeSessionId}
                            onClick={() => onSelectSession(s)}
                            editMode={editMode}
                            selected={selectedSessions.has(key)}
                            onToggleSelect={() => toggleSelectedSession(key)}
                            pinned={pinnedKeys.has(key)}
                            onTogglePin={() => togglePinned(key)}
                            onRequestDelete={() => requestSingleDelete(key)}
                          />
                        );
                      })
                  )}
                </div>
              )}
            </div>
          ) : (
            // ── Code tab ──
            <div>
              <div className="kim-code-tab-header">
                <span className="kim-section-header__label" style={{ flex: 1 }}>Projects</span>
                <button
                  className="kim-code-tab-add-btn"
                  onClick={handlePickProject}
                  disabled={projectsAdding}
                  title="Add project folder"
                >
                  {projectsAdding ? '…' : <PlusIcon />}
                </button>
              </div>

              {projectsErr && (
                <div className="kim-add-project-error" style={{ padding: '0 8px 6px' }}>{projectsErr}</div>
              )}

              {projectPaths.length === 0 ? (
                <div className="kim-empty-section" style={{ marginTop: 8 }}>
                  <div style={{ marginBottom: 8 }}>No projects added yet.</div>
                  <button className="kim-link-btn" onClick={handlePickProject} disabled={projectsAdding}>
                    {projectsAdding ? 'Selecting…' : 'Choose a project folder'}
                  </button>
                </div>
              ) : projectsLoading ? (
                <div className="kim-sidebar__loading">
                  <div className="kim-skeleton" style={{ height: 36 }} />
                  <div className="kim-skeleton" style={{ height: 36 }} />
                </div>
              ) : (
                // Merge: show all added project paths, use loaded project data where available
                projectPaths.map(path => {
                  const loaded = codexProjects.find(p => p.path === path);
                  if (loaded) {
                    return (
                      <CodexProjectTree
                        key={path}
                        project={loaded}
                        onRemove={handleRemoveProject}
                        isActive={activeProjectPath === path}
                        onSelect={() => onSelectProject(path)}
                        onNewChat={() => onNewChatInProject(path)}
                        onSelectSession={onSelectSession}
                      />
                    );
                  }
                  // Project path exists but no .codex/ sessions yet
                  const name = path.split('/').filter(Boolean).pop() ?? path;
                  return (
                    <div key={path} className={`kim-project-item${activeProjectPath === path ? ' kim-project-item--active' : ''}`}>
                      <div className="kim-project-item__header" onClick={() => onSelectProject(path)} style={{ cursor: 'pointer' }}>
                        <button className="kim-project-item__toggle" style={{ flex: 1, cursor: 'default' }}>
                          <span className="kim-project-item__icon"><FolderIcon /></span>
                          <span className="kim-project-item__name" title={path}>{name}</span>
                        </button>
                        <button
                          className="kim-project-item__new-chat"
                          onClick={(e) => { e.stopPropagation(); onNewChatInProject(path); }}
                          title={`New chat in ${name}`}
                          aria-label={`New chat in ${name}`}
                        >
                          <PlusIcon />
                        </button>
                        <button
                          className="kim-project-item__remove"
                          onClick={(e) => { e.stopPropagation(); handleRemoveProject(path); }}
                          title="Remove project"
                        >
                          <XIcon />
                        </button>
                      </div>
                      <div className="kim-empty-section" style={{ paddingLeft: 28, fontSize: 11 }}>
                        No Codex sessions yet
                      </div>
                    </div>
                  );
                })
              )}
            </div>
          )}
        </div>
      )}

      {/* Resize handle (only when not collapsed) */}
      {!collapsed && (
        <div
          className="kim-sidebar__resize-handle kim-no-drag"
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize sidebar"
          onMouseDown={startResize}
          onDoubleClick={() => setSidebarWidth(SIDEBAR_DEFAULT_WIDTH)}
          title="Drag to resize · double-click to reset"
        />
      )}

      {/* Bottom: account dropdown */}
      <div className="kim-sidebar__bottom" ref={accountMenuRef}>
        {accountMenuOpen && !collapsed && (
          <div className="kim-account-menu" role="menu">
            <div className="kim-account-menu__email">
              {account.github_username ? `@${account.github_username}` : account.display_name}
            </div>
            <button
              type="button"
              className="kim-account-menu__item"
              onClick={() => { setAccountMenuOpen(false); onOpenSettings(); }}
              role="menuitem"
            >
              <SettingsIcon />
              <span>Settings</span>
              <span className="kim-account-menu__kbd">⌘,</span>
            </button>
            <button
              type="button"
              className="kim-account-menu__item"
              onClick={() => setAccountMenuOpen(false)}
              role="menuitem"
              disabled
              title="Coming soon"
            >
              <span>Get help</span>
            </button>
            <div className="kim-account-menu__sep" />
            <button
              type="button"
              className="kim-account-menu__item"
              onClick={() => setAccountMenuOpen(false)}
              role="menuitem"
              disabled
              title="Coming soon"
            >
              <span>Upgrade plan</span>
            </button>
            <button
              type="button"
              className="kim-account-menu__item"
              onClick={() => setAccountMenuOpen(false)}
              role="menuitem"
              disabled
              title="Coming soon"
            >
              <span>Learn more</span>
            </button>
            <div className="kim-account-menu__sep" />
            <button
              type="button"
              className="kim-account-menu__item kim-account-menu__item--danger"
              onClick={async () => {
                setAccountMenuOpen(false);
                await onAccountChange({ ...account, display_name: '', github_username: undefined, github_avatar_url: undefined } as KimAccount);
              }}
              role="menuitem"
            >
              <span>Log out</span>
            </button>
          </div>
        )}
        <button
          type="button"
          className={`kim-account-trigger${accountMenuOpen ? ' is-open' : ''} kim-no-drag`}
          onClick={() => setAccountMenuOpen(v => !v)}
          aria-haspopup="menu"
          aria-expanded={accountMenuOpen}
          title={collapsed ? account.display_name : undefined}
        >
          <div className="kim-account-trigger__avatar">
            {account.github_avatar_url
              ? <img src={account.github_avatar_url} alt={account.display_name} />
              : initials}
          </div>
          {!collapsed && (
            <>
              <div className="kim-account-trigger__info">
                <span className="kim-account-trigger__name">{account.display_name || 'Account'}</span>
              </div>
              <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M4 6l4 4 4-4" />
              </svg>
            </>
          )}
        </button>
      </div>
    </aside>

      {/* ── Delete confirmation modal ── */}
      {deleteConfirmStep > 0 && (
        <div className="kim-confirm-overlay" onClick={() => setDeleteConfirmStep(0)}>
          <div className="kim-confirm-dialog" onClick={e => e.stopPropagation()}>
            <div className="kim-confirm-dialog__icon">
              <svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="var(--danger)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
              </svg>
            </div>
            {deleteConfirmStep === 1 ? (
              <>
                <div className="kim-confirm-dialog__title">Delete {selectedSessions.size} chat{selectedSessions.size > 1 ? 's' : ''}?</div>
                <div className="kim-confirm-dialog__text">This action cannot be undone. The selected chat{selectedSessions.size > 1 ? 's' : ''} will be permanently removed.</div>
                <div className="kim-confirm-dialog__actions">
                  <button className="kim-confirm-dialog__btn kim-confirm-dialog__btn--cancel" onClick={() => setDeleteConfirmStep(0)}>Cancel</button>
                  <button className="kim-confirm-dialog__btn kim-confirm-dialog__btn--danger" onClick={() => setDeleteConfirmStep(2)}>Yes, delete</button>
                </div>
              </>
            ) : (
              <>
                <div className="kim-confirm-dialog__title">Are you absolutely sure?</div>
                <div className="kim-confirm-dialog__text">This will <strong>permanently delete {selectedSessions.size} chat{selectedSessions.size > 1 ? 's' : ''}</strong>. There is no way to recover them.</div>
                <div className="kim-confirm-dialog__actions">
                  <button className="kim-confirm-dialog__btn kim-confirm-dialog__btn--cancel" onClick={() => setDeleteConfirmStep(0)}>No, keep them</button>
                  <button 
                    className="kim-confirm-dialog__btn kim-confirm-dialog__btn--danger" 
                    disabled={deleting}
                    onClick={async () => {
                      setDeleting(true);
                      try {
                        const selectedSessionIds = kimSessions
                          .filter(session => selectedSessions.has(sessionKey(session)))
                          .map(session => session.session_id);
                        await invoke('delete_sessions', { 
                          sessionIds: selectedSessionIds,
                          kimDir: kimSessionsDir,
                          codexDir: codexSessionsDir,
                        });
                        setEditMode(false);
                        setSelectedSessions(new Set());
                        setDeleteConfirmStep(0);
                        onRefreshSessions();
                      } catch (e) {
                        alert(`Failed to delete: ${e}`);
                      } finally {
                        setDeleting(false);
                      }
                    }}
                  >{deleting ? 'Deleting…' : 'Delete permanently'}</button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </>
  );
}

function areSidebarPropsEqual(prev: Props, next: Props): boolean {
  return (
    prev.kimSessions === next.kimSessions &&
    prev.activeSessionId === next.activeSessionId &&
    prev.collapsed === next.collapsed &&
    prev.loading === next.loading &&
    prev.account === next.account &&
    prev.activeTab === next.activeTab &&
    prev.activeProjectPath === next.activeProjectPath &&
    prev.sessionRefreshNonce === next.sessionRefreshNonce &&
    prev.kimSessionsDir === next.kimSessionsDir &&
    prev.codexSessionsDir === next.codexSessionsDir &&
    prev.appVersion === next.appVersion
  );
}

export const Sidebar = memo(SidebarImpl, areSidebarPropsEqual);

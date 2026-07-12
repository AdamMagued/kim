import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import { createPortal } from 'react-dom';
import { invoke } from '@tauri-apps/api/core';
import { open as openDialog } from '@tauri-apps/plugin-dialog';
import type { SessionInfo, Settings, KimAccount, PermissionMode } from '../types';
import type { PendingTask } from './chat/types';
import { toast } from './Toast';
import { ErrorBoundary } from './ErrorBoundary';
import { ConnectorsPanel } from './kim-ui';
import {
  friendlyError,
  makeConversationId,
  normalizeBrowserSite,
  browserSiteFromProvider,
  providerLabel,
  BROWSER_PROVIDER_URLS,
} from './chat/utils';
export { collapseMessages, groupCodexMessages, friendlyError } from './chat/utils';
import { useChatStream } from '../hooks/useChatStream';
import { useSessionScroll } from '../hooks/useSessionScroll';
import { useSessionLoader } from '../hooks/useSessionLoader';
import { useBrowserRestore } from '../hooks/useBrowserRestore';
import { useTaskRunner } from '../hooks/useTaskRunner';
import { useCodexTurn } from '../hooks/useCodexTurn';
import { useOsNotifications } from '../hooks/useOsNotifications';
import { StreamRenderer } from './chat/StreamRenderer';
import { WelcomeScreen } from './chat/WelcomeScreen';
import { ChatComposer } from './chat/ChatComposer';
import { CONNECTORS } from './chat/connectors';

// V-audit #7: fallback for the chat/message-rendering ErrorBoundary below.
// Scoped to "this conversation failed to render" — the sidebar, topbar, and
// session navigation stay alive around it (they're outside this boundary),
// so a render crash in one conversation's messages doesn't blank the app.
function ChatRenderErrorFallback({
  message,
  onReset,
  onNewChat,
}: {
  message: string;
  onReset: () => void;
  onNewChat?: () => void;
}) {
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        flex: 1,
        gap: 12,
        padding: 24,
        textAlign: 'center',
        color: 'var(--kim-text)',
      }}
    >
      <svg
        width="32"
        height="32"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        style={{ color: 'var(--kim-text-3)' }}
      >
        <circle cx="12" cy="12" r="10" />
        <path d="M12 8v4M12 16h.01" />
      </svg>
      <h3 style={{ margin: 0, fontSize: 14, fontWeight: 600 }}>
        This conversation failed to render
      </h3>
      {message && (
        <p style={{ margin: 0, fontSize: 12.5, opacity: 0.6, maxWidth: 360, wordBreak: 'break-word' }}>
          {message}
        </p>
      )}
      <div style={{ display: 'flex', gap: 8, marginTop: 4 }}>
        <button
          type="button"
          onClick={onReset}
          style={{
            padding: '7px 16px',
            borderRadius: 6,
            border: '1px solid var(--kim-border, rgba(255,255,255,0.12))',
            background: 'var(--kim-surface, rgba(255,255,255,0.06))',
            color: 'inherit',
            fontSize: 13,
            cursor: 'pointer',
          }}
        >
          Try again
        </button>
        {onNewChat && (
          <button
            type="button"
            onClick={onNewChat}
            style={{
              padding: '7px 16px',
              borderRadius: 6,
              border: '1px solid var(--kim-accent-line)',
              background: 'transparent',
              color: 'var(--kim-accent)',
              fontSize: 13,
              cursor: 'pointer',
            }}
          >
            Start a new chat
          </button>
        )}
      </div>
    </div>
  );
}

interface Props {
  session: SessionInfo | null;
  /** RUN-IDENTITY: identity of the single globally-active run (from App), so a
   *  ChatView remounted mid-run can re-attach to its own session's run. */
  activeRunSessionId?: string | null;
  activeRunId?: string | null;
  /** F-F-3: wall-clock start of the active run (from App), so a mid-run
   *  re-attach restores the original elapsed instead of resetting to 0. */
  activeRunStartedAt?: number | null;
  newChatMode: boolean;
  settings: Settings;
  onSettingsChange?: (next: Settings) => void;
  onTaskDone: (sessionId?: string, completedSession?: SessionInfo) => void;
  account: KimAccount;
  onAccountChange: (account: KimAccount) => Promise<void>;
  onOpenSettings?: (pane?: 'ai') => void;
  activeTab: 'chat' | 'code';
  activeProjectPath?: string | null;
  reloadSessions: () => void;
  onNewChat?: () => void;
  onNewCodeSession?: () => void;
  onSelectProject?: (path: string) => void;
  recentSessions?: SessionInfo[];
  onSelectSession?: (s: SessionInfo) => void;
  /** Mutable ref written by App; ChatView stores its openConnectors callback here
   *  so App can trigger the panel without a global CustomEvent bus. */
  openConnectorsRef?: { current: (() => void) | null };
  /** V-audit #1: queued-follow-up store lifted to App.tsx so it survives a
   *  ChatView remount (New Chat / session switch) instead of being silently
   *  dropped. See useTaskRunner's queueKeyRef for how a bucket is chosen. */
  queuedTasksStore?: Record<string, PendingTask[]>;
  setQueuedTasksStore?: Dispatch<SetStateAction<Record<string, PendingTask[]>>>;
}

export function ChatView({
  session,
  activeRunSessionId,
  activeRunId,
  activeRunStartedAt,
  newChatMode,
  settings,
  onSettingsChange,
  onTaskDone,
  account,
  onAccountChange,
  activeTab,
  activeProjectPath,
  onOpenSettings,
  onNewChat,
  onNewCodeSession,
  recentSessions,
  onSelectSession,
  onSelectProject,
  openConnectorsRef,
  queuedTasksStore,
  setQueuedTasksStore,
}: Props) {
  const [localProvider, setLocalProvider] = useState<string | null>(null);
  const [messageReloadNonce, setMessageReloadNonce] = useState(0);
  const [taskInput, setTaskInput] = useState('');
  const [connectorsOpen, setConnectorsOpen] = useState(false);
  // Per-session permission mode — starts from persistent settings but can be toggled per session.
  const [permissionMode, setPermissionMode] = useState<PermissionMode>(
    () => settings.permission_mode ?? 'full_auto'
  );
  // B15: keep the per-session toggle in sync when the persistent default changes
  // in Settings (the state initializer only ran once, so it silently diverged).
  useEffect(() => {
    setPermissionMode(settings.permission_mode ?? 'full_auto');
  }, [settings.permission_mode]);
  const [connectorsClosing, setConnectorsClosing] = useState(false);
  const [browserProvider, setBrowserProvider] = useState('claude');
  // L3: use the shared UUID helper — the old Math.random().toString(36)
  // one-liner could produce very short, collision-prone ids that end up as
  // resumeSessionId.
  const [conversationId] = useState(() => makeConversationId());

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const connectorsCloseTimerRef = useRef<number | null>(null);

  const resolveProvider = useCallback((): string => {
    return localProvider ?? settings.provider;
  }, [localProvider, settings.provider]);

  const browserCommandArgs = useCallback(
    (targetSession?: SessionInfo | null, overrideSessionId?: string | null) => {
      const s = targetSession ?? session;
      const sessionType = s?.session_type ?? (activeTab === 'code' ? 'codex' : 'kim');
      return {
        sessionId: overrideSessionId ?? s?.session_id ?? conversationId,
        sessionDate: s?.date ?? null,
        sessionType,
        kimDir: settings.kim_sessions_dir || null,
        codexDir:
          sessionType === 'codex' && s?.project_path
            ? `${s.project_path}/.codex/sessions`
            : settings.codex_sessions_dir || null,
      };
    },
    [session, activeTab, conversationId, settings.kim_sessions_dir, settings.codex_sessions_dir]
  );

  const commitCurrentBrowserUrl = useCallback(
    async (preferredSite?: string | null, targetSession?: SessionInfo | null, overrideSessionId?: string | null) => {
      const site =
        normalizeBrowserSite(preferredSite) ??
        browserSiteFromProvider(stream.currentTaskRef.current?.provider) ??
        browserSiteFromProvider(resolveProvider());
      const args = browserCommandArgs(targetSession, overrideSessionId);
      if (!args.sessionId) return;
      try {
        await invoke('session_browser_url_commit', {
          ...args,
          preferredSite: site,
        });
      } catch {
        // Non-fatal
      }
    },
    [browserCommandArgs, resolveProvider]
  );

  // OS notifications for task completion/failure
  useOsNotifications();

  // Hook into Tauri events and streaming state management
  // Codex app-server transport UX (native approvals / plan / output / diff).
  const codexTurn = useCodexTurn();
  const stream = useChatStream({
    session,
    settings,
    onTaskDone,
    commitCurrentBrowserUrl,
    setMessageReloadNonce,
    conversationId,
    activeRunSessionId,
    activeRunId,
    activeRunStartedAt,
  });

  const {
    messages,
    loadingMessages,
    newestMsgIdx,
    codexRuns,
    loadError,
  } = useSessionLoader({
    session,
    settings,
    messageReloadNonce,
    stream,
  });

  const {
    restoreBrowserForSession,
    restoreSeqRef,
    lastRestoreKeyRef,
  } = useBrowserRestore({
    session,
    newChatMode,
    settings,
    localProvider,
    browserProvider,
    resolveProvider,
    browserCommandArgs,
  });

  const scroll = useSessionScroll({
    newChatMode,
    activityLength: stream.activity.length,
    messagesLength: messages.length,
  });

  // H1: opening a (different) session should still land on the latest message
  // even if the user had paused auto-follow in the previous one.
  const setAutoFollowOutput = scroll.setAutoFollowOutput;
  useEffect(() => {
    setAutoFollowOutput(true);
  }, [session?.session_id, newChatMode, setAutoFollowOutput]);

  const handleCancel = async () => {
    if (!stream.isRunning || stream.cancelling) return;
    stream.setCancelling(true);
    stream.cancelFlagRef.current = true;
    try {
      await invoke('cancel_task');
      // M10: clear the feed only once the backend accepted the cancel — a
      // rejected invoke used to leave a still-running run with an empty feed.
      stream.clearActivityNow();
    } catch (err) {
      stream.setCancelling(false);
      stream.cancelFlagRef.current = false;
      stream.setTaskError(friendlyError(String(err)));
    }
  };

  // L5: stable merged-settings object — an inline spread re-created it every
  // render, which re-created runPendingTask and re-ran the queue-drain effect
  // on each of the ~20/sec activity ticks.
  const runnerSettings = useMemo(
    () => ({ ...settings, permission_mode: permissionMode }),
    [settings, permissionMode]
  );

  const {
    queuedTasks,
    handleSubmit,
    handleRetryLast,
    handleSteer,
  } = useTaskRunner({
    session,
    settings: runnerSettings,
    activeTab,
    activeProjectPath,
    conversationId,
    onTaskDone,
    resolveProvider,
    browserCommandArgs,
    stream,
    scroll,
    queuedTasksStore,
    setQueuedTasksStore,
  });

  // ── Reset state when entering a new chat ─────────────────────────────────────
  useEffect(() => {
    if (newChatMode) {
      stream.clearActivityNow();
      stream.setRunHistory([]);
      stream.setTaskError(null);
      stream.setTokenStats(null);
      stream.setContextState(null);
      stream.setElapsed(0);
      stream.hasSentMessageRef.current = false;
      // B6: also clear live bubbles, failure/rate-limit cards, pending approval,
      // and the retry target so stale UI can't bleed into a fresh chat.
      stream.setLiveHistory([]);
      stream.setRunFailure(null);
      stream.setRateLimitedState(null);
      stream.setHitlApprovalStatus(null);
      stream.setLastFailedTask(null);
    }
  }, [newChatMode, stream.clearActivityNow]);

  // ── Focus on new chat ───────────────────────────────────────────────────────
  useEffect(() => {
    if (newChatMode) {
      const t = setTimeout(() => textareaRef.current?.focus(), 50);
      return () => clearTimeout(t);
    }
  }, [newChatMode]);

  const handleProviderChange = useCallback(
    async (val: string) => {
      if (stream.isRunning) {
        toast('Finish or cancel the current task before switching providers.', 'warning', 3500);
        return;
      }
      const previousSite = browserSiteFromProvider(resolveProvider());

      if (val.startsWith('browser:')) {
        const sub = normalizeBrowserSite(val.split(':')[1]);
        if (!sub) return;
        setLocalProvider(`browser:${sub}`);
        setBrowserProvider(sub);
        await commitCurrentBrowserUrl(previousSite);
        restoreSeqRef.current += 1;
        lastRestoreKeyRef.current = null;

        if (session) {
          try {
            await invoke('session_browser_meta_write', {
              ...browserCommandArgs(session),
              browserLastSite: sub,
              lastLlmProvider: `browser:${sub}`,
              site: null,
              url: null,
            });
          } catch {
            // Non-fatal
          }
          const seq = restoreSeqRef.current + 1;
          restoreSeqRef.current = seq;
          const result = await restoreBrowserForSession(session, sub);
          if (restoreSeqRef.current === seq && result) {
            if (result.restored) {
              toast(`Restored ${providerLabel('browser:' + sub)} for this session.`, 'info', 3000);
            } else if (result.reason === 'stored_url_rejected') {
              toast(
                result.message ?? 'Saved browser URL was invalid, so Kim opened a fresh provider page.',
                'warning',
                5000
              );
            }
          }
        } else {
          const newUrl = BROWSER_PROVIDER_URLS[sub];
          if (newUrl) {
            try {
              await invoke('open_browser_signin_window', { url: newUrl });
            } catch {
              // Non-fatal
            }
          }
        }
      } else {
        setLocalProvider(val);
        await commitCurrentBrowserUrl(previousSite);
        if (session) {
          try {
            await invoke('session_browser_meta_write', {
              ...browserCommandArgs(session),
              browserLastSite: null,
              lastLlmProvider: val,
              site: null,
              url: null,
            });
          } catch {
            // Non-fatal
          }
        }
      }
      if (onSettingsChange) {
        onSettingsChange({ ...settings, provider: val as Settings['provider'] });
      }
    },
    [
      session,
      settings,
      onSettingsChange,
      resolveProvider,
      commitCurrentBrowserUrl,
      browserCommandArgs,
      restoreBrowserForSession,
      stream.isRunning,
      browserProvider,
    ]
  );

  const handleOllamaModeChange = useCallback((mode: 'local' | 'cloud') => {
    if (!onSettingsChange || settings.ollama.mode === mode) return;
    onSettingsChange({
      ...settings,
      provider: 'ollama',
      ollama: {
        ...settings.ollama,
        mode,
      },
    });
  }, [onSettingsChange, settings]);

  const handleOllamaModelChange = useCallback((mode: 'local' | 'cloud', model: string) => {
    if (!onSettingsChange) return;
    onSettingsChange({
      ...settings,
      ollama: {
        ...settings.ollama,
        mode,
        local_model: mode === 'local' ? model : settings.ollama.local_model,
        cloud_model: mode === 'cloud' ? model : settings.ollama.cloud_model,
      },
    });
  }, [onSettingsChange, settings]);

  const openConnectors = useCallback(() => {
    if (connectorsCloseTimerRef.current !== null) {
      window.clearTimeout(connectorsCloseTimerRef.current);
      connectorsCloseTimerRef.current = null;
    }
    setConnectorsClosing(false);
    setConnectorsOpen(true);
  }, []);

  const closeConnectors = useCallback(() => {
    if (!connectorsOpen || connectorsClosing) return;
    setConnectorsClosing(true);
    connectorsCloseTimerRef.current = window.setTimeout(() => {
      setConnectorsOpen(false);
      setConnectorsClosing(false);
      connectorsCloseTimerRef.current = null;
    }, 260);
  }, [connectorsClosing, connectorsOpen]);

  // Register the stable openConnectors callback with the ref supplied by App so
  // it can open the panel without a global CustomEvent bus.
  useEffect(() => {
    if (!openConnectorsRef) return;
    openConnectorsRef.current = openConnectors;
    return () => { openConnectorsRef.current = null; };
  }, [openConnectors, openConnectorsRef]);

  const handlePickCodeProject = async (mode: 'create' | 'open') => {
    try {
      const selected = await openDialog({
        directory: true,
        multiple: false,
        canCreateDirectories: true,
        title: mode === 'create' ? 'Create a new project folder' : 'Open a project folder',
      });
      if (typeof selected !== 'string' || !selected) return;

      const projectPaths = account.code_projects ?? [];
      const updated = Array.from(new Set([selected, ...projectPaths]));
      await onAccountChange({ ...account, code_projects: updated });

      if (onSelectProject) {
        onSelectProject(selected);
      }
    } catch {
      toast('Could not open the folder picker.', 'error', 2500);
    }
  };

  const handleEditLiveMessage = (idx: number, newText: string) => {
    // B2: `idx` is the real liveHistory index (via collapsed srcIdx). Edit is
    // Claude-style: truncate the live exchange at this message and resend the
    // edited text as a new turn. (Was: a cosmetic in-place mutation that edited
    // the wrong message after retry-collapse and never reached the agent.)
    const trimmed = newText.trim();
    if (!trimmed) return;
    stream.setLiveHistory(prev => prev.slice(0, idx));
    handleSubmit(trimmed);
  };

  const renderComposer = (heroMode = false) => {
    return (
      <ChatComposer
        textareaRef={textareaRef}
        taskInput={taskInput}
        setTaskInput={setTaskInput}
        isRunning={stream.isRunning}
        cancelling={stream.cancelling}
        handleCancel={handleCancel}
        onSubmit={handleSubmit}
        onSteer={handleSteer}
        activeTab={activeTab}
        activeProjectPath={activeProjectPath}
        settings={settings}
        resolveProvider={resolveProvider}
        handleProviderChange={handleProviderChange}
        handleOllamaModeChange={handleOllamaModeChange}
        handleOllamaModelChange={handleOllamaModelChange}
        onOpenSettings={onOpenSettings}
        heroMode={heroMode}
      />
    );
  };

  const renderConnectorsChrome = () => {
    if (!connectorsOpen && !connectorsClosing) return null;
    return createPortal(
      <div
        className={`kim-connectors-backdrop ${
          connectorsClosing ? 'kim-connectors-backdrop--closing' : ''
        }`}
        onClick={closeConnectors}
      >
        <ConnectorsPanel
          connectors={CONNECTORS}
          onClose={closeConnectors}
        />
      </div>,
      document.body
    );
  };

  // ── Welcome Screen ──────────────────────────────────────────────────────────
  if (!newChatMode && !session) {
    return (
      <WelcomeScreen
        account={account}
        activeTab={activeTab}
        onNewChat={onNewChat}
        onNewCodeSession={onNewCodeSession}
        recentSessions={recentSessions}
        onSelectSession={onSelectSession}
        handlePickCodeProject={handlePickCodeProject}
        renderConnectorsChrome={renderConnectorsChrome}
      />
    );
  }

  // ── New Chat / Session Stream feed ──────────────────────────────────────────
  const { liveHistory, activity } = stream;
  const empty = !stream.hasSentMessageRef.current && messages.length === 0 && liveHistory.length === 0 && activity.length === 0;

  return (
    // V-audit #7: a render exception anywhere in the message-rendering subtree
    // (StreamRenderer, MessageBubble, etc.) used to bubble up to the single
    // app-wide boundary in main.tsx and blank the whole app — sidebar, topbar,
    // and session navigation included. This narrower boundary catches it here
    // instead, so only this conversation's content is replaced; the outer
    // boundary in main.tsx remains as the final backstop for anything above
    // ChatView. resetKey clears a stale crash automatically when the app
    // navigates to a different session/new-chat without remounting ChatView
    // (see handleTaskDone in App.tsx, which can update `session` in place).
    <ErrorBoundary
      resetKey={session?.session_id ?? (newChatMode ? 'new-chat' : null)}
      fallback={(message, reset) => (
        <ChatRenderErrorFallback message={message} onReset={reset} onNewChat={onNewChat} />
      )}
    >
      <StreamRenderer
        messages={messages}
        loadingMessages={loadingMessages}
        loadError={loadError}
        liveHistory={stream.liveHistory}
        runHistory={stream.runHistory}
        codexRuns={codexRuns}
        taskError={stream.taskError}
        hitlApprovalStatus={stream.hitlApprovalStatus}
        onHitlRespond={stream.hitlRespond}
        codexTurn={codexTurn}
        permissionMode={permissionMode}
        onPermissionModeChange={setPermissionMode}
        runFailure={stream.runFailure}
        rateLimitedState={stream.rateLimitedState}
        settings={settings}
        newChatMode={newChatMode}
        activity={stream.activity}
        isRunning={stream.isRunning}
        autoFollowOutput={scroll.autoFollowOutput}
        setAutoFollowOutput={scroll.setAutoFollowOutput}
        bottomRef={scroll.bottomRef}
        outputRef={scroll.outputRef}
        newestMsgIdx={newestMsgIdx}
        queuedTasks={queuedTasks}
        lastRunTask={stream.lastRunTaskRef.current}
        elapsed={stream.elapsed}
        handleRetryLast={handleRetryLast}
        handleEditLiveMessage={handleEditLiveMessage}
        empty={empty}
        renderComposer={renderComposer}
        renderConnectorsChrome={renderConnectorsChrome}
        account={account}
        activeTab={activeTab}
        activeProjectPath={activeProjectPath}
        textareaRef={textareaRef}
        setTaskInput={setTaskInput}
        resolveProvider={resolveProvider}
        tokenStats={stream.tokenStats}
        contextState={stream.contextState}
      />
    </ErrorBoundary>
  );
}

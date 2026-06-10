import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { invoke } from '@tauri-apps/api/core';
import { open as openDialog } from '@tauri-apps/plugin-dialog';
import type { SessionInfo, Settings, KimAccount } from '../types';
import { toast } from './Toast';
import { ConnectorsPanel } from './kim-ui';
import {
  friendlyError,
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
import { StreamRenderer } from './chat/StreamRenderer';
import { WelcomeScreen } from './chat/WelcomeScreen';
import { ChatComposer } from './chat/ChatComposer';
import { CONNECTORS } from './chat/connectors';

interface Props {
  session: SessionInfo | null;
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
}

export function ChatView({
  session,
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
}: Props) {
  const [localProvider, setLocalProvider] = useState<string | null>(null);
  const [messageReloadNonce, setMessageReloadNonce] = useState(0);
  const [taskInput, setTaskInput] = useState('');
  const [connectorsOpen, setConnectorsOpen] = useState(false);
  const [connectorsClosing, setConnectorsClosing] = useState(false);
  const [browserProvider, setBrowserProvider] = useState('claude');
  const [conversationId] = useState(() => Math.random().toString(36).substring(7));

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

  // Hook into Tauri events and streaming state management
  const stream = useChatStream({
    session,
    settings,
    onTaskDone,
    commitCurrentBrowserUrl,
    setMessageReloadNonce,
  });

  const {
    messages,
    loadingMessages,
    newestMsgIdx,
    codexRuns,
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

  const handleCancel = async () => {
    if (!stream.isRunning || stream.cancelling) return;
    stream.setCancelling(true);
    stream.clearActivityNow();
    stream.cancelFlagRef.current = true;
    try {
      await invoke('cancel_task');
    } catch (err) {
      stream.setCancelling(false);
      stream.cancelFlagRef.current = false;
      stream.setTaskError(friendlyError(String(err)));
    }
  };

  const {
    queuedTasks,
    interruptTask,
    handleSubmit,
    handleRetryLast,
  } = useTaskRunner({
    session,
    settings,
    activeTab,
    activeProjectPath,
    conversationId,
    onTaskDone,
    resolveProvider,
    browserCommandArgs,
    stream,
    scroll,
    handleCancel,
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
        onSettingsChange({ ...settings, provider: val as any });
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

  useEffect(() => {
    function onOpen() { openConnectors(); }
    window.addEventListener('kim-open-connectors', onOpen);
    return () => window.removeEventListener('kim-open-connectors', onOpen);
  }, [openConnectors]);

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
    stream.setLiveHistory(prev => {
      const next = [...prev];
      if (next[idx]) next[idx].content = newText;
      return next;
    });
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
    <StreamRenderer
      messages={messages}
      loadingMessages={loadingMessages}
      liveHistory={stream.liveHistory}
      runHistory={stream.runHistory}
      codexRuns={codexRuns}
      taskError={stream.taskError}
      hitlApprovalStatus={stream.hitlApprovalStatus}
      settings={settings}
      newChatMode={newChatMode}
      activity={stream.activity}
      isRunning={stream.isRunning}
      autoFollowOutput={scroll.autoFollowOutput}
      setAutoFollowOutput={scroll.setAutoFollowOutput}
      bottomRef={scroll.bottomRef}
      newestMsgIdx={newestMsgIdx}
      queuedTasks={queuedTasks}
      interruptTask={interruptTask}
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
    />
  );
}

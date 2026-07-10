import { useState, useCallback, useEffect, useRef } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import { invoke } from '@tauri-apps/api/core';
import type { SessionInfo, Settings } from '../types';
import type { PendingTask } from '../components/chat/types';
import { toast } from '../components/Toast';
import { browserSiteFromProvider, friendlyError } from '../components/chat/utils';
import type { useChatStream } from './useChatStream';
import type { useSessionScroll } from './useSessionScroll';

interface UseTaskRunnerProps {
  session: SessionInfo | null;
  settings: Settings;
  activeTab: 'chat' | 'code';
  activeProjectPath?: string | null;
  conversationId: string;
  onTaskDone: (sessionId?: string, completedSession?: SessionInfo) => void;
  resolveProvider: () => string;
  browserCommandArgs: (
    targetSession?: SessionInfo | null,
    overrideSessionId?: string | null
  ) => {
    sessionId: string;
    sessionDate: string | null;
    sessionType: string;
    kimDir: string | null;
    codexDir: string | null;
  };
  stream: ReturnType<typeof useChatStream>;
  scroll: ReturnType<typeof useSessionScroll>;
  // V-audit #1: queuedTasks used to live only in this hook's own useState — a
  // full ChatView remount (New Chat / session switch) destroyed it, silently
  // breaking the UI's own promise ("Kim will run it automatically next").
  // When the caller (App.tsx) lifts this store above the ChatView remount
  // boundary and passes it down, queued messages survive the remount instead
  // of vanishing. Optional so this hook still works standalone (tests, or any
  // future caller that doesn't need cross-mount survival) via a local store.
  queuedTasksStore?: Record<string, PendingTask[]>;
  setQueuedTasksStore?: Dispatch<SetStateAction<Record<string, PendingTask[]>>>;
}

let _taskCounter = 0;

// Stable empty-array reference so `store[key] ?? EMPTY_QUEUE` doesn't create a
// new array identity on every render when nothing is queued for this key —
// keeps the drain effect (which depends on `queuedTasks`) from re-running
// needlessly.
const EMPTY_QUEUE: PendingTask[] = [];

export function useTaskRunner({
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
  queuedTasksStore,
  setQueuedTasksStore,
}: UseTaskRunnerProps) {
  // V-audit #1: the bucket a queued message is filed under is frozen at THIS
  // mount's creation and never recomputed — deliberately NOT derived from
  // stream.runOwnerSessionIdRef or an activeRunSessionId prop, both of which
  // change over a run's lifetime (null at mount for a view that doesn't yet
  // own the run; cleared the instant the run's done handler fires) and would
  // make a freshly-mounted New Chat view transiently key off a foreign
  // session's run, or make the drain effect miss its own bucket right after
  // completion. `session?.session_id ?? conversationId` is exactly what
  // runPendingTask itself resolves the run's session to (see resolvedSessionId
  // below), so reads/writes/drain all agree on one bucket for this mount's
  // entire lifetime.
  const queueKeyRef = useRef<string>(session?.session_id ?? conversationId);

  const [localQueuedTasksStore, setLocalQueuedTasksStore] = useState<Record<string, PendingTask[]>>({});
  const store = queuedTasksStore ?? localQueuedTasksStore;
  const setStore = setQueuedTasksStore ?? setLocalQueuedTasksStore;

  const queuedTasks = store[queueKeyRef.current] ?? EMPTY_QUEUE;

  const setQueuedTasks = useCallback(
    (updater: PendingTask[] | ((prev: PendingTask[]) => PendingTask[])) => {
      setStore(prev => {
        const key = queueKeyRef.current;
        const prevList = prev[key] ?? EMPTY_QUEUE;
        const nextList = typeof updater === 'function'
          ? (updater as (p: PendingTask[]) => PendingTask[])(prevList)
          : updater;
        if (nextList === prevList) return prev;
        if (nextList.length === 0) {
          if (!(key in prev)) return prev;
          const next = { ...prev };
          delete next[key];
          return next;
        }
        return { ...prev, [key]: nextList };
      });
    },
    [setStore]
  );

  const makePendingTask = useCallback(
    (text: string, providerOverride?: string): PendingTask => {
      return {
        id: ++_taskCounter,
        text,
        provider: providerOverride ?? resolveProvider(),
      };
    },
    [resolveProvider]
  );

  const runPendingTask = useCallback(
    async (pending: PendingTask) => {
      stream.doneHandledRef.current = false;
      stream.cancelFlagRef.current = false;
      stream.needHelpFlagRef.current = false;
      stream.terminationReasonRef.current = null;
      stream.lastProviderErrorCodeRef.current = null;
      stream.answerReceivedThisRunRef.current = false;
      stream.recentActivityItemRef.current.clear();
      stream.hasSentMessageRef.current = true;
      stream.currentTaskRef.current = pending;
      stream.lastRunTaskRef.current = pending;
      stream.setIsRunning(true);
      stream.clearActivityNow();
      stream.setLiveHistory([]);
      stream.setTaskError(null);
      stream.setTokenStats(null);
      stream.setHitlApprovalStatus(null);
      stream.setRunFailure(null);
      stream.setRateLimitedState(null);
      stream.setCancelling(false);
      scroll.setAutoFollowOutput(true);

      const isCompactTask = ['__kim_compact_context__', '/compact', 'compact'].includes(
        pending.text.trim().toLowerCase()
      );

      stream.activityRef.current = [
        {
          id: ++stream.activityCounterRef.current,
          kind: 'status' as const,
          icon: '›',
          text: isCompactTask ? 'Compacting this chat…' : 'Kim is thinking…',
        },
      ];
      stream.setActivity(stream.activityRef.current);

      if (!isCompactTask) {
        stream.setLiveHistory(prev => [...prev, { role: 'user', content: pending.text }]);
      }

      try {
        const resolvedSessionId =
          stream.completedCodeSessionRef.current?.session_id ?? (session?.session_id || conversationId);
        // RUN-IDENTITY: capture the session this run belongs to at spawn. Rust
        // stamps the same id (KIM_SESSION_ID) onto every event; the done handler
        // files history under THIS owner even if the user has switched views.
        stream.runOwnerSessionIdRef.current = resolvedSessionId;
        const pendingBrowserSite = browserSiteFromProvider(pending.provider);
        const pendingProvider = pending.provider.trim().toLowerCase();
        if (pendingProvider === 'browser' || pendingProvider.startsWith('browser:')) {
          invoke('session_browser_meta_write', {
            ...browserCommandArgs(session, resolvedSessionId),
            browserLastSite: pendingBrowserSite ?? null,
            lastLlmProvider: pending.provider,
            site: null,
            url: null,
          }).catch(() => {});
        }

        if (pendingProvider === 'ollama') {
          const selectedModel =
            settings.ollama.mode === 'cloud' ? settings.ollama.cloud_model : settings.ollama.local_model;
          const status = await invoke<{
            installed: boolean;
            running: boolean;
            selected_mode: string;
            selected_model_available: boolean;
            cloud_connected: boolean;
            message: string;
            cloud_message?: string | null;
          }>('ollama_get_status', {
            baseUrl: settings.ollama.base_url || null,
            selectedModel: selectedModel || null,
            mode: settings.ollama.mode,
            contextLimitOverride: settings.ollama.context_limit_override ?? null,
          });
          if (!status.installed || !status.running) {
            throw new Error(status.message || 'Ollama is not available.');
          }
          if (!status.selected_model_available) {
            throw new Error(
              settings.ollama.mode === 'cloud'
                ? 'The selected Ollama cloud model is unavailable. Pull it in Settings → AI → Ollama or pick another model.'
                : 'The selected Ollama local model is not installed. Pull it in Settings → AI → Ollama or pick another model.'
            );
          }
          if (settings.ollama.mode === 'cloud' && !status.cloud_connected) {
            throw new Error(status.cloud_message || 'Sign in to Ollama to use cloud models.');
          }
        }

        await invoke('send_task', {
          task: pending.text,
          provider: pending.provider,
          projectRoot:
            activeTab === 'code' && activeProjectPath ? activeProjectPath : settings.project_root || null,
          resumeSessionId: resolvedSessionId,
          ollamaBaseUrl: settings.ollama.base_url || null,
          ollamaMode: settings.ollama.mode,
          ollamaLocalModel: settings.ollama.local_model || null,
          ollamaCloudModel: settings.ollama.cloud_model || null,
          ollamaContextLimitOverride: settings.ollama.context_limit_override ?? null,
          permissionMode: settings.permission_mode ?? 'full_auto',
          // D-C3: the "Context budget" setting was a no-op — send_task accepts
          // context_budget_tokens (forwarded as KIM_CONTEXT_BUDGET_TOKENS to the
          // context meter) but the frontend never passed it, so every run used
          // the 200k default. Wire the real value (0/falsy → backend default).
          contextBudgetTokens: settings.context_budget_tokens || null,
        });
      } catch (err) {
        if (!stream.doneHandledRef.current) {
          invoke('set_task_active_mode', { active: false }).catch(() => {});
          stream.setIsRunning(false);
          stream.setTaskError(friendlyError(String(err)));
          stream.setLastFailedTask(pending);
          const resolvedSessionId =
            stream.completedCodeSessionRef.current?.session_id ?? (session?.session_id || conversationId);
          onTaskDone(resolvedSessionId);
        }
      }
    },
    [session, settings, activeTab, activeProjectPath, conversationId, onTaskDone, browserCommandArgs, scroll.setAutoFollowOutput]
  );

  // B1: drain the queue when a run finishes. When isRunning transitions
  // true → false and tasks are queued, dequeue the head and run it. (Previously
  // queued tasks were appended but never executed — the queue UI lied.)
  // Fix: distinguish a user cancel from a natural completion. When cancelFlagRef
  // is set, the user explicitly hit Stop — discard the queue rather than
  // auto-running the next message, which contradicts the Stop intent.
  const prevRunningRef = useRef(stream.isRunning);
  useEffect(() => {
    const justFinished = prevRunningRef.current && !stream.isRunning;
    prevRunningRef.current = stream.isRunning;
    if (justFinished && queuedTasks.length > 0) {
      if (stream.cancelFlagRef.current) {
        // Task was cancelled by the user — clear the queue instead of running
        // queued messages. cancelFlagRef stays true until the next runPendingTask
        // call resets it, so this check is safe here.
        setQueuedTasks([]);
      } else {
        const [next, ...rest] = queuedTasks;
        setQueuedTasks(rest);
        void runPendingTask(next);
      }
    }
  }, [stream.isRunning, queuedTasks, runPendingTask]);

  const handleSubmit = (fullText: string) => {
    const task = makePendingTask(fullText);

    if (stream.isRunning) {
      // D-C1: the "Queue messages while Kim is working" toggle was a no-op —
      // messages were ALWAYS queued. Honor it: when queuing is disabled, don't
      // silently swallow the message. Tell the user their options (Steer folds
      // it into the running task; otherwise wait) instead of pretending it will
      // run next.
      if (settings.allow_message_queue === false) {
        toast(
          'Kim is still working. Turn on "Queue messages" in Settings to line this up, or use Steer to fold it into the current task.',
          'warning',
          5000
        );
        return;
      }
      const nextCount = queuedTasks.length + 1;
      setQueuedTasks(prev => [...prev, task]);
      toast(`Queued message #${nextCount}. Kim will run it automatically next.`, 'info', 3000);
      return;
    }

    void runPendingTask(task);
  };

  const handleRetryLast = () => {
    const failed = stream.lastFailedTask ?? stream.lastRunTaskRef.current;
    if (!failed) return;

    if (stream.isRunning) {
      // V-audit #2: handleSubmit already refuses to auto-queue when the user
      // has turned off "Queue messages while Kim is working" (D-C1 above) —
      // Retry silently queued anyway, bypassing that setting. Apply the same
      // guard here so Retry can't queue when Send wouldn't.
      if (settings.allow_message_queue === false) {
        toast(
          'Kim is still working. Turn on "Queue messages" in Settings to line this up, or use Steer to fold it into the current task.',
          'warning',
          5000
        );
        return;
      }
      setQueuedTasks(prev => [...prev, failed]);
      toast('Retry queued. It will run after the current task.', 'info', 3000);
      return;
    }
    void runPendingTask(failed);
  };

  // K3: mid-run steering — inject text into the running agent instead of queuing.
  const handleSteer = (text: string) => {
    const t = text.trim();
    if (!t) return;
    invoke('steer_task', { text: t })
      .then(() => toast('Steering noted — Kim will fold it in next step.', 'info', 2500))
      .catch(() => toast('Could not steer (no run active?).', 'warning', 3000));
  };

  return {
    queuedTasks,
    setQueuedTasks,
    makePendingTask,
    runPendingTask,
    handleSubmit,
    handleRetryLast,
    handleSteer,
  };
}

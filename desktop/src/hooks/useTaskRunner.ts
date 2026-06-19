import { useState, useCallback, useEffect, useRef } from 'react';
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
}

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
}: UseTaskRunnerProps) {
  const [queuedTasks, setQueuedTasks] = useState<PendingTask[]>([]);

  const makePendingTask = useCallback(
    (text: string, providerOverride?: string): PendingTask => {
      return {
        id: Date.now() + Math.floor(Math.random() * 1000),
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
  const prevRunningRef = useRef(stream.isRunning);
  useEffect(() => {
    const justFinished = prevRunningRef.current && !stream.isRunning;
    prevRunningRef.current = stream.isRunning;
    if (justFinished && queuedTasks.length > 0) {
      const [next, ...rest] = queuedTasks;
      setQueuedTasks(rest);
      void runPendingTask(next);
    }
  }, [stream.isRunning, queuedTasks, runPendingTask]);

  const handleSubmit = (fullText: string) => {
    const task = makePendingTask(fullText);

    if (stream.isRunning) {
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
      setQueuedTasks(prev => [...prev, failed]);
      toast('Retry queued. It will run after the current task.', 'info', 3000);
      return;
    }
    void runPendingTask(failed);
  };

  return {
    queuedTasks,
    setQueuedTasks,
    makePendingTask,
    runPendingTask,
    handleSubmit,
    handleRetryLast,
  };
}

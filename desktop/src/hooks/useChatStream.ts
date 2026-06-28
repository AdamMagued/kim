import { useState, useEffect, useRef, useCallback } from 'react';
import { listen } from '@tauri-apps/api/event';
import { invoke } from '@tauri-apps/api/core';
import type { ActivityItem, PendingTask, HitlApprovalStatus } from '../components/chat/types';
import type { SessionInfo, Settings } from '../types';
import { parseAgentLine, buildThinkingTrace } from '../components/chat/parsers';
import { parsePlanFromActivity, browserSiteFromProvider } from '../components/chat/utils';

const MAX_ACTIVITY_ITEMS = 300;

function providerErrorMessage(code: string | null): string | null {
  if (!code) return null;
  switch (code) {
    case 'auth':
      return 'Provider authentication failed. Check the selected provider sign-in or API key.';
    case 'rate_limit':
      return 'Provider rate limit reached. Try again after a short wait.';
    case 'server_error':
      return 'Provider server error. Try again in a moment.';
    case 'timeout':
      return 'Provider request timed out. Try again or switch providers.';
    case 'network':
      return 'Provider network error. Check your connection and try again.';
    case 'invalid_request':
      return 'Provider rejected the request. Try a shorter or simpler task.';
    default:
      return `Provider error: ${code}`;
  }
}

function terminationMessage(termination: string | null): string | null {
  if (!termination) return null;
  switch (termination) {
    case 'max_iterations':
      return 'Kim stopped after reaching the maximum iteration limit. Progress is saved — send "continue" to pick up where it left off.';
    case 'stuck':
      return 'Kim stopped because the screen stopped changing.';
    case 'provider_failed':
      return 'Kim stopped because the selected provider failed.';
    case 'conversational_loop':
      return 'Kim stopped because the model kept replying without taking action.';
    case 'need_help':
      return 'Kim needs your help to continue.';
    case 'cancelled':
      return null;
    default:
      return null;
  }
}

export interface UseChatStreamProps {
  session: SessionInfo | null;
  settings: Settings;
  onTaskDone: (sessionId?: string, completedSession?: SessionInfo) => void;
  commitCurrentBrowserUrl: (preferredSite?: string | null, targetSession?: SessionInfo | null, overrideSessionId?: string | null) => Promise<void>;
  setMessageReloadNonce: React.Dispatch<React.SetStateAction<number>>;
  conversationId?: string;
}

export function useChatStream({
  session,
  settings,
  onTaskDone,
  commitCurrentBrowserUrl,
  setMessageReloadNonce,
  conversationId,
}: UseChatStreamProps) {
  const [isRunning, setIsRunning] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [activity, setActivity] = useState<ActivityItem[]>([]);
  // B5: each run carries the provider it actually used so the cost chip prices
  // with THAT, not the currently-selected provider. Optional — runs persisted
  // before this field load as undefined (cost chip hidden).
  const [runHistory, setRunHistory] = useState<{ activity: ActivityItem[]; durationSec: number; provider?: string | null }[]>([]);
  const [taskError, setTaskError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [tokenStats, setTokenStats] = useState<{ input: number; output: number; total: number } | null>(null);
  const [contextState, setContextState] = useState<{ cumulative_input: number; budget: number; phase: string; percent: number; last_input: number; last_output: number; source: string; estimate: boolean } | null>(null);
  const [liveHistory, setLiveHistory] = useState<{ role: 'user' | 'assistant'; content: string }[]>([]);
  const [lastFailedTask, setLastFailedTask] = useState<PendingTask | null>(null);
  const [hitlApprovalStatus, setHitlApprovalStatus] = useState<HitlApprovalStatus | null>(null);
  const [runFailure, setRunFailure] = useState<{ reason: string; recoverable: boolean; suggestion: string } | null>(null);
  const [rateLimitedState, setRateLimitedState] = useState<{ delay: number; attempt: number; max_retries: number } | null>(null);
  // K1: checkpoint run id emitted by Rust at spawn — used by the revert action.
  const [lastRunId, setLastRunId] = useState<string | null>(null);
  // Fix 4: promote to state so consumers re-render when cancel status changes
  const [isCancelled, setIsCancelled] = useState(false);

  // Refs for tracking streams
  // Fix 3: mirror runHistory in a ref so the IPC persist call can live outside the state updater
  const runHistoryRef = useRef<{ activity: ActivityItem[]; durationSec: number; provider?: string | null }[]>([]);
  const activityCounterRef = useRef(0);
  const activityRef = useRef<ActivityItem[]>([]);
  const activityFlushTimerRef = useRef<number | null>(null);
  const startTimeRef = useRef<number | null>(null);
  const currentTaskRef = useRef<PendingTask | null>(null);
  const lastRunTaskRef = useRef<PendingTask | null>(null);
  const cancelFlagRef = useRef(false);
  const needHelpFlagRef = useRef(false);
  const completedCodeSessionRef = useRef<SessionInfo | null>(null);
  const answerReceivedThisRunRef = useRef(false);
  const doneHandledRef = useRef(false);
  const hasSentMessageRef = useRef(false);
  // Typed-IPC capture refs — populated by kim:run-done / kim:provider-error.
  // Capture-only: no behavior changes to legacy paths, no re-render cost.
  const terminationReasonRef = useRef<string | null>(null);
  const lastProviderErrorCodeRef = useRef<string | null>(null);
  // B9: track the rate-limited auto-clear timer so a stale timer from one run
  // can't clear a later run's banner.
  const rateLimitTimerRef = useRef<number | null>(null);
  const clearRateLimitTimer = () => {
    if (rateLimitTimerRef.current !== null) {
      window.clearTimeout(rateLimitTimerRef.current);
      rateLimitTimerRef.current = null;
    }
  };

  // Deduplication maps
  const recentRawRef = useRef<Map<string, number>>(new Map());
  const recentActivityItemRef = useRef<Map<string, number>>(new Map());

  // Keep stable refs for options that change to avoid event listener rebuilds
  const isRunningRef = useRef(isRunning);
  useEffect(() => { isRunningRef.current = isRunning; }, [isRunning]);

  const onTaskDoneRef = useRef(onTaskDone);
  useEffect(() => { onTaskDoneRef.current = onTaskDone; }, [onTaskDone]);

  // commitCurrentBrowserUrl changes identity on every session / chat-tab switch (it
  // closes over browserCommandArgs → session/activeTab/conversationId). Keep it in a
  // ref so the big listener-wiring effect below doesn't tear down and re-register all
  // ~16 Tauri listeners on each switch — the async listen().then(unlisten) pattern can
  // miss the unsubscribe if it re-runs before the promise resolves, leaking duplicate
  // handlers that append activity/answers twice.
  const commitCurrentBrowserUrlRef = useRef(commitCurrentBrowserUrl);
  useEffect(() => { commitCurrentBrowserUrlRef.current = commitCurrentBrowserUrl; }, [commitCurrentBrowserUrl]);

  const sessionRef = useRef(session);
  useEffect(() => { sessionRef.current = session; }, [session]);

  const settingsRef = useRef(settings);
  useEffect(() => { settingsRef.current = settings; }, [settings]);

  // Fix 3: keep runHistoryRef in sync so the updater stays pure
  useEffect(() => { runHistoryRef.current = runHistory; }, [runHistory]);



  const activeResumeSessionId = session?.session_id ?? conversationId ?? '';
  const activeResumeSessionIdRef = useRef(activeResumeSessionId);
  useEffect(() => {
    activeResumeSessionIdRef.current = session?.session_id ?? conversationId ?? '';
  }, [session, conversationId]);

  // Deduplication functions
  const isDuplicate = useCallback((raw: string): boolean => {
    const map = recentRawRef.current;
    const now = Date.now();
    const canonical = raw.startsWith('[err]') ? raw.slice(5).trimStart() : raw;
    const last = map.get(canonical);
    if (last !== undefined && now - last < 800) return true;
    map.set(canonical, now);
    if (map.size > 200) {
      const cutoff = now - 1600;
      for (const [k, v] of map) if (v < cutoff) map.delete(k);
    }
    return false;
  }, []);

  const isDuplicateActivityItem = useCallback((item: ActivityItem): boolean => {
    const map = recentActivityItemRef.current;
    const now = Date.now();
    const key = `${item.kind}:${item.text}`;
    const last = map.get(key);
    if (last !== undefined && now - last < 2000) return true;
    map.set(key, now);
    if (map.size > 200) {
      const cutoff = now - 4000;
      for (const [k, v] of map) if (v < cutoff) map.delete(k);
    }
    return false;
  }, []);

  // Activity flushing
  const scheduleActivityFlush = useCallback(() => {
    if (activityFlushTimerRef.current !== null) return;
    activityFlushTimerRef.current = window.setTimeout(() => {
      activityFlushTimerRef.current = null;
      setActivity(activityRef.current);
    }, 50);
  }, []);

  const flushActivityNow = useCallback(() => {
    if (activityFlushTimerRef.current !== null) {
      window.clearTimeout(activityFlushTimerRef.current);
      activityFlushTimerRef.current = null;
    }
    setActivity(activityRef.current);
  }, []);

  const enqueueActivityUpdate = useCallback((updater: (prev: ActivityItem[]) => ActivityItem[]) => {
    activityRef.current = updater(activityRef.current);
    scheduleActivityFlush();
  }, [scheduleActivityFlush]);

  const clearActivityNow = useCallback(() => {
    if (activityFlushTimerRef.current !== null) {
      window.clearTimeout(activityFlushTimerRef.current);
      activityFlushTimerRef.current = null;
    }
    activityRef.current = [];
    setActivity([]);
  }, []);

  // Clean activity timer on unmount
  useEffect(() => {
    return () => {
      if (activityFlushTimerRef.current !== null) {
        window.clearTimeout(activityFlushTimerRef.current);
      }
    };
  }, []);

  // ── Timer Effect ───────────────────────────────────────────────────────────
  useEffect(() => {
    if (!isRunning) return;
    startTimeRef.current = Date.now();
    setElapsed(0);
    const id = setInterval(() => {
      setElapsed(Math.floor((Date.now() - (startTimeRef.current ?? Date.now())) / 1000));
    }, 1000);
    return () => clearInterval(id);
  }, [isRunning]);

  // Defensive isRunning guard
  useEffect(() => {
    if (!isRunning) {
      invoke('set_task_active_mode', { active: false }).catch(() => {});
    }
  }, [isRunning]);

  // ── Append stdout/stderr raw line ─────────────────────────────────────────
  const appendRaw = useCallback((line: string) => {
    if (isDuplicate(line)) return;
    const id = ++activityCounterRef.current;

    const parsed = parseAgentLine(line, id);

    switch (parsed.type) {
      case 'answer':
      case 'codex_agent_message':
        answerReceivedThisRunRef.current = true;
        setLiveHistory(prev => {
          const last = prev[prev.length - 1];
          if (last?.role === 'assistant' && last.content.trim() === parsed.payload.trim()) return prev;
          return [...prev, { role: 'assistant', content: parsed.payload }];
        });
        break;
      case 'codex_reasoning':
        enqueueActivityUpdate(prev => {
          const next = [...prev, { id, kind: 'tool' as const, icon: '💭', text: parsed.payload }];
          if (next.length > MAX_ACTIVITY_ITEMS) return next.slice(next.length - MAX_ACTIVITY_ITEMS);
          return next;
        });
        break;
      case 'codex_shell_call':
        enqueueActivityUpdate(prev => {
          const next = [...prev, { id, kind: 'tool' as const, icon: '⚡', text: parsed.payload }];
          if (next.length > MAX_ACTIVITY_ITEMS) return next.slice(next.length - MAX_ACTIVITY_ITEMS);
          return next;
        });
        break;
      case 'codex_ignored':
        break;
      case 'error':
        setTaskError(parsed.payload);
        if (lastRunTaskRef.current) setLastFailedTask(lastRunTaskRef.current);
        needHelpFlagRef.current = true;
        enqueueActivityUpdate(prev => {
          const next = [...prev, { id, kind: 'error' as const, icon: '⚠', text: parsed.payload }];
          if (next.length > MAX_ACTIVITY_ITEMS) return next.slice(next.length - MAX_ACTIVITY_ITEMS);
          return next;
        });
        break;
      case 'diff': {
        const { added, removed } = parsed.payload;
        enqueueActivityUpdate(prev => {
          if (prev.length === 0) return prev;
          const last = prev[prev.length - 1];
          if (last.kind === 'tool' && (last.text.includes('Editing') || last.text.includes('Writing') || last.text.includes('Creating'))) {
            const annotated = { ...last, text: last.text + ` +${added} -${removed}` };
            return [...prev.slice(0, -1), annotated];
          }
          return prev;
        });
        break;
      }
      case 'need_help':
        needHelpFlagRef.current = true;
        setTaskError(parsed.payload);
        if (lastRunTaskRef.current) {
          setLastFailedTask(lastRunTaskRef.current);
        }
        break;
      case 'activity_item':
        if (isDuplicateActivityItem(parsed.payload)) return;

        enqueueActivityUpdate(prev => {
          if (parsed.payload.kind === 'success') return prev; // Skip success inside activity feed to prevent duplicate assistant bubble
          if (parsed.payload.kind === 'status' && prev.length > 0 && prev[prev.length - 1].kind === 'status') {
            return [...prev.slice(0, -1), parsed.payload];
          }
          const next = [...prev, parsed.payload];
          if (next.length > MAX_ACTIVITY_ITEMS) return next.slice(next.length - MAX_ACTIVITY_ITEMS);
          return next;
        });

        // Success bubble creation
        if (parsed.payload.kind === 'success') {
          const genericSuccess = /^Task completed(?: successfully)?$/i.test(parsed.payload.text.trim());
          if (!answerReceivedThisRunRef.current && !genericSuccess) {
            setLiveHistory(prev => [...prev, { role: 'assistant', content: parsed.payload.text }]);
          }
        }
        break;
      case 'none':
      default:
        break;
    }
  }, [isDuplicate, isDuplicateActivityItem, enqueueActivityUpdate]);

  // ── Event Listener Wiring ─────────────────────────────────────────────────
  useEffect(() => {
    // Fix 2: track whether this effect instance has been cleaned up so that
    // listen() promises resolving after unmount immediately call their unlisten fn
    // instead of assigning to a stale variable (preventing duplicate handlers).
    let cancelled = false;
    let unlistenOutput: (() => void) | undefined;
    let unlistenError: (() => void) | undefined;
    let unlistenDone: (() => void) | undefined;
    let unlistenCodeSession: (() => void) | undefined;
    let unlistenCancelled: (() => void) | undefined;
    let unlistenTypedContext: (() => void) | undefined;
    let unlistenTypedStats: (() => void) | undefined;
    let unlistenTypedUi: (() => void) | undefined;
    let unlistenTypedRunDone: (() => void) | undefined;
    let unlistenTypedRunFailed: (() => void) | undefined;
    let unlistenTypedProviderError: (() => void) | undefined;
    let unlistenTypedRateLimited: (() => void) | undefined;
    let unlistenTypedHitlRequest: (() => void) | undefined;
    let unlistenTypedHitlResult: (() => void) | undefined;
    let unlistenRunId: (() => void) | undefined;

    // Typed IPC listeners (kim:* events). Post-V-1, activity/plan/step state is
    // derived from the legacy kim-agent-output stream via parsePlanFromActivity,
    // so kim:status/plan/step/done carried no extra state — those four no-op
    // listeners were removed (B10). The typed listeners below DO own state.
    listen<{
      cumulative_input: number;
      budget: number;
      phase: string;
      percent: number;
      last_input: number;
      last_output: number;
      source: string;
      estimate: boolean;
    }>('kim:context', e => {
      setContextState({
        cumulative_input: e.payload.cumulative_input,
        budget: e.payload.budget,
        phase: e.payload.phase,
        percent: e.payload.percent,
        last_input: e.payload.last_input,
        last_output: e.payload.last_output,
        source: e.payload.source,
        estimate: e.payload.estimate,
      });
    }).then(fn => { if (!cancelled) unlistenTypedContext = fn; else fn(); });

    listen<{ input: number; output: number; total: number }>('kim:stats', e => {
      setTokenStats({ input: e.payload.input, output: e.payload.output, total: e.payload.total });
    }).then(fn => { if (!cancelled) unlistenTypedStats = fn; else fn(); });

    // Typed run lifecycle events — capture-only in current slice.
    // terminationReasonRef gives the kim-agent-done handler richer context;
    // exposing it to the UI (e.g. a termination-specific banner) is the
    // remaining Tier 2b gap documented in HARNESS_ROADMAP.md.
    listen<{ termination: string; success: boolean }>('kim:run-done', e => {
      terminationReasonRef.current = e.payload.termination;
    }).then(fn => { if (!cancelled) unlistenTypedRunDone = fn; else fn(); });

    // Capture provider error code for future UI surfacing (Tier 2e remaining gap).
    // Not surfaced via setTaskError here because the taskError string is rendered
    // raw by StreamRenderer when !== 'agent-error'; a dedicated banner slot is
    // needed before a structured code can be shown to the user.
    listen<{ code: string; retryable: boolean }>('kim:provider-error', e => {
      lastProviderErrorCodeRef.current = e.payload.code;
    }).then(fn => { if (!cancelled) unlistenTypedProviderError = fn; else fn(); });

    // HITL approval visibility. This is display-only for now; the approval
    // decision path still lives in the agent/UIBridge layer.
    listen<{ tool: string; risk: string; reason: string; preview?: string }>('kim:hitl-approval-request', e => {
      setHitlApprovalStatus({
        tool: e.payload.tool,
        risk: e.payload.risk,
        reason: e.payload.reason,
        preview: e.payload.preview, // K6
        approved: null,
      });
    }).then(fn => { if (!cancelled) unlistenTypedHitlRequest = fn; else fn(); });

    listen<{ tool: string; approved: boolean }>('kim:hitl-approval-result', e => {
      setHitlApprovalStatus(prev => ({
        tool: e.payload.tool,
        risk: prev?.tool === e.payload.tool ? prev.risk : 'high',
        reason: prev?.tool === e.payload.tool ? prev.reason : 'approval_result',
        // Carry the preview over so the command/diff preview doesn't vanish the moment
        // the user clicks Approve/Deny (StreamRenderer only renders it when set).
        preview: prev?.tool === e.payload.tool ? prev.preview : undefined,
        approved: e.payload.approved,
      }));
    }).then(fn => { if (!cancelled) unlistenTypedHitlResult = fn; else fn(); });

    listen<string>('kim-run-id', e => { setLastRunId(e.payload); }) // K1
      .then(fn => { if (!cancelled) unlistenRunId = fn; else fn(); });

    listen<{ reason: string; recoverable: boolean; suggestion: string }>('kim:run-failed', e => {
      setRunFailure(e.payload);
    }).then(fn => { if (!cancelled) unlistenTypedRunFailed = fn; else fn(); });

    listen<{ delay: number; attempt: number; max_retries: number }>('kim:rate-limited', e => {
      setRateLimitedState(e.payload);
      // B9: cancel any prior auto-clear timer before scheduling a new one.
      clearRateLimitTimer();
      rateLimitTimerRef.current = window.setTimeout(() => {
        rateLimitTimerRef.current = null;
        setRateLimitedState(null);
      }, (e.payload.delay + 1) * 1000);
    }).then(fn => { if (!cancelled) unlistenTypedRateLimited = fn; else fn(); });

    listen<{ action: 'screenshot_flash' | 'show' }>('kim:ui', e => {
      if (e.payload.action === 'screenshot_flash' && isRunningRef.current) {
        invoke('show_screenshot_flash').catch(() => {});
        invoke('set_task_active_mode', { active: true }).catch(() => {});
      } else if (e.payload.action === 'show' && isRunningRef.current) {
        invoke('show_main_window').catch(() => {});
      }
    }).then(fn => { if (!cancelled) unlistenTypedUi = fn; else fn(); });

    listen<string>('kim-agent-output', event => {
      appendRaw(event.payload);
    }).then(fn => { if (!cancelled) unlistenOutput = fn; else fn(); });

    listen<string>('kim-agent-error', event => {
      appendRaw(`[err] ${event.payload}`);
    }).then(fn => { if (!cancelled) unlistenError = fn; else fn(); });

    listen<boolean>('kim-agent-done', event => {
      invoke('set_task_active_mode', { active: false }).catch(() => {});
      const wasCancelled = cancelFlagRef.current;
      const hadNeedHelp = needHelpFlagRef.current;
      doneHandledRef.current = true;
      cancelFlagRef.current = false;
      setIsCancelled(false); // Fix 4: keep state in sync with ref
      needHelpFlagRef.current = false;
      setIsRunning(false);
      setCancelling(false);
      // B7: a run that ends while an approval is pending (timeout/agent exit)
      // must not leave a dead Approve/Deny card (clicking it hits a dead run).
      setHitlApprovalStatus(null);
      clearRateLimitTimer(); // B9: don't let a stale timer touch the next run

      const startedAt = startTimeRef.current;
      const durationSec = startedAt ? Math.max(1, Math.round((Date.now() - startedAt) / 1000)) : 0;
      flushActivityNow();
      const activitySnapshot = activityRef.current;

      if (event.payload && !wasCancelled && activitySnapshot.length > 0) {
        // Fix 3: compute next outside the updater so React's StrictMode/concurrent
        // double-invocation of updater functions cannot trigger duplicate IPC writes.
        const newRunEntry = { activity: activitySnapshot, durationSec, provider: currentTaskRef.current?.provider ?? null };
        const next = [...runHistoryRef.current, newRunEntry];
        setRunHistory(next);
        const completedCodeSession = completedCodeSessionRef.current;
        const sid = completedCodeSession?.session_id ?? activeResumeSessionIdRef.current;
        if (sid) {
          invoke('save_run_history', {
            sessionId: sid,
            sessionDate: completedCodeSession?.date ?? sessionRef.current?.date ?? null,
            kimDir: settingsRef.current.kim_sessions_dir || null,
            codexDir: completedCodeSession?.project_path
              ? `${completedCodeSession.project_path}/.codex/sessions`
              : settingsRef.current.codex_sessions_dir || null,
            runs: next,
          }).catch(() => {});
        }
      }
      clearActivityNow();
      setMessageReloadNonce(v => v + 1);

      const completedCodeSession = completedCodeSessionRef.current ?? undefined;
      const completedSessionId = completedCodeSession?.session_id ?? activeResumeSessionIdRef.current;
      const runProviderSite = browserSiteFromProvider(currentTaskRef.current?.provider);
      void commitCurrentBrowserUrlRef.current(runProviderSite, completedCodeSession ?? sessionRef.current, completedSessionId);

      onTaskDoneRef.current(completedSessionId, completedCodeSession);
      completedCodeSessionRef.current = null;

      if (!event.payload && !wasCancelled) {
        if (!hadNeedHelp) {
          // Fix 1: only strip the failed run's assistant output; keep prior good answers
          // by finding the last user message and slicing everything after it off.
          setLiveHistory(prev => {
            const lastUserIdx = prev.map(m => m.role).lastIndexOf('user');
            return lastUserIdx === -1 ? [] : prev.slice(0, lastUserIdx + 1);
          });
          setTaskError(
            providerErrorMessage(lastProviderErrorCodeRef.current)
              ?? terminationMessage(terminationReasonRef.current)
              ?? 'agent-error'
          );
        }
        if (lastRunTaskRef.current) {
          setLastFailedTask(lastRunTaskRef.current);
        }
      } else if (event.payload && !hadNeedHelp) {
        setLastFailedTask(null);
      }
      currentTaskRef.current = null;
    }).then(fn => { if (!cancelled) unlistenDone = fn; else fn(); });

    listen<SessionInfo>('kim-agent-code-session', event => {
      completedCodeSessionRef.current = event.payload;
    }).then(fn => { if (!cancelled) unlistenCodeSession = fn; else fn(); });

    listen<boolean>('kim-agent-cancelled', () => {
      invoke('set_task_active_mode', { active: false }).catch(() => {});
      cancelFlagRef.current = true;
      setIsCancelled(true); // Fix 4: keep state in sync with ref
      appendRaw('⏹ Task cancelled');
      setIsRunning(false);
      setCancelling(false);
      setHitlApprovalStatus(null);
      currentTaskRef.current = null;
    }).then(fn => { if (!cancelled) unlistenCancelled = fn; else fn(); });

    return () => {
      // Fix 2: mark this effect instance as dead so any in-flight listen() promises
      // that resolve after this cleanup immediately call their unlisten fn.
      cancelled = true;
      unlistenOutput?.();
      unlistenError?.();
      unlistenDone?.();
      unlistenCodeSession?.();
      unlistenCancelled?.();
      clearRateLimitTimer(); // B9: cancel pending rate-limit timer on unmount
      unlistenTypedContext?.();
      unlistenTypedStats?.();
      unlistenTypedUi?.();
      unlistenTypedRunDone?.();
      unlistenTypedRunFailed?.();
      unlistenTypedProviderError?.();
      unlistenTypedRateLimited?.();
      unlistenTypedHitlRequest?.();
      unlistenTypedHitlResult?.();
      unlistenRunId?.();
    };
    // commitCurrentBrowserUrl intentionally omitted — accessed via ref so this effect
    // registers listeners once and doesn't re-run (and leak handlers) on session switch.
  }, [appendRaw, flushActivityNow, clearActivityNow, setMessageReloadNonce]);

  // Derived state to satisfy Prompt 8 explicit signature
  const traceItems = buildThinkingTrace(activity, parsePlanFromActivity(activity));
  const livePlan = parsePlanFromActivity(activity);
  const planSteps = livePlan?.steps ?? [];
  const activityEntries = activity;
  const lastStatus = activity.filter(a => a.kind === 'status').slice(-1)[0]?.text ?? '';
  const contextUsage = contextState?.percent ?? 0;
  const isDone = !isRunning;
  // isCancelled is now proper state (Fix 4) — declared above with useState

  return {
    // Explicit Prompt 8 properties
    traceItems,
    planSteps,
    activityEntries,
    lastStatus,
    contextUsage,
    isDone,
    isCancelled,

    // Additional state & refs needed by ChatView
    isRunning,
    setIsRunning,
    cancelling,
    setCancelling,
    activity,
    setActivity,
    runHistory,
    setRunHistory,
    taskError,
    setTaskError,
    elapsed,
    setElapsed,
    tokenStats,
    setTokenStats,
    contextState,
    setContextState,
    liveHistory,
    setLiveHistory,
    lastFailedTask,
    setLastFailedTask,
    hitlApprovalStatus,
    setHitlApprovalStatus,
    runFailure,
    setRunFailure,
    rateLimitedState,
    setRateLimitedState,
    lastRunId, // K1

    // Refs
    currentTaskRef,
    lastRunTaskRef,
    cancelFlagRef,
    needHelpFlagRef,
    completedCodeSessionRef,
    answerReceivedThisRunRef,
    doneHandledRef,
    hasSentMessageRef,
    terminationReasonRef,
    lastProviderErrorCodeRef,
    activityCounterRef,
    activityRef,
    activityFlushTimerRef,
    startTimeRef,

    // Deduplication Maps
    recentRawRef,
    recentActivityItemRef,

    // Operations
    clearActivityNow,
    flushActivityNow,
    enqueueActivityUpdate,

    // HITL approval round-trip
    hitlRespond: (approved: boolean) => invoke('hitl_respond_approval', { approved }).catch(() => {}),
  };
}

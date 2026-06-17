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
}

export function useChatStream({
  session,
  settings,
  onTaskDone,
  commitCurrentBrowserUrl,
  setMessageReloadNonce,
}: UseChatStreamProps) {
  const [isRunning, setIsRunning] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [activity, setActivity] = useState<ActivityItem[]>([]);
  const [runHistory, setRunHistory] = useState<{ activity: ActivityItem[]; durationSec: number }[]>([]);
  const [taskError, setTaskError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [tokenStats, setTokenStats] = useState<{ input: number; output: number; total: number } | null>(null);
  const [contextState, setContextState] = useState<{ cumulative_input: number; budget: number; phase: string; percent: number; last_input: number; last_output: number; source: string; estimate: boolean } | null>(null);
  const [liveHistory, setLiveHistory] = useState<{ role: 'user' | 'assistant'; content: string }[]>([]);
  const [lastFailedTask, setLastFailedTask] = useState<PendingTask | null>(null);
  const [hitlApprovalStatus, setHitlApprovalStatus] = useState<HitlApprovalStatus | null>(null);
  const [runFailure, setRunFailure] = useState<{ reason: string; recoverable: boolean; suggestion: string } | null>(null);
  const [rateLimitedState, setRateLimitedState] = useState<{ delay: number; attempt: number; max_retries: number } | null>(null);

  // Refs for tracking streams
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

  // Deduplication maps
  const recentRawRef = useRef<Map<string, number>>(new Map());
  const recentActivityItemRef = useRef<Map<string, number>>(new Map());

  // Keep stable refs for options that change to avoid event listener rebuilds
  const isRunningRef = useRef(isRunning);
  useEffect(() => { isRunningRef.current = isRunning; }, [isRunning]);

  const onTaskDoneRef = useRef(onTaskDone);
  useEffect(() => { onTaskDoneRef.current = onTaskDone; }, [onTaskDone]);

  const sessionRef = useRef(session);
  useEffect(() => { sessionRef.current = session; }, [session]);

  const settingsRef = useRef(settings);
  useEffect(() => { settingsRef.current = settings; }, [settings]);



  const activeResumeSessionId = session?.session_id ?? '';
  const activeResumeSessionIdRef = useRef(activeResumeSessionId);
  useEffect(() => { activeResumeSessionIdRef.current = activeResumeSessionId; }, [activeResumeSessionId]);

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
    let unlistenOutput: (() => void) | undefined;
    let unlistenError: (() => void) | undefined;
    let unlistenDone: (() => void) | undefined;
    let unlistenCodeSession: (() => void) | undefined;
    let unlistenCancelled: (() => void) | undefined;
    let unlistenTypedStatus: (() => void) | undefined;
    let unlistenTypedPlan: (() => void) | undefined;
    let unlistenTypedStep: (() => void) | undefined;
    let unlistenTypedDone: (() => void) | undefined;
    let unlistenTypedContext: (() => void) | undefined;
    let unlistenTypedStats: (() => void) | undefined;
    let unlistenTypedUi: (() => void) | undefined;
    let unlistenTypedRunDone: (() => void) | undefined;
    let unlistenTypedRunFailed: (() => void) | undefined;
    let unlistenTypedProviderError: (() => void) | undefined;
    let unlistenTypedRateLimited: (() => void) | undefined;
    let unlistenTypedHitlRequest: (() => void) | undefined;
    let unlistenTypedHitlResult: (() => void) | undefined;

    // Typed IPC listeners (kim:* events) — update parallel state only, never push activity items.
    // These fire when ipc_protocol == "typed" in Rust config; the legacy kim-agent-output
    // path continues to run in parallel (dual-emit) so no data is lost.
    listen<{ message: string }>('kim:status', _e => {
      // Status messages drive activity via legacy [STATUS] stderr parsing.
      // Nothing extra needed here.
    }).then(fn => { unlistenTypedStatus = fn; });

    listen<{ steps: string[] }>('kim:plan', _e => {
      // Plan state is derived via parsePlanFromActivity on the activity array.
      // No direct state mutation needed here.
    }).then(fn => { unlistenTypedPlan = fn; });

    listen<{ n: number; data: Record<string, unknown> }>('kim:step', _e => {
      // Step transitions drive plan state via legacy activity parsing.
    }).then(fn => { unlistenTypedStep = fn; });

    listen<{ n: number }>('kim:done', _e => {
      // Done markers drive plan state via legacy activity parsing.
    }).then(fn => { unlistenTypedDone = fn; });

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
    }).then(fn => { unlistenTypedContext = fn; });

    listen<{ input: number; output: number; total: number }>('kim:stats', e => {
      setTokenStats({ input: e.payload.input, output: e.payload.output, total: e.payload.total });
    }).then(fn => { unlistenTypedStats = fn; });

    // Typed run lifecycle events — capture-only in current slice.
    // terminationReasonRef gives the kim-agent-done handler richer context;
    // exposing it to the UI (e.g. a termination-specific banner) is the
    // remaining Tier 2b gap documented in HARNESS_ROADMAP.md.
    listen<{ termination: string; success: boolean }>('kim:run-done', e => {
      terminationReasonRef.current = e.payload.termination;
    }).then(fn => { unlistenTypedRunDone = fn; });

    // Capture provider error code for future UI surfacing (Tier 2e remaining gap).
    // Not surfaced via setTaskError here because the taskError string is rendered
    // raw by StreamRenderer when !== 'agent-error'; a dedicated banner slot is
    // needed before a structured code can be shown to the user.
    listen<{ code: string; retryable: boolean }>('kim:provider-error', e => {
      lastProviderErrorCodeRef.current = e.payload.code;
    }).then(fn => { unlistenTypedProviderError = fn; });

    // HITL approval visibility. This is display-only for now; the approval
    // decision path still lives in the agent/UIBridge layer.
    listen<{ tool: string; risk: string; reason: string }>('kim:hitl-approval-request', e => {
      setHitlApprovalStatus({
        tool: e.payload.tool,
        risk: e.payload.risk,
        reason: e.payload.reason,
        approved: null,
      });
    }).then(fn => { unlistenTypedHitlRequest = fn; });

    listen<{ tool: string; approved: boolean }>('kim:hitl-approval-result', e => {
      setHitlApprovalStatus(prev => ({
        tool: e.payload.tool,
        risk: prev?.tool === e.payload.tool ? prev.risk : 'high',
        reason: prev?.tool === e.payload.tool ? prev.reason : 'approval_result',
        approved: e.payload.approved,
      }));
    }).then(fn => { unlistenTypedHitlResult = fn; });

    listen<{ reason: string; recoverable: boolean; suggestion: string }>('kim:run-failed', e => {
      setRunFailure(e.payload);
    }).then(fn => { unlistenTypedRunFailed = fn; });

    listen<{ delay: number; attempt: number; max_retries: number }>('kim:rate-limited', e => {
      setRateLimitedState(e.payload);
      // Auto-clear after the delay so the banner disappears when the retry fires
      setTimeout(() => setRateLimitedState(null), (e.payload.delay + 1) * 1000);
    }).then(fn => { unlistenTypedRateLimited = fn; });

    listen<{ action: 'screenshot_flash' | 'show' }>('kim:ui', e => {
      if (e.payload.action === 'screenshot_flash' && isRunningRef.current) {
        invoke('show_screenshot_flash').catch(() => {});
        invoke('set_task_active_mode', { active: true }).catch(() => {});
      } else if (e.payload.action === 'show' && isRunningRef.current) {
        invoke('show_main_window').catch(() => {});
      }
    }).then(fn => { unlistenTypedUi = fn; });

    listen<string>('kim-agent-output', event => {
      appendRaw(event.payload);
    }).then(fn => { unlistenOutput = fn; });

    listen<string>('kim-agent-error', event => {
      appendRaw(`[err] ${event.payload}`);
    }).then(fn => { unlistenError = fn; });

    listen<boolean>('kim-agent-done', event => {
      invoke('set_task_active_mode', { active: false }).catch(() => {});
      const wasCancelled = cancelFlagRef.current;
      const hadNeedHelp = needHelpFlagRef.current;
      doneHandledRef.current = true;
      cancelFlagRef.current = false;
      needHelpFlagRef.current = false;
      setIsRunning(false);
      setCancelling(false);
      // B7: a run that ends while an approval is pending (timeout/agent exit)
      // must not leave a dead Approve/Deny card (clicking it hits a dead run).
      setHitlApprovalStatus(null);

      const startedAt = startTimeRef.current;
      const durationSec = startedAt ? Math.max(1, Math.round((Date.now() - startedAt) / 1000)) : 0;
      flushActivityNow();
      const activitySnapshot = activityRef.current;

      if (event.payload && !wasCancelled && activitySnapshot.length > 0) {
        setRunHistory(prev => {
          const next = [...prev, { activity: activitySnapshot, durationSec }];
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
          return next;
        });
      }
      clearActivityNow();
      setMessageReloadNonce(v => v + 1);

      const completedCodeSession = completedCodeSessionRef.current ?? undefined;
      const completedSessionId = completedCodeSession?.session_id ?? activeResumeSessionIdRef.current;
      const runProviderSite = browserSiteFromProvider(currentTaskRef.current?.provider);
      void commitCurrentBrowserUrl(runProviderSite, completedCodeSession ?? sessionRef.current, completedSessionId);

      onTaskDoneRef.current(completedSessionId, completedCodeSession);
      completedCodeSessionRef.current = null;

      if (!event.payload && !wasCancelled) {
        if (!hadNeedHelp) {
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
    }).then(fn => { unlistenDone = fn; });

    listen<SessionInfo>('kim-agent-code-session', event => {
      completedCodeSessionRef.current = event.payload;
    }).then(fn => { unlistenCodeSession = fn; });

    listen<boolean>('kim-agent-cancelled', () => {
      invoke('set_task_active_mode', { active: false }).catch(() => {});
      cancelFlagRef.current = true;
      appendRaw('⏹ Task cancelled');
      setIsRunning(false);
      setCancelling(false);
      setHitlApprovalStatus(null);
      currentTaskRef.current = null;
    }).then(fn => { unlistenCancelled = fn; });

    return () => {
      unlistenOutput?.();
      unlistenError?.();
      unlistenDone?.();
      unlistenCodeSession?.();
      unlistenCancelled?.();
      unlistenTypedStatus?.();
      unlistenTypedPlan?.();
      unlistenTypedStep?.();
      unlistenTypedDone?.();
      unlistenTypedContext?.();
      unlistenTypedStats?.();
      unlistenTypedUi?.();
      unlistenTypedRunDone?.();
      unlistenTypedRunFailed?.();
      unlistenTypedProviderError?.();
      unlistenTypedRateLimited?.();
      unlistenTypedHitlRequest?.();
      unlistenTypedHitlResult?.();
    };
  }, [appendRaw, flushActivityNow, clearActivityNow, setMessageReloadNonce, commitCurrentBrowserUrl]);

  // Derived state to satisfy Prompt 8 explicit signature
  const traceItems = buildThinkingTrace(activity, parsePlanFromActivity(activity));
  const livePlan = parsePlanFromActivity(activity);
  const planSteps = livePlan?.steps ?? [];
  const activityEntries = activity;
  const lastStatus = activity.filter(a => a.kind === 'status').slice(-1)[0]?.text ?? '';
  const contextUsage = contextState?.percent ?? 0;
  const isDone = !isRunning;
  const isCancelled = cancelFlagRef.current;

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

import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { listen } from '@tauri-apps/api/event';
import { invoke } from '@tauri-apps/api/core';
import { toast } from '../components/Toast';
import type { ActivityItem, PendingTask, HitlApprovalStatus, LivePlanParsed } from '../components/chat/types';
import type { SessionInfo, Settings } from '../types';
import { parseAgentLine, buildThinkingTrace } from '../components/chat/parsers';
import { browserSiteFromProvider, friendlyError, parsePlanFromActivity, TOOL_MAP } from '../components/chat/utils';
import {
  KimEventNames,
  type KimActivityPayload,
  type KimAnswerPayload,
  type KimAssistantDeltaPayload,
  type KimItemLifecyclePayload,
  type KimReasoningDeltaPayload,
  type KimContextPayload,
  type KimDiffPayload,
  type KimDonePayload,
  type KimHitlApprovalRequestPayload,
  type KimHitlApprovalResultPayload,
  type KimPlanPayload,
  type KimProviderErrorPayload,
  type KimRateLimitedPayload,
  type KimRunDonePayload,
  type KimRunFailedPayload,
  type KimStatsPayload,
  type KimStatusPayload,
  type KimStepPayload,
  type KimToolPayload,
  type KimUiPayload,
} from '../types/events.gen';

const MAX_ACTIVITY_ITEMS = 300;
// V-audit #6: liveHistory (the rendered chat bubbles for the in-progress /
// current-mount conversation) had no size cap, unlike the 300-item activity
// feed above it. A very long-running session could grow this array without
// bound. Mirror the activity cap's threshold and "keep the newest N" trim
// behavior.
const MAX_LIVE_HISTORY_ITEMS = MAX_ACTIVITY_ITEMS;

// L4: named constant for the one non-generated event this hook listens to
// (kim-run-id is emitted straight from Rust and isn't in KimEventNames).
const KIM_RUN_ID_EVENT = 'kim-run-id';

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
  // RUN-IDENTITY (D1/B4/B5): the single globally-active run's identity, tracked
  // at App level (from the `kim-run-id` event) so it survives the ChatView
  // remount that a tab/session/New-Chat switch triggers. When this view's own
  // session owns that run, the view re-derives isRunning=true and re-attaches to
  // the live event stream instead of orphaning it. Null when no run is active or
  // the active run belongs to a different session.
  activeRunSessionId?: string | null;
  activeRunId?: string | null;
}

export function useChatStream({
  session,
  settings,
  onTaskDone,
  commitCurrentBrowserUrl,
  setMessageReloadNonce,
  conversationId,
  activeRunSessionId,
  activeRunId,
}: UseChatStreamProps) {
  // The session id this view represents — the identity every run-scoped event
  // must match to be routed here (see `belongsToView`).
  const viewSessionId = session?.session_id ?? conversationId ?? '';
  // Does the currently-active run (if any) belong to THIS view's session?
  const ownsActiveRun = !!activeRunSessionId && activeRunSessionId === viewSessionId;
  // Initialize the running state from the active-run hint so a view remounted
  // mid-run (switch-back) shows Stop, not Send, and re-attaches to the stream.
  const [isRunning, setIsRunning] = useState(ownsActiveRun);
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
  const [liveHistory, setLiveHistoryRaw] = useState<{ role: 'user' | 'assistant'; content: string }[]>([]);
  const [lastFailedTask, setLastFailedTask] = useState<PendingTask | null>(null);
  const [hitlApprovalStatus, setHitlApprovalStatus] = useState<HitlApprovalStatus | null>(null);
  const [runFailure, setRunFailure] = useState<{ reason: string; recoverable: boolean; suggestion: string } | null>(null);
  const [rateLimitedState, setRateLimitedState] = useState<{ delay: number; attempt: number; max_retries: number } | null>(null);
  // K1: checkpoint run id emitted by Rust at spawn — used by the revert action.
  const [lastRunId, setLastRunId] = useState<string | null>(null);
  const [typedStatus, setTypedStatus] = useState('');
  const [typedLivePlan, setTypedLivePlan] = useState<LivePlanParsed | null>(null);
  // Fix 4: promote to state so consumers re-render when cancel status changes
  const [isCancelled, setIsCancelled] = useState(false);

  // V-audit #6: single choke point for every liveHistory update (inside this
  // hook AND from external callers like useTaskRunner/ChatView, which receive
  // this same function via the hook's return value) so the 300-item cap
  // applies everywhere liveHistory grows, not just at hand-picked call sites.
  // Mirrors the activity feed's MAX_ACTIVITY_ITEMS trim-oldest behavior.
  const setLiveHistory = useCallback(
    (updater: React.SetStateAction<{ role: 'user' | 'assistant'; content: string }[]>) => {
      setLiveHistoryRaw(prev => {
        const next = typeof updater === 'function'
          ? (updater as (p: typeof prev) => typeof prev)(prev)
          : updater;
        return next.length > MAX_LIVE_HISTORY_ITEMS
          ? next.slice(next.length - MAX_LIVE_HISTORY_ITEMS)
          : next;
      });
    },
    []
  );

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

  // RUN-IDENTITY refs. runOwnerSessionIdRef is the session the in-flight run
  // belongs to (captured at spawn by useTaskRunner, or re-derived on switch-back
  // from the active-run hint). currentRunIdRef mirrors the run_id from the
  // `kim-run-id` event. History is filed under the OWNER, never under "the
  // session currently on screen", so a mid-run switch cannot misfile it (B5/D1).
  const runOwnerSessionIdRef = useRef<string | null>(ownsActiveRun ? activeRunSessionId ?? null : null);
  const currentRunIdRef = useRef<string | null>(ownsActiveRun ? activeRunId ?? null : null);

  // Route guard: a run-scoped event is for THIS view iff it carries no session
  // envelope (legacy/codex/bridge streams) or its session_id matches this view's
  // session. Foreign-run events (a run started under a different session that is
  // still streaming after a switch) are dropped so run A never mutates view B.
  // V-audit #3: "no envelope" must mean the field is genuinely absent
  // (undefined/null), not merely falsy — `!sid` also matched an empty string,
  // which would treat a real (if degenerate) foreign session_id of "" as "no
  // session" and let it through. Compare against absence explicitly so an
  // empty-string session_id is routed like any other non-matching id.
  const belongsToView = useCallback(
    (sid?: string | null) =>
      sid === undefined || sid === null || sid === activeResumeSessionIdRef.current,
    [],
  );

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

  // ── M7: streamed assistant/reasoning deltas (codex app-server transport) ──
  // kim:assistant-delta / kim:reasoning-delta / kim:item-lifecycle arrive from
  // Rust but previously had no listeners — streamed text was dropped on the
  // floor. Assistant deltas accumulate into ONE live bubble (flushed at 50ms,
  // like activity) that the final kim:answer replaces; reasoning deltas are
  // line-buffered into the activity feed; item-lifecycle 'started' items become
  // activity entries.
  const streamingAnswerRef = useRef<{ active: boolean; text: string }>({ active: false, text: '' });
  const assistantFlushTimerRef = useRef<number | null>(null);
  const reasoningBufRef = useRef('');

  const resetDeltaStreams = useCallback(() => {
    if (assistantFlushTimerRef.current !== null) {
      window.clearTimeout(assistantFlushTimerRef.current);
      assistantFlushTimerRef.current = null;
    }
    streamingAnswerRef.current = { active: false, text: '' };
    reasoningBufRef.current = '';
  }, []);

  const flushAssistantDelta = useCallback(() => {
    const text = streamingAnswerRef.current.text;
    if (!text.trim()) return;
    setLiveHistory(prev => {
      if (!streamingAnswerRef.current.active) {
        streamingAnswerRef.current.active = true;
        return [...prev, { role: 'assistant', content: text }];
      }
      // Replace the streaming bubble (the last assistant entry we appended).
      for (let i = prev.length - 1; i >= 0; i--) {
        if (prev[i].role === 'assistant') {
          const next = [...prev];
          next[i] = { role: 'assistant', content: text };
          return next;
        }
      }
      return [...prev, { role: 'assistant', content: text }];
    });
  }, []);

  const scheduleAssistantFlush = useCallback(() => {
    if (assistantFlushTimerRef.current !== null) return;
    assistantFlushTimerRef.current = window.setTimeout(() => {
      assistantFlushTimerRef.current = null;
      flushAssistantDelta();
    }, 50);
  }, [flushAssistantDelta]);

  const clearActivityNow = useCallback(() => {
    if (activityFlushTimerRef.current !== null) {
      window.clearTimeout(activityFlushTimerRef.current);
      activityFlushTimerRef.current = null;
    }
    activityRef.current = [];
    setActivity([]);
    setTypedStatus('');
    setTypedLivePlan(null);
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
        if (!parsed.payload.trim()) break;
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

  const appendTypedActivity = useCallback((kind: ActivityItem['kind'], text: string) => {
    const cleanText = text.trim();
    if (!cleanText) return;
    const item: ActivityItem = {
      id: ++activityCounterRef.current,
      kind,
      icon: kind === 'error' ? '⚠' : kind === 'success' ? '✓' : '›',
      text: cleanText,
    };
    if (isDuplicateActivityItem(item)) return;
    enqueueActivityUpdate(prev => {
      if (kind === 'success') return prev;
      if (kind === 'status' && prev.length > 0 && prev[prev.length - 1].kind === 'status') {
        return [...prev.slice(0, -1), item];
      }
      const next = [...prev, item];
      return next.length > MAX_ACTIVITY_ITEMS ? next.slice(-MAX_ACTIVITY_ITEMS) : next;
    });
  }, [enqueueActivityUpdate, isDuplicateActivityItem]);

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
    let unlistenTypedStatus: (() => void) | undefined;
    let unlistenTypedPlan: (() => void) | undefined;
    let unlistenTypedStep: (() => void) | undefined;
    let unlistenTypedDone: (() => void) | undefined;
    let unlistenTypedTool: (() => void) | undefined;
    let unlistenTypedAnswer: (() => void) | undefined;
    let unlistenTypedDiff: (() => void) | undefined;
    let unlistenTypedActivity: (() => void) | undefined;
    let unlistenTypedAssistantDelta: (() => void) | undefined;
    let unlistenTypedReasoningDelta: (() => void) | undefined;
    let unlistenTypedItemLifecycle: (() => void) | undefined;
    let unlistenRunId: (() => void) | undefined;

    // Typed IPC listeners own Kim's UI state. The raw listener below remains
    // exclusively for the external Codex JSONL/text compatibility stream.
    listen<KimStatusPayload>(KimEventNames.STATUS, e => {
      if (!belongsToView(e.payload.session_id)) return;
      setTypedStatus(e.payload.message);
      appendTypedActivity('status', e.payload.message);
    }).then(fn => { if (!cancelled) unlistenTypedStatus = fn; else fn(); });

    listen<KimPlanPayload>(KimEventNames.PLAN, e => {
      if (!belongsToView(e.payload.session_id)) return;
      const steps = e.payload.steps.filter((step): step is string => typeof step === 'string').slice(0, 12);
      if (steps.length < 2) return;
      setTypedLivePlan({ steps, activeStep: 0, doneSteps: [], structured: true });
    }).then(fn => { if (!cancelled) unlistenTypedPlan = fn; else fn(); });

    listen<KimStepPayload>(KimEventNames.STEP, e => {
      if (!belongsToView(e.payload.session_id)) return;
      setTypedLivePlan(prev => {
        if (!prev) return prev;
        const activeStep = Math.max(0, Math.min(e.payload.n, prev.steps.length));
        if (activeStep === prev.activeStep) return prev;
        return { ...prev, activeStep };
      });
    }).then(fn => { if (!cancelled) unlistenTypedStep = fn; else fn(); });

    listen<KimDonePayload>(KimEventNames.DONE, e => {
      if (!belongsToView(e.payload.session_id)) return;
      setTypedLivePlan(prev => {
        if (!prev) return prev;
        const doneSteps = prev.doneSteps.includes(e.payload.n)
          ? prev.doneSteps
          : [...prev.doneSteps, e.payload.n].sort((a, b) => a - b);
        const activeStep = Math.max(prev.activeStep, e.payload.n);
        if (activeStep === prev.activeStep && doneSteps.length === prev.doneSteps.length) return prev;
        return { ...prev, activeStep, doneSteps };
      });
    }).then(fn => { if (!cancelled) unlistenTypedDone = fn; else fn(); });

    listen<KimContextPayload>(KimEventNames.CONTEXT, e => {
      if (!belongsToView(e.payload.session_id)) return;
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

    listen<KimStatsPayload>(KimEventNames.STATS, e => {
      if (!belongsToView(e.payload.session_id)) return;
      setTokenStats({ input: e.payload.input, output: e.payload.output, total: e.payload.total });
    }).then(fn => { if (!cancelled) unlistenTypedStats = fn; else fn(); });

    // Typed run lifecycle events — capture-only in current slice.
    // terminationReasonRef gives the kim-agent-done handler richer context;
    // exposing it to the UI (e.g. a termination-specific banner) is the
    // remaining Tier 2b gap documented in docs/archive/HARNESS_ROADMAP.md.
    listen<KimRunDonePayload>(KimEventNames.RUN_DONE, e => {
      if (!belongsToView(e.payload.session_id)) return;
      terminationReasonRef.current = e.payload.termination;
    }).then(fn => { if (!cancelled) unlistenTypedRunDone = fn; else fn(); });

    // Capture provider error code for future UI surfacing (Tier 2e remaining gap).
    // Not surfaced via setTaskError here because the taskError string is rendered
    // raw by StreamRenderer when !== 'agent-error'; a dedicated banner slot is
    // needed before a structured code can be shown to the user.
    listen<KimProviderErrorPayload>(KimEventNames.PROVIDER_ERROR, e => {
      if (!belongsToView(e.payload.session_id)) return;
      lastProviderErrorCodeRef.current = e.payload.code;
    }).then(fn => { if (!cancelled) unlistenTypedProviderError = fn; else fn(); });

    // HITL approval visibility. This is display-only for now; the approval
    // decision path still lives in the agent/UIBridge layer.
    listen<KimHitlApprovalRequestPayload>(KimEventNames.HITL_APPROVAL_REQUEST, e => {
      if (!belongsToView(e.payload.session_id)) return;
      setHitlApprovalStatus({
        tool: e.payload.tool,
        risk: e.payload.risk,
        reason: e.payload.reason,
        preview: e.payload.preview, // K6
        id: e.payload.id, // T1: echoed back on the decision for stale-approval voiding
        approved: null,
      });
    }).then(fn => { if (!cancelled) unlistenTypedHitlRequest = fn; else fn(); });

    listen<KimHitlApprovalResultPayload>(KimEventNames.HITL_APPROVAL_RESULT, e => {
      if (!belongsToView(e.payload.session_id)) return;
      setHitlApprovalStatus(prev => ({
        tool: e.payload.tool,
        risk: prev?.tool === e.payload.tool ? prev.risk : 'high',
        reason: prev?.tool === e.payload.tool ? prev.reason : 'approval_result',
        // Carry the preview over so the command/diff preview doesn't vanish the moment
        // the user clicks Approve/Deny (StreamRenderer only renders it when set).
        preview: prev?.tool === e.payload.tool ? prev.preview : undefined,
        id: prev?.tool === e.payload.tool ? prev.id : undefined,
        approved: e.payload.approved,
      }));
    }).then(fn => { if (!cancelled) unlistenTypedHitlResult = fn; else fn(); });

    // RUN-IDENTITY: kim-run-id now carries { run_id, session_id }. Only adopt it
    // as THIS view's run when the session matches — otherwise it's a run started
    // for a different session (foreign) and must not claim this view's pill/owner.
    listen<{ run_id: string; session_id: string }>(KIM_RUN_ID_EVENT, e => {
      const { run_id, session_id } = e.payload;
      if (!belongsToView(session_id)) return;
      currentRunIdRef.current = run_id;
      runOwnerSessionIdRef.current = session_id || activeResumeSessionIdRef.current;
      setLastRunId(run_id); // K1 (checkpoint revert)
    })
      .then(fn => { if (!cancelled) unlistenRunId = fn; else fn(); });

    listen<KimRunFailedPayload>(KimEventNames.RUN_FAILED, e => {
      if (!belongsToView(e.payload.session_id)) return;
      setRunFailure(e.payload);
    }).then(fn => { if (!cancelled) unlistenTypedRunFailed = fn; else fn(); });

    listen<KimRateLimitedPayload>(KimEventNames.RATE_LIMITED, e => {
      if (!belongsToView(e.payload.session_id)) return;
      setRateLimitedState(e.payload);
      // B9: cancel any prior auto-clear timer before scheduling a new one.
      clearRateLimitTimer();
      rateLimitTimerRef.current = window.setTimeout(() => {
        rateLimitTimerRef.current = null;
        setRateLimitedState(null);
      }, (e.payload.delay + 1) * 1000);
    }).then(fn => { if (!cancelled) unlistenTypedRateLimited = fn; else fn(); });

    listen<KimUiPayload>(KimEventNames.UI, e => {
      if (!belongsToView(e.payload.session_id)) return;
      if (e.payload.action === 'screenshot_flash' && isRunningRef.current) {
        invoke('show_screenshot_flash').catch(() => {});
        invoke('set_task_active_mode', { active: true }).catch(() => {});
      } else if (e.payload.action === 'show' && isRunningRef.current) {
        invoke('show_main_window').catch(() => {});
      }
    }).then(fn => { if (!cancelled) unlistenTypedUi = fn; else fn(); });

    listen<KimToolPayload>(KimEventNames.TOOL, e => {
      if (!belongsToView(e.payload.session_id)) return;
      const id = ++activityCounterRef.current;
      const def = TOOL_MAP[e.payload.name];
      const item: ActivityItem = {
        id,
        kind: 'tool',
        icon: def?.icon ?? '›',
        text: def ? def.label(e.payload.args) : `Using tool: \`${e.payload.name}\``,
      };
      if (isDuplicateActivityItem(item)) return;
      enqueueActivityUpdate(prev => {
        const next = [...prev, item];
        return next.length > MAX_ACTIVITY_ITEMS ? next.slice(-MAX_ACTIVITY_ITEMS) : next;
      });
    }).then(fn => { if (!cancelled) unlistenTypedTool = fn; else fn(); });

    listen<KimAnswerPayload>(KimEventNames.ANSWER, e => {
      if (!belongsToView(e.payload.session_id)) return;
      const text = e.payload.text.trim();
      if (!text) return;
      answerReceivedThisRunRef.current = true;
      // M7: if assistant deltas were streaming, the final answer REPLACES the
      // streaming bubble instead of appending a duplicate.
      const wasStreaming = streamingAnswerRef.current.active;
      resetDeltaStreams();
      setLiveHistory(prev => {
        if (wasStreaming) {
          for (let i = prev.length - 1; i >= 0; i--) {
            if (prev[i].role === 'assistant') {
              const next = [...prev];
              next[i] = { role: 'assistant', content: text };
              return next;
            }
          }
        }
        const last = prev[prev.length - 1];
        if (last?.role === 'assistant' && last.content.trim() === text) return prev;
        return [...prev, { role: 'assistant', content: text }];
      });
    }).then(fn => { if (!cancelled) unlistenTypedAnswer = fn; else fn(); });

    // M7: streamed assistant text (codex app-server transport) — previously no
    // listener existed and these events were dropped.
    listen<KimAssistantDeltaPayload>(KimEventNames.ASSISTANT_DELTA, e => {
      if (!belongsToView(e.payload.session_id)) return;
      const chunk = e.payload.chunk;
      if (!chunk) return;
      streamingAnswerRef.current.text += chunk;
      // A streamed answer counts as "answer received" so the success-line
      // fallback below doesn't append a duplicate bubble.
      if (streamingAnswerRef.current.text.trim()) answerReceivedThisRunRef.current = true;
      scheduleAssistantFlush();
    }).then(fn => { if (!cancelled) unlistenTypedAssistantDelta = fn; else fn(); });

    // M7: streamed reasoning — surface completed lines in the activity feed
    // (mirrors the raw path's per-line codex_reasoning handling).
    listen<KimReasoningDeltaPayload>(KimEventNames.REASONING_DELTA, e => {
      if (!belongsToView(e.payload.session_id)) return;
      reasoningBufRef.current += e.payload.chunk ?? '';
      let nl = reasoningBufRef.current.indexOf('\n');
      while (nl !== -1) {
        const lineText = reasoningBufRef.current.slice(0, nl).trim();
        reasoningBufRef.current = reasoningBufRef.current.slice(nl + 1);
        nl = reasoningBufRef.current.indexOf('\n');
        if (!lineText) continue;
        const id = ++activityCounterRef.current;
        enqueueActivityUpdate(prev => {
          const next = [...prev, { id, kind: 'tool' as const, icon: '💭', text: lineText }];
          return next.length > MAX_ACTIVITY_ITEMS ? next.slice(-MAX_ACTIVITY_ITEMS) : next;
        });
      }
    }).then(fn => { if (!cancelled) unlistenTypedReasoningDelta = fn; else fn(); });

    // M7: item lifecycle — show started commands / file changes in the feed.
    listen<KimItemLifecyclePayload>(KimEventNames.ITEM_LIFECYCLE, e => {
      if (!belongsToView(e.payload.session_id)) return;
      if (e.payload.phase !== 'started') return;
      const title = (e.payload.title ?? '').trim();
      let text: string | null = null;
      if (e.payload.kind === 'commandExecution') {
        text = title ? `Running \`${title.length > 80 ? `${title.slice(0, 80)}…` : title}\`` : 'Running a command';
      } else if (e.payload.kind === 'fileChange') {
        text = title ? `Editing ${title}` : 'Editing files';
      } else if (e.payload.kind === 'webSearch') {
        text = title ? `Searching the web for "${title}"` : 'Searching the web';
      }
      if (!text) return;
      const item: ActivityItem = { id: ++activityCounterRef.current, kind: 'tool', icon: '⚡', text };
      if (isDuplicateActivityItem(item)) return;
      enqueueActivityUpdate(prev => {
        const next = [...prev, item];
        return next.length > MAX_ACTIVITY_ITEMS ? next.slice(-MAX_ACTIVITY_ITEMS) : next;
      });
    }).then(fn => { if (!cancelled) unlistenTypedItemLifecycle = fn; else fn(); });

    listen<KimDiffPayload>(KimEventNames.DIFF, e => {
      if (!belongsToView(e.payload.session_id)) return;
      enqueueActivityUpdate(prev => {
        if (prev.length === 0) return prev;
        const last = prev[prev.length - 1];
        if (last.kind !== 'tool' || !/(Editing|Writing|Creating)/.test(last.text)) return prev;
        return [
          ...prev.slice(0, -1),
          { ...last, text: `${last.text} +${e.payload.added} -${e.payload.removed}` },
        ];
      });
    }).then(fn => { if (!cancelled) unlistenTypedDiff = fn; else fn(); });

    listen<KimActivityPayload>(KimEventNames.ACTIVITY, e => {
      if (!belongsToView(e.payload.session_id)) return;
      if (e.payload.kind === 'error') {
        const message = friendlyError(e.payload.text);
        setTaskError(message);
        needHelpFlagRef.current = true;
        if (lastRunTaskRef.current) setLastFailedTask(lastRunTaskRef.current);
        appendTypedActivity('error', message);
      } else if (e.payload.kind === 'success') {
        appendTypedActivity('success', e.payload.text);
        // M8: raw-path parity — when a run ends with a [SUCCESS] line but no
        // ANSWER ever arrived, the summary must still become an assistant
        // bubble (otherwise a success-without-answer stream renders nothing).
        const successText = e.payload.text.trim();
        const genericSuccess = /^Task completed(?: successfully)?$/i.test(successText);
        if (successText && !genericSuccess && !answerReceivedThisRunRef.current) {
          setLiveHistory(prev => {
            const last = prev[prev.length - 1];
            if (last?.role === 'assistant' && last.content.trim() === successText) return prev;
            return [...prev, { role: 'assistant', content: successText }];
          });
        }
      } else {
        setTypedStatus(e.payload.text);
        appendTypedActivity('status', e.payload.text);
      }
    }).then(fn => { if (!cancelled) unlistenTypedActivity = fn; else fn(); });

    listen<string>('kim-agent-output', event => {
      appendRaw(event.payload);
    }).then(fn => { if (!cancelled) unlistenOutput = fn; else fn(); });

    listen<string>('kim-agent-error', event => {
      appendRaw(`[err] ${event.payload}`);
    }).then(fn => { if (!cancelled) unlistenError = fn; else fn(); });

    listen<boolean>('kim-agent-done', event => {
      invoke('set_task_active_mode', { active: false }).catch(() => {});
      // RUN-IDENTITY (B4/B5/D1): kim-agent-done is a global signal and only ONE
      // run is ever active. A view that doesn't own the run (e.g. a New-Chat or
      // other-session view mounted after the user switched away mid-run) must
      // NOT react — otherwise it flips its own state, files the run's activity
      // under the wrong session, or raises an error banner for a foreign run.
      const ownsRun = isRunningRef.current || runOwnerSessionIdRef.current !== null;
      if (!ownsRun) return;
      // M7: land any still-buffered streamed assistant text, then reset the
      // delta stream state for the next run.
      if (assistantFlushTimerRef.current !== null) {
        window.clearTimeout(assistantFlushTimerRef.current);
        assistantFlushTimerRef.current = null;
        flushAssistantDelta();
      }
      resetDeltaStreams();
      const wasCancelled = cancelFlagRef.current;
      const hadNeedHelp = needHelpFlagRef.current;
      doneHandledRef.current = true;
      // NOTE: do NOT reset cancelFlagRef.current here. On a user cancel the
      // backend often emits kim-agent-done before kim-agent-cancelled, and React
      // batches this callback's state updates so useTaskRunner's queue-drain
      // passive effect only runs after this callback returns. Clearing the ref
      // now would make that effect read `false` and auto-run the next queued
      // task, defeating Stop. The flag is reset by the next runPendingTask().
      // L1: don't clobber a cancel that already happened — kim-agent-done often
      // arrives AFTER kim-agent-cancelled set isCancelled=true.
      setIsCancelled(wasCancelled);
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
        // RUN-IDENTITY: file under the session the RUN belongs to (captured at
        // spawn / re-attach), never under whatever session is on screen now.
        const sid =
          completedCodeSession?.session_id ??
          runOwnerSessionIdRef.current ??
          activeResumeSessionIdRef.current;
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
      const completedSessionId =
        completedCodeSession?.session_id ??
        runOwnerSessionIdRef.current ??
        activeResumeSessionIdRef.current;
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
      } else if (event.payload) {
        // B2/C15: a transient mid-run error (recoverable stderr line, retried
        // provider hiccup) used to leave a PERMANENT red Retry banner over a
        // successful answer because hadNeedHelp skipped this cleanup. When the
        // run succeeds and an answer actually arrived, the error was recovered
        // — clear it. Only a needs-help run WITHOUT an answer keeps its banner
        // (that banner is the agent's actual question to the user).
        if (!hadNeedHelp || answerReceivedThisRunRef.current) {
          setLastFailedTask(null);
          setTaskError(null);
        }
      }
      currentTaskRef.current = null;
      // RUN-IDENTITY: this run is finished — release ownership so a later
      // foreign done can't be mistaken for this view's run.
      runOwnerSessionIdRef.current = null;
      currentRunIdRef.current = null;
    }).then(fn => { if (!cancelled) unlistenDone = fn; else fn(); });

    listen<SessionInfo>('kim-agent-code-session', event => {
      completedCodeSessionRef.current = event.payload;
    }).then(fn => { if (!cancelled) unlistenCodeSession = fn; else fn(); });

    listen<boolean>('kim-agent-cancelled', () => {
      invoke('set_task_active_mode', { active: false }).catch(() => {});
      // RUN-IDENTITY: only the owning view reacts to the global cancel signal.
      if (!isRunningRef.current && runOwnerSessionIdRef.current === null) return;
      resetDeltaStreams(); // M7: drop buffered deltas from the cancelled run
      cancelFlagRef.current = true;
      setIsCancelled(true); // Fix 4: keep state in sync with ref
      appendRaw('⏹ Task cancelled');
      setIsRunning(false);
      setCancelling(false);
      setHitlApprovalStatus(null);
      currentTaskRef.current = null;
      runOwnerSessionIdRef.current = null;
      currentRunIdRef.current = null;
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
      unlistenTypedStatus?.();
      unlistenTypedPlan?.();
      unlistenTypedStep?.();
      unlistenTypedDone?.();
      unlistenTypedTool?.();
      unlistenTypedAnswer?.();
      unlistenTypedDiff?.();
      unlistenTypedActivity?.();
      unlistenTypedAssistantDelta?.();
      unlistenTypedReasoningDelta?.();
      unlistenTypedItemLifecycle?.();
      unlistenRunId?.();
      // M7: cancel a pending assistant-delta flush on teardown.
      if (assistantFlushTimerRef.current !== null) {
        window.clearTimeout(assistantFlushTimerRef.current);
        assistantFlushTimerRef.current = null;
      }
    };
    // commitCurrentBrowserUrl intentionally omitted — accessed via ref so this effect
    // registers listeners once and doesn't re-run (and leak handlers) on session switch.
  }, [appendRaw, appendTypedActivity, enqueueActivityUpdate, flushActivityNow, clearActivityNow, isDuplicateActivityItem, setMessageReloadNonce, flushAssistantDelta, resetDeltaStreams, scheduleAssistantFlush, belongsToView]);

  // Derived state to satisfy Prompt 8 explicit signature.
  // #34: prefer the typed kim:plan-driven plan, but fall back to parsing the
  // legacy activity stream so runs whose plan arrives only as text (codex and
  // kimctl-bridge streams that don't emit typed kim:plan) still render a live
  // plan checklist instead of nothing.
  // L1: memoized — both functions are O(N) regex passes that used to run on
  // EVERY ChatView render (~20x/sec during a run) for values with no consumer
  // outside this hook's public API / tests.
  const livePlan = useMemo(
    () => typedLivePlan ?? parsePlanFromActivity(activity),
    [typedLivePlan, activity],
  );
  const traceItems = useMemo(() => buildThinkingTrace(activity, livePlan), [activity, livePlan]);
  const planSteps = livePlan?.steps ?? [];
  const activityEntries = activity;
  const lastStatus = typedStatus || (activity.filter(a => a.kind === 'status').slice(-1)[0]?.text ?? '');
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
    // RUN-IDENTITY: owner captured at spawn by useTaskRunner so a mid-run switch
    // files this run's output under its own session, not the on-screen one.
    runOwnerSessionIdRef,
    currentRunIdRef,
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

    // HITL approval round-trip. `decision` carries the K1 vocabulary
    // (accept | acceptForSession | decline); when omitted, it is derived
    // from `approved` so existing call sites keep working.
    hitlRespond: (approved: boolean, decision?: 'accept' | 'acceptForSession' | 'decline') => {
      // A3/B10: disable the card as soon as a decision is clicked. On paths
      // that never echo hitl-approval-result (codex bridge gate), the card
      // stayed live and a SECOND click buffered a stdin decision line that the
      // NEXT gate consumed — silently authorizing an unseen action.
      setHitlApprovalStatus(prev => (prev ? { ...prev, decisionSent: true } : prev));
      return invoke('hitl_respond_approval', {
        approved,
        decision: decision ?? (approved ? 'accept' : 'decline'),
        // T1: echo the pending request's id so Python only applies a decision
        // matching the current prompt (a late click on a timed-out prompt is voided).
        id: hitlApprovalStatus?.id,
      }).catch(() => {
        // M4: clicking Approve/Deny used to silently no-op when the invoke
        // rejected (dead run, IPC error) — card stayed pending, agent blocked.
        // Re-enable the buttons: the decision never reached the agent.
        setHitlApprovalStatus(prev => (prev ? { ...prev, decisionSent: false } : prev));
        toast('Could not send your approval decision — the run may have ended.', 'error', 4000);
      });
    },
  };
}

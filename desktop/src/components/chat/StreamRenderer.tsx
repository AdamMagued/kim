import { useMemo, useCallback, useRef } from 'react';
import type { RefObject } from 'react';
import type { ActivityItem, CodexRunGroup, TouchedFile, PendingTask, HitlApprovalStatus } from './types';
import type { KimMessage, Settings, KimAccount, PermissionMode } from '../../types';
import { MessageBubble } from '../MessageBubble';
import { SignalCard } from '../ToolCallCard';
import { WorkedForPill } from '../kim-ui';
import {
  formatDuration,
  parsePlanFromActivity,
  collapseMessages,
  isRealUserMessage,
  isIntermediateToolCall,
  synthesizeExchangeActivity,
  basename,
  getGreeting,
  providerLabel,
  estimateCostUsd,
  formatCostUsd,
  costBasisLabel,
} from './utils';
import { buildThinkingTrace, traceToWorkedFor } from './parsers';
import { ActivityFeed } from './ActivityFeed';
import { CodexTurnPanel, HitlStatusCard } from './CodexTurnPanel';
import { ContextMeter, type ContextMeterState } from './ContextMeter';
import type { CodexTurnState } from '../../hooks/useCodexTurn';

// ── Blobby Loader Component ──────────────────────────────────────────────────

function BlobLoader({ which }: { which: 3 | 6 | 12 | 15 | 20 }) {
  if (which === 3) {
    return (
      <svg viewBox="0 0 100 100" className="kim-blob-loader kim-blob-l3" aria-hidden="true">
        <path d="M50,12 C74,12 92,30 88,54 C84,78 64,90 44,86 C20,82 8,60 14,38 C20,20 34,12 50,12 Z" fill="currentColor" />
      </svg>
    );
  }
  if (which === 6) {
    return (
      <svg viewBox="0 0 100 100" className="kim-blob-loader kim-blob-l6" aria-hidden="true">
        <g style={{ filter: 'url(#kim-goo)' }}>
          <circle className="kim-blob-l6__a" cx="50" cy="50" r="18" fill="currentColor" />
          <circle className="kim-blob-l6__b" cx="50" cy="50" r="18" fill="currentColor" />
        </g>
      </svg>
    );
  }
  if (which === 12) {
    return (
      <svg viewBox="0 0 100 100" className="kim-blob-loader kim-blob-l12" aria-hidden="true">
        <rect className="kim-blob-l12__pill" x="20" y="35" width="60" height="30" rx="15" fill="currentColor" />
      </svg>
    );
  }
  if (which === 15) {
    return (
      <svg viewBox="0 0 100 100" className="kim-blob-loader kim-blob-l15" aria-hidden="true">
        <g style={{ filter: 'url(#kim-goo)' }}>
          <circle className="kim-blob-l15__d1" cx="50" cy="50" r="13" fill="currentColor" />
          <circle className="kim-blob-l15__d2" cx="50" cy="50" r="13" fill="currentColor" />
        </g>
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 100 100" className="kim-blob-loader kim-blob-l20" aria-hidden="true">
      <g style={{ filter: 'url(#kim-goo)' }}>
        <circle className="kim-blob-l20__a" cx="50" cy="50" r="18" fill="currentColor" />
        <circle className="kim-blob-l20__b" cx="50" cy="50" r="18" fill="currentColor" />
      </g>
    </svg>
  );
}

// ── StreamRenderer Props ─────────────────────────────────────────────────────

export interface StreamRendererProps {
  messages: KimMessage[];
  loadingMessages: boolean;
  /** M17: set when the session file couldn't be read — rendered instead of the
   *  bare "No messages in this session" empty state. */
  loadError?: string | null;
  liveHistory: { role: 'user' | 'assistant'; content: string }[];
  runHistory: { activity: ActivityItem[]; durationSec: number; provider?: string | null }[];
  codexRuns: CodexRunGroup[];
  taskError: string | null;
  hitlApprovalStatus: HitlApprovalStatus | null;
  // M3: carries the full decision vocabulary so the legacy HITL card can send
  // acceptForSession (the narrowed `(approved: boolean) => void` type silently
  // dropped the decision param the hook already supports).
  onHitlRespond?: (approved: boolean, decision?: 'accept' | 'acceptForSession' | 'decline') => void;
  codexTurn?: CodexTurnState | null;
  permissionMode?: PermissionMode;
  onPermissionModeChange?: (mode: PermissionMode) => void;
  runFailure?: { reason: string; recoverable: boolean; suggestion: string } | null;
  rateLimitedState?: { delay: number; attempt: number; max_retries: number } | null;
  settings: Settings;
  newChatMode: boolean;
  activity: ActivityItem[];
  isRunning: boolean;
  autoFollowOutput: boolean;
  setAutoFollowOutput: (val: boolean) => void;
  bottomRef: RefObject<HTMLDivElement | null>;
  /** H1: attached to `.kim-messages` so useSessionScroll can detect the user
   *  scrolling up (was created but never attached — auto-follow was forced). */
  outputRef?: RefObject<HTMLDivElement | null>;
  newestMsgIdx: number | null;
  queuedTasks: PendingTask[];
  lastRunTask: PendingTask | null;
  elapsed: number;
  handleRetryLast: () => void;
  handleEditLiveMessage: (idx: number, newText: string) => void;
  empty: boolean;
  renderComposer: (heroMode?: boolean) => React.ReactNode;
  renderConnectorsChrome: () => React.ReactNode;
  account: KimAccount;
  activeTab: 'chat' | 'code';
  activeProjectPath?: string | null;
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  setTaskInput: (val: string) => void;
  resolveProvider: () => string;
  tokenStats?: { input: number; output: number; total: number } | null;
  /** A1/D-A1: context-usage state from the kim:context pipeline. Rendered as
   *  the ContextMeter gauge above the composer (previously plumbed nowhere). */
  contextState?: ContextMeterState | null;
}

// ── Component ────────────────────────────────────────────────────────────────

export function StreamRenderer({
  messages,
  loadingMessages,
  loadError,
  liveHistory,
  runHistory,
  codexRuns,
  taskError,
  hitlApprovalStatus,
  onHitlRespond,
  codexTurn,
  permissionMode,
  onPermissionModeChange,
  runFailure,
  rateLimitedState,
  settings,
  newChatMode,
  activity,
  isRunning,
  autoFollowOutput,
  setAutoFollowOutput,
  bottomRef,
  outputRef,
  newestMsgIdx,
  queuedTasks,
  lastRunTask,
  elapsed,
  handleRetryLast,
  handleEditLiveMessage,
  empty,
  renderComposer,
  renderConnectorsChrome,
  account,
  activeTab,
  activeProjectPath,
  textareaRef,
  setTaskInput,
  resolveProvider,
  tokenStats,
  contextState,
}: StreamRendererProps) {

  // ── Memoized derived values ────────────────────────────────────────────────
  // collapseMessages is O(N) with regex+JSON.parse — memoize so it only runs
  // when the underlying array reference changes (liveHistory/messages are
  // useState values in useChatStream / useSessionLoader; they're stable across
  // activity/elapsed ticks that drive the ~20x/sec re-render rate).

  const collapsedLive = useMemo(() => collapseMessages(liveHistory), [liveHistory]);
  const collapsedSaved = useMemo(() => collapseMessages(messages), [messages]);

  // Index of the last non-intermediate assistant message in liveHistory.
  // Precomputed in O(N) so the two render branches can replace the O(N²)
  // `collapsed.slice(i+1).some(...)` with a simple index comparison.
  const lastNonIntermAsstIdxLive = useMemo(() => {
    for (let i = collapsedLive.length - 1; i >= 0; i--) {
      const { msg } = collapsedLive[i];
      if (msg.role === 'assistant' && !isIntermediateToolCall(msg)) return i;
    }
    return -1;
  }, [collapsedLive]);

  // Counts consumed by the session branch to offset live vs. saved indices.
  const liveAsstCount = useMemo(
    () => collapsedLive.filter(({ msg }) => msg.role === 'assistant' && !isIntermediateToolCall(msg)).length,
    [collapsedLive],
  );
  const savedAsstCount = useMemo(
    () => collapsedSaved.filter(({ msg }) => msg.role === 'assistant' && !isIntermediateToolCall(msg)).length,
    [collapsedSaved],
  );
  const savedUserCount = useMemo(() => messages.filter(isRealUserMessage).length, [messages]);

  // Cache synthesizeExchangeActivity results (O(N) calls × O(N) function =
  // O(N²) per messages change, but NOT per render — the critical reduction).
  const synthActivityMap = useMemo(() => {
    const map = new Map<number, ActivityItem[]>();
    let userIdx = -1;
    for (const { msg } of collapsedSaved) {
      if (msg.role === 'user' && isRealUserMessage(msg)) userIdx++;
      if (msg.role === 'assistant' && !isIntermediateToolCall(msg) && !map.has(userIdx)) {
        const synth = synthesizeExchangeActivity(messages, userIdx);
        if (synth.length > 0) map.set(userIdx, synth);
      }
    }
    return map;
  }, [collapsedSaved, messages]);

  // ── Stable callback refs ─────────────────────────────────────────────────────
  // handleRetryLast and handleEditLiveMessage are plain arrow functions in their
  // parent hooks (not wrapped in useCallback), so they arrive as new references
  // on every render. Wrapping them in refs gives stable identities for the
  // live-message-list useMemo deps — otherwise the memo would recompute on
  // every activity/elapsed tick (~20x/sec) and defeat the optimisation entirely.
  const _retryRef = useRef(handleRetryLast);
  _retryRef.current = handleRetryLast;
  const stableRetry = useCallback(() => _retryRef.current(), []);

  const _editRef = useRef(handleEditLiveMessage);
  _editRef.current = handleEditLiveMessage;
  const stableEdit = useCallback((idx: number, newText: string) => _editRef.current(idx, newText), []);

  // renderWorkedFor as a stable useCallback so it can be a dep of the live-msg
  // memos below. The only non-constant value it closes over is tokenStats.
  const renderWorkedFor = useCallback((_idx: number, run: { activity: ActivityItem[]; durationSec: number; provider?: string | null }, showCost = false) => {
    const historyTrace = buildThinkingTrace(run.activity, parsePlanFromActivity(run.activity));
    const workedForTrace = traceToWorkedFor(historyTrace);
    // Nothing meaningful happened (e.g. a plain conversational reply — no tools,
    // no real reasoning steps): don't show an empty "Reasoning trace" panel.
    if (workedForTrace.length === 0) return null;
    const duration = run.durationSec > 0 ? formatDuration(run.durationSec) : '…';
    // B5: price with the provider THIS run used (not the currently-selected one).
    const runProvider = run.provider ?? null;
    const costUsd = showCost && tokenStats && runProvider
      ? estimateCostUsd(runProvider, tokenStats.input, tokenStats.output)
      : null;
    return (
      <div className="kim-msg-row kim-msg-row--assistant" style={{ flexDirection: 'column', alignItems: 'flex-start', gap: 6 }}>
        <WorkedForPill trace={workedForTrace} duration={duration} />
        {costUsd !== null && (
          <span
            title={`~${formatCostUsd(costUsd)} — ${costBasisLabel(runProvider ?? '') ?? 'estimated'} · ${tokenStats?.input.toLocaleString()} in / ${tokenStats?.output.toLocaleString()} out tokens`}
            style={{
              fontFamily: 'var(--kim-mono)',
              fontSize: 11.5,
              color: costUsd === 0 ? 'var(--kim-text-4)' : 'var(--kim-text-3)',
              background: 'var(--kim-surface)',
              border: '1px solid var(--kim-border)',
              borderRadius: 6,
              padding: '2px 7px',
              cursor: 'default',
              userSelect: 'none',
            }}
          >
            {costUsd === 0 ? 'local · $0' : `~${formatCostUsd(costUsd)}`}
          </span>
        )}
      </div>
    );
  }, [tokenStats]);

  // showLiveActivity: true when the last real user message in collapsedLive has
  // no non-intermediate assistant reply after it. Stable during activity/elapsed
  // ticks because it only depends on collapsedLive and lastNonIntermAsstIdxLive.
  const showLiveActivity = useMemo(() => {
    for (let i = collapsedLive.length - 1; i >= 0; i--) {
      const { msg } = collapsedLive[i];
      if (msg.role === 'user' && isRealUserMessage(msg)) {
        return i > lastNonIntermAsstIdxLive;
      }
    }
    return false;
  }, [collapsedLive, lastNonIntermAsstIdxLive]);

  // Memoized live message node lists. ActivityFeed is intentionally excluded
  // from both (rendered separately via showLiveActivity below each list) so
  // that the ~20x/sec activity/elapsed ticks do NOT cause the message-list JSX
  // to rebuild — only ActivityFeed itself re-renders on those ticks.
  //
  // All deps here are stable across activity/elapsed ticks:
  //   collapsedLive / runHistory — useState refs, only update on message events
  //   settings.typing_animation  — primitive string
  //   stableRetry / stableEdit   — useCallback with empty deps (ref-backed)
  //   renderWorkedFor            — useCallback whose only dep is tokenStats
  const liveMsgNodesNewChat = useMemo(() => {
    let liveUserIdx = -1;
    let liveAsstRunIdx = -1;
    return collapsedLive.map(({ msg, retries, srcIdx }, i) => {
      if (msg.role === 'user' && isRealUserMessage(msg)) liveUserIdx += 1;
      if (isIntermediateToolCall(msg)) return null;
      let workedRun: { activity: ActivityItem[]; durationSec: number; provider?: string | null } | null = null;
      let workedRunIsLast = false;
      if (msg.role === 'assistant') {
        liveAsstRunIdx += 1;
        workedRun = runHistory[liveAsstRunIdx] ?? null;
        workedRunIsLast = liveAsstRunIdx === runHistory.length - 1;
      }
      return (
        <div key={`live-${srcIdx}`}>
          {workedRun && renderWorkedFor(liveUserIdx, workedRun, workedRunIsLast)}
          <MessageBubble
            message={msg}
            animate={i === collapsedLive.length - 1}
            typingAnimation={settings.typing_animation ?? 'none'}
            onRetry={stableRetry}
            retries={retries}
            onEdit={msg.role === 'user' ? (newText) => stableEdit(srcIdx, newText) : undefined}
          />
        </div>
      );
    });
  }, [collapsedLive, runHistory, settings.typing_animation, stableRetry, stableEdit, renderWorkedFor]);

  // Session-branch variant: live messages are offset by saved message counts.
  const liveMsgNodesSession = useMemo(() => {
    let liveUserMsgIdx = savedUserCount - 1;
    let liveAsstIdx = savedAsstCount;
    return collapsedLive.map(({ msg, retries, srcIdx }, i) => {
      if (msg.role === 'user' && isRealUserMessage(msg)) liveUserMsgIdx += 1;
      if (isIntermediateToolCall(msg)) return null;
      let workedRun: { activity: ActivityItem[]; durationSec: number; provider?: string | null } | null = null;
      let workedRunIsLast2 = false;
      if (msg.role === 'assistant') {
        workedRun = runHistory[liveAsstIdx] ?? null;
        workedRunIsLast2 = liveAsstIdx === runHistory.length - 1;
        liveAsstIdx += 1;
      }
      return (
        <div key={`live-${srcIdx}`}>
          {workedRun && renderWorkedFor(liveUserMsgIdx, workedRun, workedRunIsLast2)}
          <MessageBubble
            message={msg}
            animate={i === collapsedLive.length - 1}
            typingAnimation={settings.typing_animation ?? 'none'}
            onRetry={stableRetry}
            retries={retries}
            onEdit={msg.role === 'user' ? (newText) => stableEdit(srcIdx, newText) : undefined}
          />
        </div>
      );
    });
  }, [collapsedLive, runHistory, savedUserCount, savedAsstCount, settings.typing_animation, stableRetry, stableEdit, renderWorkedFor]);

  // L2: session-branch saved-message nodes, memoized like the live lists so
  // the O(msgs × activity) regex work in renderWorkedFor doesn't re-run on
  // every 50ms activity flush / 1s elapsed tick in long sessions.
  const savedMsgNodes = useMemo(() => {
    let userMsgIdx = -1;
    return collapsedSaved.map(({ msg, retries, srcIdx }, i) => {
      if (msg.role === 'user' && isRealUserMessage(msg)) userMsgIdx += 1;
      if (isIntermediateToolCall(msg)) return null;

      let workedRun: { activity: ActivityItem[]; durationSec: number; provider?: string | null } | null = null;
      if (msg.role === 'assistant') {
        const savedIdx = userMsgIdx - liveAsstCount;
        workedRun = runHistory[savedIdx] ?? null;
        if (!workedRun) {
          // Use precomputed map — avoids re-calling synthesizeExchangeActivity per render
          const synth = synthActivityMap.get(userMsgIdx) ?? null;
          if (synth) workedRun = { activity: synth, durationSec: 0 };
        }
      }

      return (
        <div key={`msg-${srcIdx}`}>
          {workedRun && renderWorkedFor(userMsgIdx, workedRun)}
          <MessageBubble
            message={msg}
            animate={i === newestMsgIdx}
            typingAnimation={settings.typing_animation ?? 'none'}
            onRetry={stableRetry}
            retries={retries}
          />
        </div>
      );
    });
  }, [collapsedSaved, runHistory, liveAsstCount, synthActivityMap, newestMsgIdx, settings.typing_animation, stableRetry, renderWorkedFor]);

  // ── Render Helpers ─────────────────────────────────────────────────────────

  function renderHitlStatus() {
    // Approval UI lives in CodexTurnPanel.tsx; this renders BOTH the legacy
    // task-level HITL card and the codex app-server turn panel (native
    // per-command approvals, plan, live output, diff).
    if (!hitlApprovalStatus && !codexTurn) return null;
    return (
      <>
        {hitlApprovalStatus && (
          <div className="kim-msg-row kim-msg-row--assistant">
            <HitlStatusCard status={hitlApprovalStatus} onRespond={onHitlRespond} />
          </div>
        )}
        {codexTurn && <CodexTurnPanel turn={codexTurn} />}
      </>
    );
  }

  function renderPermissionToggle() {
    if (!onPermissionModeChange) return null;
    const modes: { value: PermissionMode; label: string }[] = [
      { value: 'full_auto', label: 'Full auto' },
      { value: 'ask_risky', label: 'Ask risky' },
      { value: 'ask_always', label: 'Ask always' },
    ];
    const current = permissionMode ?? 'full_auto';
    return (
      <div className="kim-permission-toggle" role="group" aria-label="Permission mode">
        {modes.map(m => (
          <button
            key={m.value}
            className={`kim-permission-toggle__btn${current === m.value ? ' kim-permission-toggle__btn--active' : ''}`}
            onClick={() => onPermissionModeChange(m.value)}
            aria-pressed={current === m.value}
          >
            {m.label}
          </button>
        ))}
      </div>
    );
  }

  // B3: render the raw 'agent-error' sentinel as a friendly message everywhere
  // (previously only the new-chat branch did; the session branch leaked it raw).
  function friendlyTaskError(err: string): string {
    return err === 'agent-error'
      ? 'Kim ran into a problem and had to stop. Check the activity above for clues, or try rephrasing your task.'
      : err;
  }

  // B4: structured run-failure card — shared by both branches.
  function renderRunFailure() {
    if (!runFailure || isRunning) return null;
    return (
      <div className="kim-msg-row kim-msg-row--assistant">
        <div
          className="kim-run-failed-card"
          role="alert"
          style={{
            background: 'var(--kim-surface)',
            border: '1px solid var(--kim-red, #e05c5c)',
            borderRadius: 12,
            padding: '14px 16px',
            display: 'flex',
            flexDirection: 'column',
            gap: 8,
            maxWidth: 520,
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ color: 'var(--kim-red, #e05c5c)', fontSize: 16 }}>✕</span>
            <span style={{ fontWeight: 500, fontSize: 13.5 }}>
              {runFailure.reason === 'max_iterations'
                ? 'Iteration limit reached'
                : runFailure.reason === 'stuck'
                ? 'Kim got stuck'
                : runFailure.reason === 'need_help'
                ? 'Kim needs help'
                : runFailure.reason === 'conversational_loop'
                ? 'Conversational loop detected'
                : 'Task failed'}
            </span>
          </div>
          <div style={{ fontSize: 12.5, color: 'var(--kim-text-2)', lineHeight: 1.55 }}>
            {runFailure.suggestion}
          </div>
          {runFailure.recoverable && lastRunTask && (
            <div style={{ display: 'flex', gap: 8, marginTop: 4 }}>
              <button
                type="button"
                className="kr-btn kr-btn-primary"
                style={{ fontSize: 12, padding: '6px 12px' }}
                onClick={() => void handleRetryLast()}
              >
                Retry
              </button>
            </div>
          )}
        </div>
      </div>
    );
  }

  // B4: rate-limited backoff banner — shared by both branches.
  function renderRateLimited() {
    if (!rateLimitedState || !isRunning) return null;
    return (
      <div className="kim-msg-row kim-msg-row--assistant">
        <div
          style={{
            background: 'var(--kim-surface)',
            border: '1px solid var(--kim-border)',
            borderRadius: 10,
            padding: '10px 14px',
            fontSize: 12.5,
            color: 'var(--kim-text-2)',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            maxWidth: 400,
          }}
        >
          <span style={{ fontSize: 14 }}>⏳</span>
          <span>
            Rate-limited — retrying in {rateLimitedState.delay}s
            {rateLimitedState.attempt < rateLimitedState.max_retries
              ? ` (attempt ${rateLimitedState.attempt}/${rateLimitedState.max_retries})`
              : ''}
            …
          </span>
        </div>
      </div>
    );
  }

  function renderFilePills(files: TouchedFile[]) {
    if (files.length === 0) return null;
    return (
      <div className="kim-msg-row kim-msg-row--assistant">
        <div className="kim-file-pills">
          {/* L10: key by path — index keys mis-reconcile when the list mutates */}
          {files.map((f, i) => (
            <span key={f.path || i} className="kim-file-pill">
              <span className="kim-file-pill__name">{basename(f.path)}</span>
              {(f.added > 0 || f.removed > 0) && (
                <span className="kim-file-pill__stats">
                  {f.added > 0 && <span className="kim-file-pill__stat-add">+{f.added}</span>}
                  {f.removed > 0 && <span className="kim-file-pill__stat-del">-{f.removed}</span>}
                </span>
              )}
            </span>
          ))}
        </div>
      </div>
    );
  }

  // ── Render ─────────────────────────────────────────────────────────────────

  if (newChatMode) {
    const starters =
      activeTab === 'code'
        ? [
            ['◇', 'Read-only review'],
            ['↑', 'Edit + run tests'],
            ['→', 'Pick up last task'],
          ]
        : [
            ['✦', 'Catch me up on yesterday'],
            ['↳', 'Open my email'],
            ['◇', "What's on screen?"],
            ['→', 'Draft a quick reply'],
          ];

    const providerPillLabel =
      resolveProvider() === 'ollama'
        ? `Ollama · ${settings.ollama.mode}`
        : providerLabel(resolveProvider());

    return (
      <div className={`kim-chat${empty ? ' kim-chat--empty-hero' : ''}`}>
        {renderConnectorsChrome()}

        {/* B11: only the bottom sentinel gets bottomRef — binding it here too
            made the scroll target mount-order-dependent. */}
        <div className="kim-messages" ref={outputRef}>
          {empty && (
            <div
              style={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                width: '100%',
                maxWidth: 680,
                textAlign: 'center',
                margin: 'auto',
                padding: '40px 20px',
              }}
            >
              <div
                style={{
                  fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
                  fontSize: 11,
                  color: 'var(--kim-text-3)',
                  marginBottom: 18,
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 8,
                  padding: '6px 12px',
                  border: '1px solid var(--kim-border)',
                  borderRadius: 999,
                  background: 'var(--kim-surface)',
                }}
              >
                <span className="kr-pulse-dot" style={{ background: 'var(--kim-green)' }} />
                <span>{providerPillLabel}</span>
              </div>

              <h1
                style={{
                  fontWeight: 700,
                  fontSize: 38,
                  color: 'var(--kim-text)',
                  margin: '0 0 12px',
                  letterSpacing: '-0.02em',
                  lineHeight: 1.1,
                }}
              >
                {activeTab === 'code' && activeProjectPath
                  ? `What should we work on in ${activeProjectPath.split('/').filter(Boolean).pop() ?? 'this project'}?`
                  : activeTab === 'code' ? 'What are we building?'
                  : getGreeting(account.display_name.split(' ')[0])}
              </h1>
              <p
                style={{
                  color: 'var(--kim-text-3)',
                  fontSize: 14,
                  margin: 0,
                  marginBottom: 28,
                  maxWidth: 480,
                  lineHeight: 1.5,
                }}
              >
                {activeTab === 'code'
                  ? activeProjectPath
                    ? "Describe a feature, hand me a bug, or point me at a file. I'll read first, plan, then write."
                    : 'Create a new project or open a project folder before typing.'
                  : 'Pick up where you left off, or start fresh.'}
              </p>

              <div style={{ width: 'min(680px, 95%)' }}>
                {renderComposer(true)}
              </div>

              {renderPermissionToggle()}

              <div
                style={{
                  display: 'flex',
                  flexWrap: 'wrap',
                  gap: 8,
                  marginTop: 18,
                  justifyContent: 'center',
                  maxWidth: 680,
                }}
              >
                {starters.map(([k, t]) => (
                  <button
                    key={t}
                    type="button"
                    className="kr-btn"
                    disabled={activeTab === 'code' && !activeProjectPath}
                    onClick={() => {
                      setTaskInput(t);
                      textareaRef.current?.focus();
                    }}
                    style={{
                      background: 'transparent',
                      padding: '7px 12px',
                      fontSize: 12.5,
                      color: 'var(--kim-text-2)',
                    }}
                  >
                    <span style={{ color: 'var(--kim-accent)', fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace' }}>{k}</span>
                    <span>{t}</span>
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Live conversation history — memoized node list (stable across
              activity/elapsed ticks). ActivityFeed is a sibling, not embedded
              in the loop, so only it re-renders during the ~20x/sec ticks. */}
          {liveMsgNodesNewChat}
          {showLiveActivity && <ActivityFeed activity={activity} elapsed={elapsed} />}

          {renderHitlStatus()}

          {/* Error / retry — B4: suppress the legacy banner when the structured
              run-failure card is shown for the same run (avoid double surface).
              B2: never render the retry banner mid-run — a transient stderr line
              would otherwise show a red "Retry" over a task that is still working
              and inviting a duplicate-task retry. */}
          {taskError && !runFailure && !isRunning && (
            <div className="kim-msg-row kim-msg-row--assistant">
              <div className="kim-task-error" role="alert">
                <span className="kim-task-error__icon">⚠</span>
                <span>{friendlyTaskError(taskError)}</span>
                {lastRunTask && (
                  <button type="button" className="kim-task-error__retry" onClick={() => void handleRetryLast()}>
                    Retry
                  </button>
                )}
              </div>
            </div>
          )}

          {renderRunFailure()}
          {renderRateLimited()}

          {/* Queued messages: show each as a real (dimmed, pending) user bubble so the
              user sees what they sent immediately — not a bare count that pops the
              message + reply in together only once it actually runs. */}
          {queuedTasks.map((qt, qi) => (
            <div key={`queued-${qt.id}`} className="kim-queued-msg" style={{ opacity: 0.55 }}>
              <MessageBubble message={{ role: 'user', content: qt.text }} />
              <div className="kim-queue-indicator" role="status" aria-live="polite">
                {qi === 0
                  ? 'Queued — sends automatically when the current reply finishes'
                  : `Queued (#${qi + 1})`}
              </div>
            </div>
          ))}

          {!autoFollowOutput && (activity.length > 0 || isRunning) && (
            <div className="kim-jump-latest-wrap">
              <button
                type="button"
                className="kim-jump-latest-btn"
                onClick={() => {
                  setAutoFollowOutput(true);
                  bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
                }}
              >
                Jump to latest
              </button>
            </div>
          )}

          <div ref={bottomRef} />
        </div>

        {!empty && contextState && (
          <div className="kim-context-meter-wrap"><ContextMeter state={contextState} /></div>
        )}
        {!empty && renderPermissionToggle()}
        {!empty && renderComposer(false)}
      </div>
    );
  }

  // Existing session view
  return (
    <div className="kim-chat">
      {renderConnectorsChrome()}

      {/* Messages */}
      <div className="kim-messages" ref={outputRef}>
        {loadingMessages ? (
          <div className="kim-messages__loading">
            <svg width="0" height="0" style={{ position: 'absolute' }}>
              <defs>
                <filter id="kim-goo">
                  <feGaussianBlur in="SourceGraphic" stdDeviation="3" result="blur" />
                  <feColorMatrix in="blur" values="1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 0 20 -9" result="goo" />
                  <feComposite in="SourceGraphic" in2="goo" operator="atop" />
                </filter>
              </defs>
            </svg>
            <BlobLoader which={6} />
            <span>Loading conversation…</span>
          </div>
        ) : messages.length === 0 ? (
          <div className="kim-messages__empty">
            {/* M17: distinguish "couldn't read the file" from "genuinely empty" */}
            <div className="kim-messages__empty-text" role={loadError ? 'alert' : undefined}>
              {loadError ?? 'No messages in this session'}
            </div>
          </div>
        ) : (
          <>
            {codexRuns.length > 0 ? (
              /* Grouped Codex session view */
              codexRuns.map((run, runIdx) => (
                <div key={`codex-run-${runIdx}`}>
                  <MessageBubble
                    message={run.userMessage}
                    typingAnimation={settings.typing_animation ?? 'none'}
                    onRetry={handleRetryLast}
                  />
                  {run.intermediateActivity.length > 0 && renderWorkedFor(runIdx, {
                    activity: run.intermediateActivity,
                    durationSec: run.durationSec,
                  })}
                  {run.finalAssistantMessage && (
                    <MessageBubble
                      message={run.finalAssistantMessage}
                      animate={runIdx === codexRuns.length - 1}
                      typingAnimation={settings.typing_animation ?? 'none'}
                      onRetry={handleRetryLast}
                    />
                  )}
                  {renderFilePills(run.touchedFiles)}
                </div>
              ))
            ) : (
              /* Normal message view — L2: memoized node list (savedMsgNodes) */
              savedMsgNodes
            )}

            {/* Newly added messages in this session — same memo strategy:
                message nodes are stable across activity/elapsed ticks;
                ActivityFeed is rendered as a sibling so only it updates. */}
            {liveMsgNodesSession}
            {showLiveActivity && <ActivityFeed activity={activity} elapsed={elapsed} />}

            {renderHitlStatus()}

            {/* B4: render the structured failure + rate-limit surfaces in the
                session branch too (previously only the new-chat branch had them). */}
            {renderRunFailure()}
            {renderRateLimited()}

            {/* B3/B4: friendly agent-error text; suppressed when the structured
                run-failure card already explains the same run. B2: and never
                mid-run — a recovered stderr line must not show Resend Task over
                a task that is still working. */}
            {taskError && !runFailure && !isRunning && (
              <div className="kim-msg-row kim-msg-row--assistant">
                <div style={{ maxWidth: '78%', minWidth: 0 }}>
                  <SignalCard kind="error" text={friendlyTaskError(taskError)} onAction={handleRetryLast} actionLabel="Resend Task" />
                </div>
              </div>
            )}

            {/* H1: "Jump to latest" pill — the session branch never had one even
                though auto-follow can now be paused here too. */}
            {!autoFollowOutput && (activity.length > 0 || isRunning) && (
              <div className="kim-jump-latest-wrap">
                <button
                  type="button"
                  className="kim-jump-latest-btn"
                  onClick={() => {
                    setAutoFollowOutput(true);
                    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
                  }}
                >
                  Jump to latest
                </button>
              </div>
            )}
          </>
        )}
        <div ref={bottomRef} />
      </div>

      {contextState && (
        <div className="kim-context-meter-wrap"><ContextMeter state={contextState} /></div>
      )}
      {renderPermissionToggle()}
      {renderComposer()}
    </div>
  );
}

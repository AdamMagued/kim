import type { RefObject } from 'react';
import type { ActivityItem, CodexRunGroup, TouchedFile, PendingTask, HitlApprovalStatus } from './types';
import type { KimMessage, Settings, KimAccount } from '../../types';
import { MessageBubble } from '../MessageBubble';
import { SignalCard } from '../ToolCallCard';
import { ThinkingWithPlan, WorkedForPill } from '../kim-ui';
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
} from './utils';
import { buildThinkingTrace, traceToWorkedFor } from './parsers';

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
  liveHistory: { role: 'user' | 'assistant'; content: string }[];
  runHistory: { activity: ActivityItem[]; durationSec: number }[];
  codexRuns: CodexRunGroup[];
  taskError: string | null;
  hitlApprovalStatus: HitlApprovalStatus | null;
  runFailure?: { reason: string; recoverable: boolean; suggestion: string } | null;
  rateLimitedState?: { delay: number; attempt: number; max_retries: number } | null;
  settings: Settings;
  newChatMode: boolean;
  activity: ActivityItem[];
  isRunning: boolean;
  autoFollowOutput: boolean;
  setAutoFollowOutput: (val: boolean) => void;
  bottomRef: RefObject<HTMLDivElement | null>;
  newestMsgIdx: number | null;
  queuedTasks: PendingTask[];
  interruptTask: PendingTask | null;
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
}

// ── Component ────────────────────────────────────────────────────────────────

export function StreamRenderer({
  messages,
  loadingMessages,
  liveHistory,
  runHistory,
  codexRuns,
  taskError,
  hitlApprovalStatus,
  runFailure,
  rateLimitedState,
  settings,
  newChatMode,
  activity,
  isRunning,
  autoFollowOutput,
  setAutoFollowOutput,
  bottomRef,
  newestMsgIdx,
  queuedTasks,
  interruptTask,
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
}: StreamRendererProps) {

  // ── Render Helpers ─────────────────────────────────────────────────────────

  function renderActivityFeed() {
    if (activity.length === 0) return null;
    const livePlan = parsePlanFromActivity(activity);
    const toolCalls = activity.filter(a => a.kind === 'tool').length;
    const streamStepCount = Math.max(toolCalls, activity.length);
    const trace = buildThinkingTrace(activity, livePlan);

    return (
      <div className="kim-msg-row kim-msg-row--assistant kim-msg-row--live">
        <ThinkingWithPlan
          trace={trace}
          duration={elapsed > 0 ? formatDuration(elapsed) : undefined}
          steps={streamStepCount}
          planLook="card"
          style={{ flex: 1, minWidth: 0 }}
        />
      </div>
    );
  }

  function renderHitlStatus() {
    if (!hitlApprovalStatus) return null;
    const state =
      hitlApprovalStatus.approved === null
        ? 'Waiting for approval'
        : hitlApprovalStatus.approved
          ? 'Approved'
          : 'Denied';
    const detail =
      hitlApprovalStatus.approved === null
        ? 'Kim paused before a high-risk action.'
        : hitlApprovalStatus.approved
          ? 'Kim can continue with the approved action.'
          : 'Kim will choose another approach or ask for help.';

    return (
      <div className="kim-msg-row kim-msg-row--assistant">
        <div
          className={`kim-hitl-status${hitlApprovalStatus.approved === false ? ' kim-hitl-status--denied' : ''}`}
          role="status"
          aria-live="polite"
        >
          <span className="kim-hitl-status__label">{state}</span>
          <span className="kim-hitl-status__body">
            {detail} Tool: {hitlApprovalStatus.tool}. Risk: {hitlApprovalStatus.risk} ({hitlApprovalStatus.reason}).
          </span>
        </div>
      </div>
    );
  }

  function renderWorkedFor(_idx: number, run: { activity: ActivityItem[]; durationSec: number }) {
    const historyTrace = buildThinkingTrace(run.activity, parsePlanFromActivity(run.activity));
    const workedForTrace = traceToWorkedFor(historyTrace);
    const duration = run.durationSec > 0 ? formatDuration(run.durationSec) : '…';
    return (
      <div className="kim-msg-row kim-msg-row--assistant">
        <WorkedForPill trace={workedForTrace} duration={duration} />
      </div>
    );
  }

  function renderFilePills(files: TouchedFile[]) {
    if (files.length === 0) return null;
    return (
      <div className="kim-msg-row kim-msg-row--assistant">
        <div className="kim-file-pills">
          {files.map((f, i) => (
            <span key={i} className="kim-file-pill">
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
        ? `Ollama Cloud · ${settings.ollama.cloud_model || 'none'}`
        : providerLabel(resolveProvider());

    return (
      <div className={`kim-chat${empty ? ' kim-chat--empty-hero' : ''}`}>
        {renderConnectorsChrome()}

        <div className="kim-messages" ref={bottomRef as any}>
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

          {/* Live conversation history */}
          {(() => {
            const collapsed = collapseMessages(liveHistory);
            let liveUserIdx = -1;
            let liveAsstRunIdx = -1;
            return collapsed.map(({ msg, retries }, i) => {
              if (msg.role === 'user' && isRealUserMessage(msg)) liveUserIdx += 1;
              if (isIntermediateToolCall(msg)) return null;
              const showActivityAfter = msg.role === 'user' && isRealUserMessage(msg) &&
                !collapsed.slice(i + 1).some(({ msg: m }) => m.role === 'assistant' && !isIntermediateToolCall(m));
              let workedRun: { activity: ActivityItem[]; durationSec: number } | null = null;
              if (msg.role === 'assistant') {
                liveAsstRunIdx += 1;
                workedRun = runHistory[liveAsstRunIdx] ?? null;
              }
              return (
                <div key={`live-${i}`}>
                  {workedRun && renderWorkedFor(liveUserIdx, workedRun)}
                  <MessageBubble
                    message={msg}
                    animate={i === liveHistory.length - 1}
                    typingAnimation={settings.typing_animation ?? 'none'}
                    onRetry={handleRetryLast}
                    retries={retries}
                    onEdit={msg.role === 'user' ? (newText) => handleEditLiveMessage(i, newText) : undefined}
                  />
                  {showActivityAfter && renderActivityFeed()}
                </div>
              );
            });
          })()}

          {renderHitlStatus()}

          {/* Error / retry */}
          {taskError && taskError !== 'agent-error' && (
            <div className="kim-msg-row kim-msg-row--assistant">
              <div className="kim-task-error" role="alert">
                <span className="kim-task-error__icon">⚠</span>
                <span>{taskError}</span>
                {lastRunTask && (
                  <button type="button" className="kim-task-error__retry" onClick={() => void handleRetryLast()}>
                    Retry
                  </button>
                )}
              </div>
            </div>
          )}
          {taskError === 'agent-error' && (
            <div className="kim-msg-row kim-msg-row--assistant">
              <div className="kim-task-error" role="alert">
                <span className="kim-task-error__icon">⚠</span>
                <span>Kim ran into a problem and had to stop. Check the activity above for clues, or try rephrasing your task.</span>
                {lastRunTask && (
                  <button type="button" className="kim-task-error__retry" onClick={() => void handleRetryLast()}>
                    Retry
                  </button>
                )}
              </div>
            </div>
          )}

          {/* Structured run-failed card — rendered when Python emits kim:run_failed */}
          {runFailure && !isRunning && (
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
          )}

          {/* Rate-limited banner — shows while backing off, auto-clears after delay */}
          {rateLimitedState && isRunning && (
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
          )}

          {(queuedTasks.length > 0 || interruptTask) && (
            <div className="kim-queue-indicator" role="status" aria-live="polite">
              {interruptTask
                ? 'Interrupt pending. Current task will be replaced when cancellation completes.'
                : `${queuedTasks.length} queued message${queuedTasks.length === 1 ? '' : 's'} waiting.`}
            </div>
          )}

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

          <div ref={bottomRef as any} />
        </div>

        {renderComposer(false)}
      </div>
    );
  }

  // Existing session view
  return (
    <div className="kim-chat">
      {renderConnectorsChrome()}

      {/* Messages */}
      <div className="kim-messages">
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
            <div className="kim-messages__empty-text">No messages in this session</div>
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
              /* Normal message view */
              (() => {
                const collapsed = collapseMessages(messages);
                let userMsgIdx = -1;
                const liveAsstCount = collapseMessages(liveHistory)
                  .filter(({ msg }) => msg.role === 'assistant' && !isIntermediateToolCall(msg)).length;
                return collapsed.map(({ msg, retries }, i) => {
                  if (msg.role === 'user' && isRealUserMessage(msg)) userMsgIdx += 1;
                  if (isIntermediateToolCall(msg)) return null;

                  let workedRun: { activity: ActivityItem[]; durationSec: number } | null = null;
                  if (msg.role === 'assistant') {
                    const savedIdx = userMsgIdx - liveAsstCount;
                    workedRun = runHistory[savedIdx] ?? null;
                    if (!workedRun) {
                      const synth = synthesizeExchangeActivity(messages, userMsgIdx);
                      if (synth.length > 0) workedRun = { activity: synth, durationSec: 0 };
                    }
                  }

                  return (
                    <div key={i}>
                      {workedRun && renderWorkedFor(userMsgIdx, workedRun)}
                      <MessageBubble
                        message={msg}
                        animate={i === newestMsgIdx}
                        typingAnimation={settings.typing_animation ?? 'none'}
                        onRetry={handleRetryLast}
                        retries={retries}
                      />
                    </div>
                  );
                });
              })()
            )}

            {/* Newly added messages in this session */}
            {(() => {
              const collapsed = collapseMessages(liveHistory);
              const savedAsstCount = collapseMessages(messages)
                .filter(({ msg }) => msg.role === 'assistant' && !isIntermediateToolCall(msg)).length;
              const savedUserCount = messages.filter(isRealUserMessage).length;
              let liveUserMsgIdx = savedUserCount - 1;
              let liveAsstIdx = savedAsstCount;
              return collapsed.map(({ msg, retries }, i) => {
                if (msg.role === 'user' && isRealUserMessage(msg)) liveUserMsgIdx += 1;
                if (isIntermediateToolCall(msg)) return null;
                const showActivityAfter = msg.role === 'user' && isRealUserMessage(msg) &&
                  !collapsed.slice(i + 1).some(({ msg: m }) => m.role === 'assistant' && !isIntermediateToolCall(m));
                let workedRun: { activity: ActivityItem[]; durationSec: number } | null = null;
                if (msg.role === 'assistant') {
                  workedRun = runHistory[liveAsstIdx] ?? null;
                  liveAsstIdx += 1;
                }
                return (
                  <div key={`live-${i}`}>
                    {workedRun && renderWorkedFor(liveUserMsgIdx, workedRun)}
                    <MessageBubble
                      message={msg}
                      animate={i === liveHistory.length - 1}
                      typingAnimation={settings.typing_animation ?? 'none'}
                      onRetry={handleRetryLast}
                      retries={retries}
                      onEdit={msg.role === 'user' ? (newText) => handleEditLiveMessage(i, newText) : undefined}
                    />
                    {showActivityAfter && renderActivityFeed()}
                  </div>
                );
              });
            })()}

            {renderHitlStatus()}

            {taskError && (
              <div className="kim-msg-row kim-msg-row--assistant">
                <div style={{ maxWidth: '78%', minWidth: 0 }}>
                  <SignalCard kind="error" text={taskError} onAction={handleRetryLast} actionLabel="Resend Task" />
                </div>
              </div>
            )}
          </>
        )}
        <div ref={bottomRef as any} />
      </div>

      {renderComposer()}
    </div>
  );
}

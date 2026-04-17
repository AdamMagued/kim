import { useEffect, useRef, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import type { SessionInfo, KimMessage, Settings } from '../types';
import { MessageBubble } from './MessageBubble';

// Hard cap on live output lines to avoid unbounded memory growth on long
// agent runs. Older lines are dropped from the top and a placeholder row
// is inserted once so the user knows content was truncated.
const MAX_LIVE_OUTPUT_LINES = 500;

// Example prompts shown on the new-chat empty state.
const EXAMPLE_PROMPTS: { icon: string; title: string; hint: string }[] = [
  { icon: '📝', title: 'Summarize this PDF on my desktop', hint: 'Read and extract key insights' },
  { icon: '🎨', title: 'Make a gradient hero section in React', hint: 'Write, save, and open the file' },
  { icon: '🔍', title: 'Find all TODOs in my project', hint: 'Search across files and list them' },
  { icon: '🚀', title: 'Deploy this folder to GitHub', hint: 'Stage, commit, and push' },
];

interface Props {
  session: SessionInfo | null;
  newChatMode: boolean;
  settings: Settings;
  onTaskDone: () => void;
}

export function ChatView({ session, newChatMode, settings, onTaskDone }: Props) {
  const [messages, setMessages] = useState<KimMessage[]>([]);
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [taskInput, setTaskInput] = useState('');
  const [isRunning, setIsRunning] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [liveOutput, setLiveOutput] = useState<string[]>([]);
  const [taskError, setTaskError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Keep a stable ref to the onTaskDone callback so the event-listener
  // effect never re-subscribes. Previously the effect depended on
  // onTaskDone and duplicated listeners whenever the parent re-rendered.
  const onTaskDoneRef = useRef(onTaskDone);
  useEffect(() => {
    onTaskDoneRef.current = onTaskDone;
  }, [onTaskDone]);

  // Load messages when a session is selected
  useEffect(() => {
    if (!session) {
      setMessages([]);
      return;
    }
    setLoadingMessages(true);
    invoke<KimMessage[]>('load_session_messages', {
      sessionId: session.session_id,
      kimDir: settings.kim_sessions_dir || null,
      clawDir: settings.claw_sessions_dir || null,
    })
      .then(setMessages)
      .catch(err => console.error('Failed to load messages:', err))
      .finally(() => setLoadingMessages(false));
  }, [session, settings.kim_sessions_dir, settings.claw_sessions_dir]);

  // Scroll to bottom when messages change
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, liveOutput]);

  // Focus the textarea when we enter new-chat mode.
  useEffect(() => {
    if (newChatMode) {
      const t = setTimeout(() => textareaRef.current?.focus(), 50);
      return () => clearTimeout(t);
    }
  }, [newChatMode]);

  // Append a line to liveOutput, capping at MAX_LIVE_OUTPUT_LINES to avoid
  // unbounded memory growth on long-running tasks.
  function appendLive(line: string) {
    setLiveOutput(prev => {
      const next = [...prev, line];
      if (next.length > MAX_LIVE_OUTPUT_LINES) {
        const dropped = next.length - MAX_LIVE_OUTPUT_LINES;
        return [
          `[truncated ${dropped} earlier line${dropped === 1 ? '' : 's'}]`,
          ...next.slice(dropped + 1),
        ];
      }
      return next;
    });
  }

  // Listen for agent events — subscribe once.
  useEffect(() => {
    let unlistenOutput: (() => void) | undefined;
    let unlistenError: (() => void) | undefined;
    let unlistenDone: (() => void) | undefined;
    let unlistenCancelled: (() => void) | undefined;

    listen<string>('kim-agent-output', event => {
      appendLive(event.payload);
    }).then(fn => { unlistenOutput = fn; });

    listen<string>('kim-agent-error', event => {
      appendLive(`[err] ${event.payload}`);
    }).then(fn => { unlistenError = fn; });

    // Track cancel via ref so the done-listener can read it without
    // restarting the subscription.
    let cancelFlag = false;

    listen<boolean>('kim-agent-done', event => {
      setIsRunning(false);
      setCancelling(false);
      if (event.payload) {
        onTaskDoneRef.current();
      } else if (!cancelFlag) {
        setTaskError('Agent exited with an error. Check logs.');
      }
      cancelFlag = false;
    }).then(fn => { unlistenDone = fn; });

    listen<boolean>('kim-agent-cancelled', () => {
      cancelFlag = true;
      appendLive('⏹ Task cancelled');
      setIsRunning(false);
      setCancelling(false);
    }).then(fn => { unlistenCancelled = fn; });

    return () => {
      unlistenOutput?.();
      unlistenError?.();
      unlistenDone?.();
      unlistenCancelled?.();
    };
    // Subscribe-once semantics: intentionally no deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleCancel() {
    if (!isRunning || cancelling) return;
    setCancelling(true);
    try {
      await invoke('cancel_task');
    } catch (err) {
      setCancelling(false);
      setTaskError(`Cancel failed: ${String(err)}`);
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const task = taskInput.trim();
    if (!task || isRunning) return;

    setIsRunning(true);
    setLiveOutput([]);
    setTaskError(null);
    setTaskInput('');
    // Reset textarea height
    if (textareaRef.current) textareaRef.current.style.height = 'auto';

    try {
      await invoke('send_task', {
        task,
        provider: settings.provider || null,
        projectRoot: settings.project_root || null,
      });
    } catch (err) {
      setIsRunning(false);
      setTaskError(String(err));
    }
  }

  // Auto-resize textarea
  function handleTextareaInput(e: React.ChangeEvent<HTMLTextAreaElement>) {
    setTaskInput(e.target.value);
    const el = e.target;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 200) + 'px';
  }

  function pickExample(p: string) {
    setTaskInput(p);
    // Auto-resize after state update
    setTimeout(() => {
      if (textareaRef.current) {
        textareaRef.current.style.height = 'auto';
        textareaRef.current.style.height =
          Math.min(textareaRef.current.scrollHeight, 200) + 'px';
        textareaRef.current.focus();
      }
    }, 0);
  }

  // ── Empty welcome state (no session, no newChat) ───────────────────────────
  if (!newChatMode && !session) {
    return (
      <div className="kim-chat">
        <div className="kim-empty-welcome">
          <div className="kim-empty-welcome__icon">
            <div className="kim-empty-welcome__glow" />
            <svg viewBox="0 0 24 24" width="44" height="44" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 2l2.5 6.5L21 11l-6.5 2.5L12 20l-2.5-6.5L3 11l6.5-2.5L12 2z" />
            </svg>
          </div>
          <div className="kim-empty-welcome__title">Welcome to Kim</div>
          <div className="kim-empty-welcome__subtitle">
            Pick a session from the sidebar or start a new chat
          </div>
          <div className="kim-empty-welcome__kbd-hint">
            Press <kbd>⌘</kbd> <kbd>N</kbd> for a new chat
          </div>
        </div>
      </div>
    );
  }

  // ── New chat mode ──────────────────────────────────────────────────────────
  if (newChatMode) {
    const hasStarted = isRunning || liveOutput.length > 0 || taskError;

    return (
      <div className="kim-chat">
        <div className="kim-chat__output">
          {!hasStarted && (
            <div className="kim-new-chat-empty">
              <div className="kim-new-chat-empty__badge">
                <span className="kim-pulse-dot kim-pulse-dot--accent" />
                Ready
              </div>
              <div className="kim-new-chat-empty__title">What should Kim do?</div>
              <div className="kim-new-chat-empty__subtitle">
                Describe a task in plain English. Kim can see your screen, control
                your mouse, run commands, browse the web, and write code.
              </div>

              <div className="kim-examples">
                {EXAMPLE_PROMPTS.map((ex, i) => (
                  <button
                    key={i}
                    className="kim-example-card"
                    onClick={() => pickExample(ex.title)}
                  >
                    <div className="kim-example-card__icon">{ex.icon}</div>
                    <div className="kim-example-card__body">
                      <div className="kim-example-card__title">{ex.title}</div>
                      <div className="kim-example-card__hint">{ex.hint}</div>
                    </div>
                    <div className="kim-example-card__arrow">↗</div>
                  </button>
                ))}
              </div>
            </div>
          )}

          {taskError && (
            <div className="kim-task-error" role="alert">
              <span className="kim-task-error__icon">⚠</span>
              <span>{taskError}</span>
            </div>
          )}

          {liveOutput.length > 0 && (
            <div className="kim-live-output">
              {liveOutput.map((line, i) => {
                const isErr = line.startsWith('[err]');
                const isCancelled = line.startsWith('⏹');
                const isTruncated = line.startsWith('[truncated');
                return (
                  <div
                    key={i}
                    className={
                      'kim-live-output__line' +
                      (isErr ? ' kim-live-output__line--err' : '') +
                      (isCancelled ? ' kim-live-output__line--cancelled' : '') +
                      (isTruncated ? ' kim-live-output__line--truncated' : '')
                    }
                  >
                    {line}
                  </div>
                );
              })}
            </div>
          )}

          {isRunning && (
            <div className="kim-working-indicator">
              <div className="kim-working-indicator__dots">
                <span /><span /><span />
              </div>
              <span className="kim-working-indicator__text">
                {cancelling ? 'Stopping Kim…' : 'Kim is thinking…'}
              </span>
            </div>
          )}

          <div ref={bottomRef} />
        </div>

        {/* Composer */}
        <form className="kim-composer" onSubmit={handleSubmit}>
          <div className={'kim-composer__box' + (isRunning ? ' kim-composer__box--running' : '')}>
            <textarea
              ref={textareaRef}
              value={taskInput}
              onChange={handleTextareaInput}
              placeholder={isRunning ? 'Kim is working — press Stop to cancel' : 'Message Kim…'}
              rows={1}
              disabled={isRunning}
              onKeyDown={e => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  void handleSubmit(e as unknown as React.FormEvent);
                }
              }}
              className="kim-composer__textarea"
            />

            <div className="kim-composer__actions">
              {isRunning ? (
                <button
                  type="button"
                  onClick={handleCancel}
                  disabled={cancelling}
                  title={cancelling ? 'Stopping…' : 'Stop task'}
                  className={'kim-btn kim-btn--stop' + (cancelling ? ' kim-btn--stop-pending' : '')}
                  aria-label="Stop task"
                >
                  <svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor">
                    <rect x="6" y="6" width="12" height="12" rx="2" />
                  </svg>
                </button>
              ) : (
                <button
                  type="submit"
                  disabled={!taskInput.trim()}
                  className="kim-btn kim-btn--send"
                  aria-label="Send"
                >
                  <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M5 12h14M13 5l7 7-7 7" />
                  </svg>
                </button>
              )}
            </div>
          </div>
          <div className="kim-composer__hint">
            <span><kbd>↵</kbd> to send</span>
            <span className="kim-composer__hint-sep">·</span>
            <span><kbd>⇧</kbd>+<kbd>↵</kbd> for new line</span>
            <span className="kim-composer__hint-sep">·</span>
            <span>provider: <strong>{settings.provider}</strong></span>
          </div>
        </form>
      </div>
    );
  }

  // ── Existing session view ──────────────────────────────────────────────────
  return (
    <div className="kim-chat">
      {/* Session header */}
      <div className="kim-session-header">
        <div className="kim-session-header__main">
          <div className="kim-session-header__row">
            <span
              className={`kim-session-badge kim-session-badge--${session!.session_type}`}
            >
              {session!.session_type === 'kim' ? 'Kim' : 'Claw Code'}
            </span>
            <span className="kim-session-header__id">{session!.session_id}</span>
          </div>
          <div className="kim-session-header__meta">
            <span>{session!.date}</span>
            <span className="kim-session-header__dot">·</span>
            <span>{session!.message_count} message{session!.message_count !== 1 ? 's' : ''}</span>
            {session!.has_summary && (
              <>
                <span className="kim-session-header__dot">·</span>
                <span className="kim-session-header__summary-tag">summarized</span>
              </>
            )}
          </div>
          {session!.summary && (
            <div className="kim-session-header__summary">
              {session!.summary}
            </div>
          )}
        </div>
      </div>

      {/* Messages */}
      <div className="kim-messages">
        {loadingMessages ? (
          <div className="kim-messages__loading">
            <div className="kim-spinner" />
            <span>Loading conversation…</span>
          </div>
        ) : messages.length === 0 ? (
          <div className="kim-messages__empty">
            <div className="kim-messages__empty-icon">💬</div>
            <div className="kim-messages__empty-text">No messages in this session</div>
          </div>
        ) : (
          <>
            {messages.map((msg, i) => (
              <MessageBubble key={i} message={msg} />
            ))}
          </>
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}

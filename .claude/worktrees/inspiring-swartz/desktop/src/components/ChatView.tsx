import { useEffect, useRef, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import type { SessionInfo, KimMessage, Settings } from '../types';
import { MessageBubble } from './MessageBubble';

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

  // Listen for agent events
  useEffect(() => {
    let unlistenOutput: (() => void) | undefined;
    let unlistenError: (() => void) | undefined;
    let unlistenDone: (() => void) | undefined;
    let unlistenCancelled: (() => void) | undefined;

    listen<string>('kim-agent-output', event => {
      setLiveOutput(prev => [...prev, event.payload]);
    }).then(fn => { unlistenOutput = fn; });

    listen<string>('kim-agent-error', event => {
      setLiveOutput(prev => [...prev, `[err] ${event.payload}`]);
    }).then(fn => { unlistenError = fn; });

    // Track whether the user initiated a cancel so the "agent-done" event
    // does not overwrite the friendly "Task cancelled" message with a generic
    // "exited with an error" banner.
    let cancelFlag = false;

    listen<boolean>('kim-agent-done', event => {
      setIsRunning(false);
      setCancelling(false);
      if (event.payload) {
        onTaskDone();
      } else if (!cancelFlag) {
        setTaskError('Agent exited with an error. Check logs.');
      }
      cancelFlag = false;
    }).then(fn => { unlistenDone = fn; });

    listen<boolean>('kim-agent-cancelled', () => {
      cancelFlag = true;
      setLiveOutput(prev => [...prev, '⏹ Task cancelled']);
      setIsRunning(false);
      setCancelling(false);
    }).then(fn => { unlistenCancelled = fn; });

    return () => {
      unlistenOutput?.();
      unlistenError?.();
      unlistenDone?.();
      unlistenCancelled?.();
    };
  }, [onTaskDone]);

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

  // ── Empty / new chat state ─────────────────────────────────────────────────
  if (newChatMode || (!session && !newChatMode)) {
    return (
      <div
        style={{
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
          background: 'var(--bg)',
          overflow: 'hidden',
        }}
      >
        {/* Welcome / empty */}
        {!newChatMode && (
          <div
            style={{
              flex: 1,
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              color: 'var(--text-muted)',
              gap: '12px',
            }}
          >
            <div style={{ fontSize: '48px' }}>🤖</div>
            <div style={{ fontSize: '20px', fontWeight: 600, color: 'var(--text)' }}>
              Kim
            </div>
            <div style={{ fontSize: '14px' }}>
              Select a session or start a new chat
            </div>
          </div>
        )}

        {/* New chat mode */}
        {newChatMode && (
          <>
            {/* Live output area */}
            <div style={{ flex: 1, overflowY: 'auto', padding: '16px' }}>
              {liveOutput.length === 0 && !isRunning && !taskError && (
                <div
                  style={{
                    display: 'flex',
                    flexDirection: 'column',
                    alignItems: 'center',
                    justifyContent: 'center',
                    height: '100%',
                    color: 'var(--text-muted)',
                    gap: '8px',
                  }}
                >
                  <div style={{ fontSize: '32px' }}>✨</div>
                  <div style={{ fontSize: '16px', fontWeight: 600, color: 'var(--text)' }}>
                    New chat
                  </div>
                  <div style={{ fontSize: '13px' }}>Type a task below to get started</div>
                </div>
              )}

              {taskError && (
                <div
                  style={{
                    background: '#fee2e2',
                    border: '1px solid #fca5a5',
                    borderRadius: '8px',
                    padding: '12px 16px',
                    color: '#991b1b',
                    fontSize: '13px',
                    marginBottom: '12px',
                  }}
                >
                  {taskError}
                </div>
              )}

              {liveOutput.map((line, i) => (
                <div
                  key={i}
                  style={{
                    fontFamily: 'monospace',
                    fontSize: '12px',
                    color: line.startsWith('[err]') ? '#ef4444' : 'var(--text)',
                    padding: '2px 0',
                    wordBreak: 'break-all',
                  }}
                >
                  {line}
                </div>
              ))}

              {isRunning && (
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '8px',
                    padding: '8px 0',
                    color: 'var(--accent)',
                    fontSize: '13px',
                  }}
                >
                  <span className="animate-pulse">●</span>
                  <span>Kim is working…</span>
                </div>
              )}

              <div ref={bottomRef} />
            </div>

            {/* Input */}
            <div
              style={{
                borderTop: '1px solid var(--border)',
                padding: '16px',
                background: 'var(--bg)',
              }}
            >
              <form onSubmit={handleSubmit} style={{ display: 'flex', gap: '10px', alignItems: 'flex-end' }}>
                <textarea
                  ref={textareaRef}
                  value={taskInput}
                  onChange={handleTextareaInput}
                  placeholder="Describe a task for Kim…"
                  rows={1}
                  disabled={isRunning}
                  onKeyDown={e => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault();
                      void handleSubmit(e as unknown as React.FormEvent);
                    }
                  }}
                  style={{
                    flex: 1,
                    resize: 'none',
                    padding: '10px 14px',
                    borderRadius: '12px',
                    border: '1px solid var(--border)',
                    background: 'var(--bg-input)',
                    color: 'var(--text)',
                    fontSize: '14px',
                    outline: 'none',
                    fontFamily: 'inherit',
                    lineHeight: 1.5,
                    overflowY: 'hidden',
                    minHeight: '42px',
                    maxHeight: '200px',
                    opacity: isRunning ? 0.6 : 1,
                  }}
                />
                {isRunning ? (
                  <button
                    type="button"
                    onClick={handleCancel}
                    disabled={cancelling}
                    title={cancelling ? 'Stopping…' : 'Stop task'}
                    style={{
                      width: '42px',
                      height: '42px',
                      borderRadius: '12px',
                      border: 'none',
                      background: cancelling ? '#b91c1c' : '#dc2626',
                      color: '#fff',
                      cursor: cancelling ? 'wait' : 'pointer',
                      fontSize: '18px',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      flexShrink: 0,
                      boxShadow: '0 0 0 3px rgba(220, 38, 38, 0.25)',
                      transition: 'all 0.15s ease',
                    }}
                  >
                    ⏹
                  </button>
                ) : (
                  <button
                    type="submit"
                    disabled={!taskInput.trim()}
                    style={{
                      width: '42px',
                      height: '42px',
                      borderRadius: '12px',
                      border: 'none',
                      background: taskInput.trim() ? 'var(--accent)' : 'var(--bg-card)',
                      color: taskInput.trim() ? '#fff' : 'var(--text-muted)',
                      cursor: taskInput.trim() ? 'pointer' : 'not-allowed',
                      fontSize: '18px',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      flexShrink: 0,
                      transition: 'all 0.15s ease',
                    }}
                  >
                    ↑
                  </button>
                )}
              </form>
              <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '6px', paddingLeft: '2px' }}>
                Enter to send · Shift+Enter for new line
              </div>
            </div>
          </>
        )}
      </div>
    );
  }

  // ── Session view ───────────────────────────────────────────────────────────
  return (
    <div
      style={{
        flex: 1,
        display: 'flex',
        flexDirection: 'column',
        background: 'var(--bg)',
        overflow: 'hidden',
      }}
    >
      {/* Session header */}
      <div
        style={{
          padding: '12px 20px',
          borderBottom: '1px solid var(--border)',
          display: 'flex',
          alignItems: 'center',
          gap: '12px',
          flexShrink: 0,
        }}
      >
        <div>
          <div style={{ fontWeight: 600, fontSize: '14px', color: 'var(--text)' }}>
            {session!.session_id}
          </div>
          <div style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
            {session!.date} · {session!.message_count} messages ·{' '}
            <span
              style={{
                background: session!.session_type === 'kim' ? 'var(--accent-muted)' : '#f3e8ff',
                color: session!.session_type === 'kim' ? 'var(--accent)' : '#7c3aed',
                padding: '1px 7px',
                borderRadius: '8px',
                fontSize: '11px',
                fontWeight: 500,
              }}
            >
              {session!.session_type === 'kim' ? 'Kim' : 'Claw Code'}
            </span>
          </div>
          {session!.summary && (
            <div
              style={{
                fontSize: '12px',
                color: 'var(--text-muted)',
                marginTop: '4px',
                maxWidth: '600px',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {session!.summary}
            </div>
          )}
        </div>
      </div>

      {/* Messages */}
      <div style={{ flex: 1, overflowY: 'auto', paddingTop: '12px', paddingBottom: '16px' }}>
        {loadingMessages ? (
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              height: '100%',
              color: 'var(--text-muted)',
              fontSize: '13px',
            }}
          >
            Loading…
          </div>
        ) : messages.length === 0 ? (
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              height: '100%',
              color: 'var(--text-muted)',
              fontSize: '13px',
            }}
          >
            No messages in this session
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

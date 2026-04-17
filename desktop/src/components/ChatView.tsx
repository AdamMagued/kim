import { useEffect, useRef, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import type { SessionInfo, KimMessage, Settings, KimAccount } from '../types';
import { MessageBubble } from './MessageBubble';
import { BrowserProviderPicker } from './BrowserProviderPicker';

const MAX_ACTIVITY_ITEMS = 300;

// ── Activity feed ─────────────────────────────────────────────────────────────

interface ActivityItem {
  id: number;
  kind: 'tool' | 'info' | 'error' | 'success' | 'cancelled' | 'status';
  icon: string;
  text: string;
}

let _activityCounter = 0;

/** Lines that contain these strings are silently dropped */
const HIDDEN_PATTERNS = [
  'take_screenshot',
  'screenshot',
  'capture_screen',
  'TASK_COMPLETE',
  'NEED_HELP',
  // noisy internal logs
  'INFO] kimdir',
  'DEBUG] kimdir',
];

/** Friendly names + icons for known tool calls */
const TOOL_MAP: Record<string, { icon: string; label: (args: Record<string, unknown>) => string }> = {
  read_file:          { icon: '📄', label: a => `Reading \`${basename(String(a.path ?? a.file_path ?? ''))}\`` },
  write_file:         { icon: '✏️', label: a => `Writing \`${basename(String(a.path ?? a.file_path ?? ''))}\`` },
  create_file:        { icon: '📝', label: a => `Creating \`${basename(String(a.path ?? ''))}\`` },
  edit_file:          { icon: '✏️', label: a => `Editing \`${basename(String(a.path ?? a.file_path ?? ''))}\`` },
  delete_file:        { icon: '🗑', label: a => `Deleting \`${basename(String(a.path ?? ''))}\`` },
  list_directory:     { icon: '📁', label: a => `Listing \`${basename(String(a.path ?? a.directory ?? ''))}\`` },
  search_files:       { icon: '🔍', label: a => `Searching for \`${String(a.pattern ?? a.query ?? '')}\`` },
  grep:               { icon: '🔍', label: a => `Searching code for \`${String(a.pattern ?? '')}\`` },
  run_command:        { icon: '⚡', label: a => `Running \`${shorten(String(a.command ?? a.cmd ?? ''), 60)}\`` },
  execute_command:    { icon: '⚡', label: a => `Running \`${shorten(String(a.command ?? ''), 60)}\`` },
  bash:               { icon: '⚡', label: a => `Running \`${shorten(String(a.command ?? ''), 60)}\`` },
  browser_navigate:   { icon: '🌐', label: a => `Opening ${String(a.url ?? 'a web page')}` },
  web_search:         { icon: '🔍', label: a => `Searching the web for "${String(a.query ?? '')}"\`` },
  type_text:          { icon: '⌨️',  label: _a => 'Typing text' },
  click:              { icon: '🖱', label: _a => 'Clicking' },
  scroll:             { icon: '↕',  label: _a => 'Scrolling' },
  move_mouse:         { icon: '🖱', label: _a => 'Moving mouse' },
  press_key:          { icon: '⌨️',  label: a => `Pressing key \`${String(a.key ?? '')}\`` },
  open_application:   { icon: '🚀', label: a => `Opening ${String(a.app_name ?? a.application ?? '')}` },
  close_application:  { icon: '✕',  label: a => `Closing ${String(a.app_name ?? '')}` },
  read_clipboard:     { icon: '📋', label: _a => 'Reading clipboard' },
  write_clipboard:    { icon: '📋', label: _a => 'Writing to clipboard' },
  get_screen_text:    { icon: '👁', label: _a => 'Reading screen text' },
  ask_user:           { icon: '💬', label: a => `Asking: "${String(a.question ?? '')}"` },
};

function basename(p: string): string {
  if (!p) return '';
  return p.split(/[/\\]/).pop() ?? p;
}

function shorten(s: string, max: number): string {
  return s.length > max ? s.slice(0, max) + '…' : s;
}

/** Map technical error text to something a non-technical user can understand */
function friendlyError(raw: string): string {
  const r = raw.toLowerCase();
  if (r.includes('api key') || r.includes('unauthorized') || r.includes('401'))
    return 'Your API key isn\'t working. Open Settings → AI to check your credentials.';
  if (r.includes('rate limit') || r.includes('429') || r.includes('too many requests'))
    return 'Kim is being rate-limited by the AI provider. Wait a moment and try again.';
  if (r.includes('quota') || r.includes('billing') || r.includes('insufficient_quota'))
    return 'You\'ve hit your API usage limit. Check your billing on the provider\'s website.';
  if (r.includes('network') || r.includes('connection refused') || r.includes('econnrefused') || r.includes('fetch'))
    return 'Can\'t reach the AI provider. Check your internet connection and try again.';
  if (r.includes('timeout') || r.includes('timed out'))
    return 'The request took too long and timed out. Try a simpler task or check your connection.';
  if (r.includes('model') && (r.includes('not found') || r.includes('invalid')))
    return 'The selected AI model isn\'t available. Open Settings → AI to pick a different one.';
  if (r.includes('context') && r.includes('length'))
    return 'The conversation is too long for the AI to handle. Try starting a new chat.';
  if (r.includes('permission') || r.includes('access denied'))
    return 'Kim doesn\'t have permission to access that file or folder.';
  // Strip noise from log lines and return a condensed version
  const cleaned = raw
    .replace(/\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2},?\d*\s*/g, '')
    .replace(/\[(ERROR|WARN|INFO|DEBUG|TOOL|CRITICAL)\]\s*/g, '')
    .replace(/orchestrator\.\w+:\s*/g, '')
    .trim();
  return cleaned.length > 0 && cleaned.length < 200 ? cleaned : 'Something went wrong. Check your settings and try again.';
}

function parseLogLine(raw: string, id: number): ActivityItem | null {
  // Hide screenshot and other noisy lines
  for (const pat of HIDDEN_PATTERNS) {
    if (raw.toLowerCase().includes(pat.toLowerCase())) return null;
  }

  // Hide truncated meta-lines
  if (raw.startsWith('[truncated')) return null;

  // ⏹ Cancelled
  if (raw.startsWith('⏹')) {
    return { id, kind: 'cancelled', icon: '⏹', text: 'Task stopped' };
  }

  // SUCCESS from stdout
  if (raw.includes('[SUCCESS]')) {
    const text = raw.replace(/.*\[SUCCESS\]\s*/, '').trim();
    return { id, kind: 'success', icon: '✓', text: text || 'Task completed successfully' };
  }

  // [err] prefix = came from stderr
  const isErr = raw.startsWith('[err]');
  const line = isErr ? raw.slice(5).trim() : raw;

  // Strip timestamp prefix: "2024-01-01 12:00:00,123 "
  const stripped = line.replace(/^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}[.,]?\d*\s+/, '');

  // [TOOL] lines
  const toolMatch = stripped.match(/\[TOOL\]\s+[\w.]+:\s+(\w+)\((.{0,200})\)/);
  if (toolMatch) {
    const toolName = toolMatch[1];
    const argsRaw = toolMatch[2] ?? '{}';
    // Try to parse JSON args (may be truncated)
    let args: Record<string, unknown> = {};
    try { args = JSON.parse(argsRaw); } catch {
      // Try to extract first key-value pair at minimum
      const m = argsRaw.match(/"(\w+)":\s*"([^"]+)"/);
      if (m) args = { [m[1]]: m[2] };
    }

    const def = TOOL_MAP[toolName];
    if (def) {
      return { id, kind: 'tool', icon: def.icon, text: def.label(args) };
    }
    // Unknown tool — show generic
    return { id, kind: 'tool', icon: '🔧', text: `Using tool: \`${toolName}\`` };
  }

  // [ERROR] / [CRITICAL] lines
  if (stripped.match(/\[(ERROR|CRITICAL)\]/)) {
    const msg = stripped.replace(/\[(ERROR|CRITICAL)\]\s+[\w.]*:\s*/, '').trim();
    return { id, kind: 'error', icon: '⚠', text: friendlyError(msg) };
  }

  // [FAILED] from stdout
  if (raw.includes('[FAILED]') || raw.includes('[ERROR]')) {
    const msg = raw.replace(/.*\[(FAILED|ERROR)\]\s*/, '').trim();
    return { id, kind: 'error', icon: '⚠', text: friendlyError(msg) };
  }

  // Generic [err] stderr lines that aren't tool calls
  if (isErr) {
    // Filter out pure INFO/DEBUG log noise that isn't user-facing
    if (stripped.match(/\[(INFO|DEBUG)\]/)) {
      const msg = stripped.replace(/\[(INFO|DEBUG)\]\s+[\w.]*:\s*/, '').trim();
      // Only show if it looks like a meaningful status update
      if (msg.length < 5 || msg.match(/^(Starting|Listening|Initialized|Loaded|Connected)/i)) return null;
      return { id, kind: 'status', icon: '·', text: msg.length > 120 ? msg.slice(0, 120) + '…' : msg };
    }
    // Non-classified stderr
    const msg = stripped.replace(/\[[\w]+\]\s+[\w.]*:\s*/, '').trim();
    if (!msg) return null;
    return { id, kind: 'status', icon: '·', text: msg.length > 120 ? msg.slice(0, 120) + '…' : msg };
  }

  return null;
}

// ── Greeting ──────────────────────────────────────────────────────────────────

const EXAMPLE_PROMPTS: { title: string; hint: string; icon: string }[] = [
  { title: 'Summarize the PDF on my desktop', hint: 'Read and extract key insights', icon: '📄' },
  { title: 'Find all TODOs in my project', hint: 'Search files and list them', icon: '🔍' },
  { title: 'Stage, commit, and push my changes', hint: 'Full git workflow', icon: '⚡' },
  { title: 'Search the web and write a report', hint: 'Browse + summarize', icon: '🌐' },
];

const KIM_CAPABILITIES = [
  { icon: '🖥', label: 'See your screen', desc: 'Kim takes screenshots to understand what\'s happening' },
  { icon: '🖱', label: 'Control your mouse', desc: 'Click buttons, drag files, navigate any app' },
  { icon: '⌨️', label: 'Type & edit', desc: 'Write code, fill forms, compose emails' },
  { icon: '📁', label: 'Manage files', desc: 'Read, write, move, search files on your computer' },
  { icon: '🌐', label: 'Browse the web', desc: 'Search, visit websites, extract information' },
  { icon: '⚡', label: 'Run commands', desc: 'Terminal commands, scripts, git operations' },
];

const KIM_SHORTCUTS = [
  { keys: ['⌘', 'N'], label: 'New chat' },
  { keys: ['⌘', 'B'], label: 'Toggle sidebar' },
  { keys: ['⌘', ','], label: 'Settings' },
  { keys: ['⇧', '↵'], label: 'New line in message' },
];

function getGreeting(name: string): string {
  const hour = new Date().getHours();
  if (hour < 5) return `Late night, ${name}`;
  if (hour < 12) return `Good morning, ${name}`;
  if (hour < 17) return `Good afternoon, ${name}`;
  if (hour < 21) return `Good evening, ${name}`;
  return `Evening, ${name}`;
}

function formatElapsed(s: number): string {
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return `${m}m ${sec}s`;
}

// ── Props ─────────────────────────────────────────────────────────────────────

interface Props {
  session: SessionInfo | null;
  newChatMode: boolean;
  settings: Settings;
  onTaskDone: () => void;
  account: KimAccount;
}

// ── Component ─────────────────────────────────────────────────────────────────

export function ChatView({ session, newChatMode, settings, onTaskDone, account }: Props) {
  const [messages, setMessages] = useState<KimMessage[]>([]);
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [taskInput, setTaskInput] = useState('');
  const [isRunning, setIsRunning] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [activity, setActivity] = useState<ActivityItem[]>([]);
  const [taskError, setTaskError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [tokenStats, setTokenStats] = useState<{ input: number; output: number; total: number } | null>(null);
  // Which browser AI provider is selected (only relevant when settings.provider === 'browser')
  const [browserProvider, setBrowserProvider] = useState('claude');

  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const startTimeRef = useRef<number | null>(null);

  // Keep a stable ref to the onTaskDone callback
  const onTaskDoneRef = useRef(onTaskDone);
  useEffect(() => { onTaskDoneRef.current = onTaskDone; }, [onTaskDone]);

  // ── Timer ───────────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!isRunning) return;
    startTimeRef.current = Date.now();
    setElapsed(0);
    const id = setInterval(() => {
      setElapsed(Math.floor((Date.now() - (startTimeRef.current ?? Date.now())) / 1000));
    }, 1000);
    return () => clearInterval(id);
  }, [isRunning]);

  // ── Load messages ───────────────────────────────────────────────────────────
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

  // ── Scroll to bottom ────────────────────────────────────────────────────────
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, activity]);

  // ── Focus on new chat ───────────────────────────────────────────────────────
  useEffect(() => {
    if (newChatMode) {
      const t = setTimeout(() => textareaRef.current?.focus(), 50);
      return () => clearTimeout(t);
    }
  }, [newChatMode]);

  // ── Append to activity feed ─────────────────────────────────────────────────
  function appendRaw(line: string) {
    const id = ++_activityCounter;

    // Handle [STATS] token lines — update token counter, don't add to feed
    const statsMatch = line.match(/\[STATS\]\s+input_tokens=(\d+)\s+output_tokens=(\d+)\s+total_tokens=(\d+)/);
    if (statsMatch) {
      setTokenStats({ input: parseInt(statsMatch[1]), output: parseInt(statsMatch[2]), total: parseInt(statsMatch[3]) });
      return;
    }

    // Handle [DIFF] lines — annotate the previous file-write activity item
    const diffMatch = line.match(/\[DIFF\]\s+path=(\S+)\s+\+(\d+)\s+-(\d+)/);
    if (diffMatch) {
      const [, _path, added, removed] = diffMatch;
      setActivity(prev => {
        if (prev.length === 0) return prev;
        const last = prev[prev.length - 1];
        if (last.kind === 'tool' && (last.text.includes('Editing') || last.text.includes('Writing') || last.text.includes('Creating'))) {
          const annotated = { ...last, text: last.text + ` +${added} -${removed}` };
          return [...prev.slice(0, -1), annotated];
        }
        return prev;
      });
      return;
    }

    const item = parseLogLine(line, id);
    if (!item) return;
    setActivity(prev => {
      const next = [...prev, item];
      if (next.length > MAX_ACTIVITY_ITEMS) return next.slice(next.length - MAX_ACTIVITY_ITEMS);
      return next;
    });
  }

  // ── Agent event listeners ───────────────────────────────────────────────────
  useEffect(() => {
    let unlistenOutput: (() => void) | undefined;
    let unlistenError: (() => void) | undefined;
    let unlistenDone: (() => void) | undefined;
    let unlistenCancelled: (() => void) | undefined;

    listen<string>('kim-agent-output', event => {
      appendRaw(event.payload);
    }).then(fn => { unlistenOutput = fn; });

    listen<string>('kim-agent-error', event => {
      appendRaw(`[err] ${event.payload}`);
    }).then(fn => { unlistenError = fn; });

    let cancelFlag = false;

    listen<boolean>('kim-agent-done', event => {
      setIsRunning(false);
      setCancelling(false);
      if (event.payload) {
        onTaskDoneRef.current();
      } else if (!cancelFlag) {
        setTaskError('agent-error');
      }
      cancelFlag = false;
    }).then(fn => { unlistenDone = fn; });

    listen<boolean>('kim-agent-cancelled', () => {
      cancelFlag = true;
      appendRaw('⏹ Task cancelled');
      setIsRunning(false);
      setCancelling(false);
    }).then(fn => { unlistenCancelled = fn; });

    return () => {
      unlistenOutput?.();
      unlistenError?.();
      unlistenDone?.();
      unlistenCancelled?.();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Actions ─────────────────────────────────────────────────────────────────

  async function handleCancel() {
    if (!isRunning || cancelling) return;
    setCancelling(true);
    try {
      await invoke('cancel_task');
    } catch (err) {
      setCancelling(false);
      setTaskError(friendlyError(String(err)));
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const task = taskInput.trim();
    if (!task || isRunning) return;

    setIsRunning(true);
    setActivity([]);
    setTaskError(null);
    setTokenStats(null);
    setTaskInput('');
    if (textareaRef.current) textareaRef.current.style.height = 'auto';

    try {
      const resolvedProvider = settings.provider === 'browser'
        ? `browser:${browserProvider}`
        : (settings.provider || null);
      await invoke('send_task', {
        task,
        provider: resolvedProvider,
        projectRoot: settings.project_root || null,
      });
    } catch (err) {
      setIsRunning(false);
      setTaskError(friendlyError(String(err)));
    }
  }

  function handleTextareaInput(e: React.ChangeEvent<HTMLTextAreaElement>) {
    setTaskInput(e.target.value);
    const el = e.target;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 200) + 'px';
  }

  function pickExample(p: string) {
    setTaskInput(p);
    setTimeout(() => {
      if (textareaRef.current) {
        textareaRef.current.style.height = 'auto';
        textareaRef.current.style.height =
          Math.min(textareaRef.current.scrollHeight, 200) + 'px';
        textareaRef.current.focus();
      }
    }, 0);
  }

  // ── Activity feed render ─────────────────────────────────────────────────────

  /** Renders activity text: backtick → <code>, +N → green, -N → red */
  function renderActivityText(text: string): React.ReactNode {
    // Split on backtick segments AND +N/-N diff markers
    const parts = text.split(/(`[^`]+`|\+\d+|-\d+)/g);
    if (parts.length === 1) return text;
    return parts.map((p, i) => {
      if (p.startsWith('`') && p.endsWith('`'))
        return <code key={i}>{p.slice(1, -1)}</code>;
      if (/^\+\d+$/.test(p))
        return <span key={i} className="kim-diff-added">{p}</span>;
      if (/^-\d+$/.test(p))
        return <span key={i} className="kim-diff-removed">{p}</span>;
      return p;
    });
  }

  function renderActivityFeed() {
    if (activity.length === 0) return null;
    return (
      <div className="kim-activity-feed">
        {activity.map(item => (
          <div key={item.id} className={`kim-activity-item kim-activity-item--${item.kind}`}>
            <span className="kim-activity-item__icon" aria-hidden="true">{item.icon}</span>
            <span className="kim-activity-item__text">{renderActivityText(item.text)}</span>
          </div>
        ))}
      </div>
    );
  }

  // ── Composer ─────────────────────────────────────────────────────────────────

  function renderComposer() {
    return (
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
          <span>via <strong>{settings.provider}</strong></span>
        </div>
      </form>
    );
  }

  // ── Empty welcome state ──────────────────────────────────────────────────────
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
          <div className="kim-empty-welcome__title">{getGreeting(account.display_name)}</div>
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

  // ── New chat mode ─────────────────────────────────────────────────────────────
  if (newChatMode) {
    const hasStarted = isRunning || activity.length > 0 || taskError;

    return (
      <div className="kim-chat">
        <div className="kim-chat__output">
          {!hasStarted && (
            <div className="kim-new-chat-empty">
              <div className="kim-new-chat-empty__badge">
                <span className="kim-pulse-dot kim-pulse-dot--accent" />
                Ready
              </div>
              <div className="kim-new-chat-empty__title">{getGreeting(account.display_name)}</div>
              <div className="kim-new-chat-empty__subtitle">
                Describe any task in plain English below — Kim will figure out how to do it.
              </div>

              {settings.provider === 'browser' && (
                <BrowserProviderPicker
                  selected={browserProvider}
                  onSelect={setBrowserProvider}
                />
              )}

              {/* Example prompts */}
              <div className="kim-examples" style={{ marginTop: settings.provider === 'browser' ? 24 : 0 }}>
                {EXAMPLE_PROMPTS.map((ex, i) => (
                  <button
                    key={i}
                    className="kim-example-card"
                    onClick={() => pickExample(ex.title)}
                  >
                    <span className="kim-example-card__icon">{ex.icon}</span>
                    <div className="kim-example-card__body">
                      <div className="kim-example-card__title">{ex.title}</div>
                      <div className="kim-example-card__hint">{ex.hint}</div>
                    </div>
                    <div className="kim-example-card__arrow">↗</div>
                  </button>
                ))}
              </div>

              {/* What Kim can do */}
              <div className="kim-capabilities">
                <div className="kim-capabilities__label">What Kim can do</div>
                <div className="kim-capabilities__grid">
                  {KIM_CAPABILITIES.map((cap, i) => (
                    <div key={i} className="kim-capability-item">
                      <span className="kim-capability-item__icon">{cap.icon}</span>
                      <div>
                        <div className="kim-capability-item__label">{cap.label}</div>
                        <div className="kim-capability-item__desc">{cap.desc}</div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              {/* Keyboard shortcuts */}
              <div className="kim-shortcuts">
                <div className="kim-shortcuts__label">Keyboard shortcuts</div>
                <div className="kim-shortcuts__row">
                  {KIM_SHORTCUTS.map((s, i) => (
                    <span key={i} className="kim-shortcut">
                      {s.keys.map((k, ki) => <kbd key={ki}>{k}</kbd>)}
                      <span className="kim-shortcut__label">{s.label}</span>
                    </span>
                  ))}
                </div>
              </div>
            </div>
          )}

          {/* Task error banner */}
          {taskError && taskError !== 'agent-error' && (
            <div className="kim-task-error" role="alert">
              <span className="kim-task-error__icon">⚠</span>
              <span>{taskError}</span>
            </div>
          )}
          {taskError === 'agent-error' && (
            <div className="kim-task-error" role="alert">
              <span className="kim-task-error__icon">⚠</span>
              <span>Kim ran into a problem and had to stop. Check the activity above for clues, or try rephrasing your task.</span>
            </div>
          )}

          {/* Activity feed */}
          {renderActivityFeed()}

          {/* Working indicator with timer */}
          {isRunning && (
            <div className="kim-working-indicator">
              <div className="kim-working-indicator__dots">
                <span /><span /><span />
              </div>
              <span className="kim-working-indicator__text">
                {cancelling ? 'Stopping Kim…' : 'Kim is working…'}
              </span>
              {!cancelling && tokenStats && (
                <span className="kim-working-indicator__tokens" title={`Input: ${tokenStats.input.toLocaleString()} · Output: ${tokenStats.output.toLocaleString()}`}>
                  {tokenStats.total.toLocaleString()} tokens
                </span>
              )}
              {!cancelling && elapsed > 0 && (
                <span className="kim-working-indicator__timer">{formatElapsed(elapsed)}</span>
              )}
            </div>
          )}

          <div ref={bottomRef} />
        </div>

        {renderComposer()}
      </div>
    );
  }

  // ── Existing session view ─────────────────────────────────────────────────────
  return (
    <div className="kim-chat">
      {/* Session header */}
      <div className="kim-session-header">
        <div className="kim-session-header__main">
          <div className="kim-session-header__row">
            <span className={`kim-session-badge kim-session-badge--${session!.session_type}`}>
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

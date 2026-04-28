import { useCallback, useEffect, useRef, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import type { SessionInfo, KimMessage, Settings, KimAccount } from '../types';
import { MessageBubble } from './MessageBubble';
import { SignalCard } from './ToolCallCard';
import { BrowserProviderPicker } from './BrowserProviderPicker';
import { Bloop, type BloopState } from './Bloop';
import { useChromaShader } from '../hooks/useChromaShader';
import { toast } from './Toast';

/** Subtle chrome WebGL backdrop, matching the onboarding shader. */
function ChatChromaBackdrop() {
  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  useChromaShader(canvasRef, containerRef);
  return (
    <div ref={containerRef} className="kim-chat__backdrop" aria-hidden="true">
      <canvas ref={canvasRef} className="kim-chat__backdrop-canvas" />
      <div className="kim-chat__backdrop-scrim" />
    </div>
  );
}

const MAX_ACTIVITY_ITEMS = 300;

// ── Activity feed ─────────────────────────────────────────────────────────────

interface ActivityItem {
  id: number;
  kind: 'tool' | 'info' | 'error' | 'success' | 'cancelled' | 'status';
  icon: string;
  text: string;
}

let _activityCounter = 0;

export function collapseMessages(msgs: KimMessage[]) {
  const res: {msg: KimMessage, retries: number}[] = [];
  for (const msg of msgs) {
    if (res.length > 0 && msg.role === 'assistant' && typeof msg.content === 'string') {
      const prev = res[res.length - 1];
      if (prev.msg.role === 'assistant' && typeof prev.msg.content === 'string') {
        const c1 = msg.content.trim().replace(/^(?:Gemini said|Claude said|Assistant said):\s*/i, '');
        const c2 = prev.msg.content.trim().replace(/^(?:Gemini said|Claude said|Assistant said):\s*/i, '');
        if (c1 === c2 && c1.startsWith('{')) {
          try {
            JSON.parse(c1);
            prev.retries += 1;
            continue;
          } catch {}
        }
      }
    }
    res.push({ msg, retries: 0 });
  }
  return res;
}

// ── Aggressive log suppression ───────────────────────────────────────────────
// Nothing that matches these rules should ever reach the activity feed.

/** Substring patterns that silently drop a line (case-insensitive match). */
const HIDDEN_SUBSTRINGS = [
  // screenshot / internal commands
  'take_screenshot', 'screenshot', 'capture_screen',
  // kimdir noise
  'INFO] kimdir', 'DEBUG] kimdir',
  // CLI noise
  'Running: ',
  // argparse / CLI usage block
  'usage: python', 'python -m orchestrator', 'optional arguments:',
  '--task TASK', '--provider {', '--max-iter', '--resume SESSION_ID',
  'argument --provider', 'invalid choice:', 'choose from',
  '[--task', '[--provider', '[--config', '[--max-iter', '[--resume', '[-h]',
  // BrowserProvider internal debug
  'BrowserProvider:', 'cdp_url=', "sites=['", 'headless =',
  // VoiceEngine
  'VoiceEngine initialized:', 'fallback chain:',
  // MCP / asyncio internals
  'mcp_server', 'mcp.shared', 'McpError', 'stdio_client',
  'asyncio.run(', 'ExceptionGroup:', 'TaskGroup',
  'mcp/client', 'mcp/shared',
  // Python venv / site-packages paths
  'site-packages', 'venv/lib/python', 'venv/bin/python',
  '/opt/homebrew/', '/usr/local/lib/python',
  // Node.js / npm deprecation warnings that leak into stderr
  '--trace-deprecation', 'DeprecationWarning', 'ExperimentalWarning',
  // Error while finding module
  'Error while finding module specification',
  // asyncio runner internals
  'return runner.run', 'return self._loop.run_until_complete',
  'runner.run(main)', 'loop.run_until_complete',
  // common traceback boilerplate
  '_run_module_as_main', '_run_code', '_cli_main',
  'mcp_agent_context', 'mcp_session_context', 'mcp_server_context',
  'session.initialize', 'send_request', '__aexit__',
  'return await anext', 'anext(self.gen)',
  'BaseExceptionGroup', 'raise BaseExceptionGroup',
  'unhandled errors in a TaskGroup',
  'return future.result()',
  'File "<frozen runpy>"',
  'getattr(logger, level.lower(), logger.info)(message)',
];

/** Regex patterns that silently drop a line. */
const HIDDEN_REGEX: RegExp[] = [
  /^\s*\|/,                        // exception group framing lines: "  |  ..."
  /^\s*\+[-+]+/,                   // exception group border: "+-+---..."
  /^\s*\^\^\^\^/,                  // Python error pointer: "    ^^^^^"
  /^\s*File\s+"[^"]+",\s+line\s+\d+/,  // traceback file lines
  /^\s*Traceback \(most recent call last\)/,
  /^\s*raise\s+\w/,
  /^\s*async with\s/,
  /^\s*await\s+(?:self|session|anext|runner)\./,
  /^\s*return\s+(?:await|self|runner|future)\./,
  /^\s*[A-Za-z_]+Error:/,          // any XxxError: line
  /^\s*[A-Za-z_.]+\.[A-Za-z_.]+Error:/, // module.XxxError:
  /python@\d+\.\d+/,               // Python version paths
  /\/Users\/\w+\/.*\/python[\d.]+\//,  // Python lib paths
  /\^+$/,                          // lines that are only carets
  /^[-+\s]*\d+\s+sub-exception/,  // "1 sub-exception"
];

function isNoiseLine(raw: string): boolean {
  // Strip the [err] prefix that appendRaw prepends before pattern-matching,
  // otherwise regex anchors like /^\s*\|/ never fire.
  const line = raw.startsWith('[err]') ? raw.slice(5).trimStart() : raw;
  const lower = line.toLowerCase();
  for (const sub of HIDDEN_SUBSTRINGS) {
    if (lower.includes(sub.toLowerCase())) return true;
  }
  for (const re of HIDDEN_REGEX) {
    if (re.test(line)) return true;
  }
  return false;
}

// NOTE: _recentRaw has been moved inside the ChatView component as a useRef
// to avoid cross-session pollution. See the `recentRawRef` ref below.

/** Friendly names + icons for known tool calls */
const TOOL_MAP: Record<string, { icon: string; label: (args: Record<string, unknown>) => string }> = {
  // File operations (actual MCP tool names)
  read_file:          { icon: '›', label: a => `Reading \`${basename(String(a.path ?? a.file_path ?? ''))}\`` },
  write_file:         { icon: '›', label: a => `Writing \`${basename(String(a.path ?? a.file_path ?? ''))}\`` },
  create_file:        { icon: '›', label: a => `Creating \`${basename(String(a.path ?? ''))}\`` },
  edit_file:          { icon: '›', label: a => `Editing \`${basename(String(a.path ?? a.file_path ?? ''))}\`` },
  append_file:        { icon: '›', label: a => `Appending to \`${basename(String(a.path ?? ''))}\`` },
  delete_file:        { icon: '›', label: a => `Deleting \`${basename(String(a.path ?? ''))}\`` },
  list_dir:           { icon: '›', label: a => `Listing \`${basename(String(a.path ?? a.directory ?? ''))}\`` },
  // Keep legacy alias in case older server versions use it
  list_directory:     { icon: '›', label: a => `Listing \`${basename(String(a.path ?? a.directory ?? ''))}\`` },
  find_files:         { icon: '›', label: a => `Searching for \`${String(a.pattern ?? a.query ?? '')}\`` },
  search_files:       { icon: '›', label: a => `Searching for \`${String(a.pattern ?? a.query ?? '')}\`` },
  search_in_files:    { icon: '›', label: a => `Searching code for \`${String(a.pattern ?? '')}\`` },
  grep:               { icon: '›', label: a => `Searching code for \`${String(a.pattern ?? '')}\`` },
  // Shell
  run_command:        { icon: '›', label: a => `Running \`${shorten(String(a.command ?? a.cmd ?? ''), 60)}\`` },
  run_terminal:       { icon: '›', label: a => `Running \`${shorten(String(a.command ?? ''), 60)}\`` },
  // Mouse / keyboard (actual MCP tool names)
  type_text:          { icon: '›', label: _a => 'Typing text' },
  click:              { icon: '›', label: _a => 'Clicking' },
  double_click:       { icon: '›', label: _a => 'Double-clicking' },
  right_click:        { icon: '›', label: _a => 'Right-clicking' },
  scroll:             { icon: '›', label: _a => 'Scrolling' },
  move_mouse:         { icon: '›', label: _a => 'Moving mouse' },
  press_key:          { icon: '›', label: a => `Pressing \`${String(a.key ?? '')}\`` },
  hotkey:             { icon: '›', label: a => `Pressing \`${String(a.keys ?? a.key ?? '')}\`` },
  drag:               { icon: '›', label: _a => 'Dragging' },
  // Apps
  open_application:   { icon: '›', label: a => `Opening ${String(a.app_name ?? a.application ?? '')}` },
  close_application:  { icon: '›', label: a => `Closing ${String(a.app_name ?? '')}` },
  // Clipboard
  read_clipboard:     { icon: '›', label: _a => 'Reading clipboard' },
  write_clipboard:    { icon: '›', label: _a => 'Writing to clipboard' },
  // Screen reading
  get_screen_text:    { icon: '›', label: _a => 'Reading screen text' },
  // Web
  web_search:         { icon: '›', label: a => `Searching the web for "${String(a.query ?? '')}"` },
  open_url:           { icon: '›', label: a => `Opening ${String(a.url ?? 'a web page')}` },
  // Git
  git_status:         { icon: '›', label: _a => 'Checking git status' },
  git_commit:         { icon: '›', label: a => `Git commit: "${shorten(String(a.message ?? ''), 50)}"` },
  git_diff:           { icon: '›', label: _a => 'Viewing git diff' },
  // User interaction
  ask_user:           { icon: '›', label: a => `Asking: "${String(a.question ?? '')}"` },
};

function basename(p: string): string {
  if (!p) return '';
  return p.split(/[/\\]/).pop() ?? p;
}

function shorten(s: string, max: number): string {
  return s.length > max ? s.slice(0, max) + '…' : s;
}

/** Map technical error text to something a non-technical user can understand */
export function friendlyError(raw: string): string {
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
  if (r.includes('invalid choice') || r.includes('argument --provider') || r.includes('exit status: 2'))
    return 'The selected provider isn\'t configured correctly. Open Settings → AI to choose a provider.';
  // Strip noise from log lines and return a condensed version
  const cleaned = raw
    .replace(/\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2},?\d*\s*/g, '')
    .replace(/\[(ERROR|WARN|INFO|DEBUG|TOOL|CRITICAL)\]\s*/g, '')
    .replace(/orchestrator\.\w+:\s*/g, '')
    .trim();
  return cleaned.length > 0 && cleaned.length < 200 ? cleaned : 'Something went wrong. Check your settings and try again.';
}

function parseLogLine(raw: string, id: number): ActivityItem | null {
  if (!raw.trim()) return null;

  // Aggressive noise suppression first — catches tracebacks, internal debug, etc.
  if (isNoiseLine(raw)) return null;

  // Truncated meta-lines
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

  // Explicit completion/help signals from the agent loop
  const taskCompleteMatch = stripped.match(/(?:^|\b)TASK_COMPLETE:\s*(.+)$/i);
  if (taskCompleteMatch) {
    const summary = taskCompleteMatch[1].trim();
    return {
      id,
      kind: 'success',
      icon: '✓',
      text: summary || 'Task completed',
    };
  }

  const needHelpMatch = stripped.match(/(?:^|\b)NEED_HELP:\s*(.+)$/i);
  if (needHelpMatch) {
    const reason = needHelpMatch[1].trim();
    return {
      id,
      kind: 'error',
      icon: '⚠',
      text: friendlyError(reason || 'Kim needs your help to continue.'),
    };
  }

  // [TOOL] lines — 'module:' prefix is optional in newer agent log format
  const toolMatch = stripped.match(/\[TOOL\]\s+(?:[\w.]+:\s+)?(\w+)\((.{0,200})\)/);
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
    return { id, kind: 'tool', icon: '›', text: `Using tool: \`${toolName}\`` };
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
    // Filter out INFO/DEBUG noise entirely
    if (stripped.match(/\[(INFO|DEBUG)\]/)) return null;
    // Non-classified stderr — only surface very short, clearly user-facing messages.
    // Long lines are almost always stack trace noise or internal logging.
    const msg = stripped.replace(/\[[\w]+\]\s+[\w.]*:\s*/, '').trim();
    if (!msg || msg.length > 80) return null;
    // Drop lines that look like code / paths / stack frames
    if (/[/\\].+\.py/.test(msg)) return null;
    if (/^\s*at\s/.test(msg)) return null;
    return { id, kind: 'status', icon: '·', text: msg };
  }

  return null;
}

// ── Greeting ──────────────────────────────────────────────────────────────────

const EXAMPLE_PROMPTS: { title: string; hint: string }[] = [
  { title: 'Summarize the PDF on my desktop', hint: 'Read and extract key insights' },
  { title: 'Find all TODOs in my project', hint: 'Search files and list them' },
  { title: 'Stage, commit, and push my changes', hint: 'Full git workflow' },
  { title: 'Search the web and write a report', hint: 'Browse and summarize' },
];

const KIM_CAPABILITIES = [
  { label: 'See your screen', desc: 'Kim takes screenshots to understand what\'s happening' },
  { label: 'Control your mouse', desc: 'Click buttons, drag files, navigate any app' },
  { label: 'Type and edit', desc: 'Write code, fill forms, compose emails' },
  { label: 'Manage files', desc: 'Read, write, move, search files on your computer' },
  { label: 'Browse the web', desc: 'Search, visit websites, extract information' },
  { label: 'Run commands', desc: 'Terminal commands, scripts, git operations' },
];

const KIM_SHORTCUTS = [
  { keys: ['⌘', 'N'], label: 'New chat' },
  { keys: ['⌘', 'B'], label: 'Toggle sidebar' },
  { keys: ['⌘', ','], label: 'Settings' },
  { keys: ['⇧', '↵'], label: 'New line in message' },
];

const PROVIDER_LABELS: Record<string, string> = {
  claude: 'Claude',
  openai: 'OpenAI',
  gemini: 'Gemini',
  deepseek: 'DeepSeek',
  browser: 'Browser',
  'browser:claude': 'Browser Claude',
  'browser:chatgpt': 'Browser ChatGPT',
  'browser:gemini': 'Browser Gemini',
  'browser:grok': 'Browser Grok',
  'browser:deepseek': 'Browser DeepSeek',
  'browser:custom': 'Browser Custom',
};

interface PendingTask {
  id: number;
  text: string;
  provider: string;
}

function makeConversationId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function providerLabel(provider: string): string {
  return PROVIDER_LABELS[provider] ?? provider;
}

function getGreeting(name: string): string {
  const hour = new Date().getHours();
  if (hour < 5) return `Late night, ${name}`;
  if (hour < 12) return `Good morning, ${name}`;
  if (hour < 17) return `Good afternoon, ${name}`;
  if (hour < 21) return `Good evening, ${name} (test)`;
  return `Evening, ${name} (test)`;
}

// ── Blobby Loaders (3, 6, 12, 15, 20) ────────────────────────────────────────

/** Renders one of the 5 organic blob loading animations. Size is ~24×24px. */
function BlobLoader({ which }: { which: 3 | 6 | 12 | 15 | 20 }) {
  // Colors inherit from parent via currentColor
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
  // 20 — mitosis
  return (
    <svg viewBox="0 0 100 100" className="kim-blob-loader kim-blob-l20" aria-hidden="true">
      <g style={{ filter: 'url(#kim-goo)' }}>
        <circle className="kim-blob-l20__a" cx="50" cy="50" r="18" fill="currentColor" />
        <circle className="kim-blob-l20__b" cx="50" cy="50" r="18" fill="currentColor" />
      </g>
    </svg>
  );
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
  activeTab: 'chat' | 'code';
  activeProjectPath?: string | null;
}

// ── Component ─────────────────────────────────────────────────────────────────

export function ChatView({ session, newChatMode, settings, onTaskDone, account, activeTab, activeProjectPath }: Props) {
  const [messages, setMessages] = useState<KimMessage[]>([]);
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [newestMsgIdx, setNewestMsgIdx] = useState<number | null>(null);
  const [localProvider, setLocalProvider] = useState<string | null>(null);
  const [messageReloadNonce, setMessageReloadNonce] = useState(0);
  const prevMsgCountRef = useRef(0);
  const [taskInput, setTaskInput] = useState('');
  const [isRunning, setIsRunning] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [activity, setActivity] = useState<ActivityItem[]>([]);
  const [taskError, setTaskError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [tokenStats, setTokenStats] = useState<{ input: number; output: number; total: number } | null>(null);
  const [queuedTasks, setQueuedTasks] = useState<PendingTask[]>([]);
  const [interruptTask, setInterruptTask] = useState<PendingTask | null>(null);
  const [lastFailedTask, setLastFailedTask] = useState<PendingTask | null>(null);
  const [autoFollowOutput, setAutoFollowOutput] = useState(true);
  // Which browser AI provider is selected (only relevant when settings.provider === 'browser')
  const [browserProvider, setBrowserProvider] = useState('claude');
  const [conversationId] = useState(() => makeConversationId());
  const activeResumeSessionId = session?.session_id ?? conversationId;

  // Live conversation history for new-chat mode — persists across task runs
  // and doesn't get wiped by the disk-based message reload.
  const [liveHistory, setLiveHistory] = useState<{role: 'user' | 'assistant'; content: string}[]>([]);

  const bottomRef = useRef<HTMLDivElement>(null);
  const outputRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const startTimeRef = useRef<number | null>(null);
  const currentTaskRef = useRef<PendingTask | null>(null);
  // Tracks the most recently submitted task — never cleared, so retry always works
  // even when the task "succeeded" with a NEED_HELP (e.g., 409 sign-in required).
  const lastRunTaskRef = useRef<PendingTask | null>(null);
  const previousProviderRef = useRef(settings.provider);
  // Set to true when the kim-agent-done event fires; prevents the invoke()
  // rejection from double-reporting errors.
  const doneHandledRef = useRef(false);
  // Set to true in handleCancel() BEFORE the cancel invoke so the kim-agent-done
  // listener skips the error banner when the task was intentionally stopped.
  // Must be a ref (not a closure-local let) because kim-agent-done always fires
  // ~100ms BEFORE kim-agent-cancelled (Rust emits done inside child.wait(), then
  // the cancel poller emits cancelled), so a closure variable would still be false.
  const cancelFlagRef = useRef(false);
  const needHelpFlagRef = useRef(false);

  // ── Deduplication (per-session, not module-global) ───────────────────────
  // Python writes many lines to both stdout AND stderr, causing duplicates.
  // We track exact duplicates seen within 800 ms and drop them.
  const recentRawRef = useRef<Map<string, number>>(new Map());
  const isDuplicate = (raw: string): boolean => {
    const map = recentRawRef.current;
    const now = Date.now();
    const last = map.get(raw);
    if (last !== undefined && now - last < 800) return true;
    map.set(raw, now);
    // Prune stale entries to prevent unbounded memory growth
    if (map.size > 200) {
      const cutoff = now - 1600;
      for (const [k, v] of map) if (v < cutoff) map.delete(k);
    }
    return false;
  };

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
    setLiveHistory([]);
    invoke<KimMessage[]>('load_session_messages', {
      sessionId: session.session_id,
      kimDir: settings.kim_sessions_dir || null,
      clawDir: session.session_type === 'claw' && session.project_path 
        ? `${session.project_path}/.claw/sessions` 
        : settings.claw_sessions_dir || null,
    })
      .then(msgs => {
        const prev = prevMsgCountRef.current;
        const lastAssistantIdx = msgs.reduceRight((found, m, i) =>
          found === -1 && m.role === 'assistant' ? i : found, -1);
        // Only animate if this is a refresh after task done (new messages appeared)
        if (prev > 0 && msgs.length > prev && lastAssistantIdx >= prev) {
          setNewestMsgIdx(lastAssistantIdx);
        } else {
          setNewestMsgIdx(null);
        }
        prevMsgCountRef.current = msgs.length;
        setMessages(msgs);
      })
      .catch(err => console.error('Failed to load messages:', err))
      .finally(() => setLoadingMessages(false));
  }, [session, settings.kim_sessions_dir, settings.claw_sessions_dir, messageReloadNonce]);

  // ── Scroll behavior ─────────────────────────────────────────────────────────
  useEffect(() => {
    const scroller = outputRef.current;
    if (!newChatMode || !scroller) return;

    const onScroll = () => {
      const distanceFromBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
      setAutoFollowOutput(distanceFromBottom < 80);
    };

    onScroll();
    scroller.addEventListener('scroll', onScroll, { passive: true });
    return () => scroller.removeEventListener('scroll', onScroll);
  }, [newChatMode]);

  useEffect(() => {
    // If we're starting a brand new chat and there's no activity yet,
    // don't scroll to the bottom (otherwise it skips the greeting/examples).
    if (newChatMode && activity.length === 0) {
      return;
    }
    if (!newChatMode) {
      // For existing sessions, or when newChatMode starts generating activity
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
      return;
    }
    if (autoFollowOutput) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages, activity, newChatMode, autoFollowOutput]);

  // ── Reset state when entering a new chat ─────────────────────────────────────
  // This ensures that if an error or activity is present from a previous run,
  // entering new-chat mode always shows a clean slate.
  useEffect(() => {
    if (newChatMode) {
      setActivity([]);
      setTaskError(null);
      setTokenStats(null);
      setElapsed(0);
      // Note: do NOT set isRunning=false here — if a task is actually still
      // running we should show that state. But if not running, we want clean UI.
    }
  }, [newChatMode]);

  // ── Focus on new chat ───────────────────────────────────────────────────────
  useEffect(() => {
    if (newChatMode) {
      const t = setTimeout(() => textareaRef.current?.focus(), 50);
      return () => clearTimeout(t);
    }
  }, [newChatMode]);

  useEffect(() => {
    const prev = previousProviderRef.current;
    if (prev !== settings.provider && (activity.length > 0 || isRunning)) {
      toast(
        `Provider changed from ${providerLabel(prev)} to ${providerLabel(settings.provider)}. ` +
          'Kim will continue this chat with shared memory on your next message.',
        'info',
        7000,
      );
    }
    previousProviderRef.current = settings.provider;
  }, [settings.provider, activity.length, isRunning]);

  // ── Append to activity feed ─────────────────────────────────────────────────
  function appendRaw(line: string) {
    if (isDuplicate(line)) return;   // drop stdout/stderr duplicates
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
    if (!item) {
      if (line.includes('[UI] SCREENSHOT_FLASH')) {
        invoke('show_screenshot_flash').catch(() => {});
        invoke('hide_main_window').catch(() => {});
      } else if (line.includes('[UI] HIDE')) {
        invoke('hide_main_window').catch(() => {});
      } else if (line.includes('[UI] SHOW')) {
        invoke('show_main_window').catch(() => {});
      }
      return;
    }

    const needHelpMatch = line.match(/(?:^|\b)NEED_HELP:\s*(.+)$/i);
    if (needHelpMatch) {
      needHelpFlagRef.current = true;
      setTaskError(needHelpMatch[1].trim() || 'Kim needs your help to continue.');
      if (lastRunTaskRef.current) {
        setLastFailedTask(lastRunTaskRef.current);
      }
      return; // Skip adding to activity feed to avoid duplicate error messages
    }

    setActivity(prev => {
      if (item.kind === 'success') return prev; // Skip adding to activity feed to avoid duplicating the assistant bubble
      const next = [...prev, item];
      if (next.length > MAX_ACTIVITY_ITEMS) return next.slice(next.length - MAX_ACTIVITY_ITEMS);
      return next;
    });

    // Capture success results as assistant bubbles in liveHistory
    if (item.kind === 'success') {
      setLiveHistory(prev => [...prev, { role: 'assistant', content: item.text }]);
    }
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

    listen<boolean>('kim-agent-done', event => {
      const wasCancelled = cancelFlagRef.current;
      const hadNeedHelp = needHelpFlagRef.current;
      doneHandledRef.current = true;
      cancelFlagRef.current = false; // reset for next task
      needHelpFlagRef.current = false; // reset
      setIsRunning(false);
      setCancelling(false);
      // Existing session view is now interactive, so reload message history
      // after every run completion to reflect newly appended turns.
      setMessageReloadNonce(v => v + 1);
      // Always refresh sessions — failed runs still create session files.
      onTaskDoneRef.current();
      if (!event.payload && !wasCancelled) {
        if (!hadNeedHelp) {
          setTaskError('agent-error');
        }
        if (lastRunTaskRef.current) {
          setLastFailedTask(lastRunTaskRef.current);
        }
      } else if (event.payload && !hadNeedHelp) {
        setLastFailedTask(null);
      }
      currentTaskRef.current = null;
    }).then(fn => { unlistenDone = fn; });

    listen<boolean>('kim-agent-cancelled', () => {
      cancelFlagRef.current = true; // safety: also set here in case done fires later
      appendRaw('⏹ Task cancelled');
      setIsRunning(false);
      setCancelling(false);
      currentTaskRef.current = null;
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

  const queueEnabled = Boolean(settings.allow_message_queue);

  const resolveProvider = useCallback((): string => {
    const p = localProvider ?? settings.provider;
    // If localProvider is already "browser:claude" etc., pass it through
    if (p.startsWith('browser:')) return p;
    if (p === 'browser') return `browser:${browserProvider}`;
    return p || 'browser';
  }, [localProvider, settings.provider, browserProvider]);

  const makePendingTask = useCallback((text: string, providerOverride?: string): PendingTask => {
    return {
      id: Date.now() + Math.floor(Math.random() * 1000),
      text,
      provider: providerOverride ?? resolveProvider(),
    };
  }, [resolveProvider]);

  const runPendingTask = useCallback(async (pending: PendingTask) => {
    doneHandledRef.current = false;
    // Reset cancel flag at the start of every run. The kim-agent-cancelled
    // listener sets it to true AFTER kim-agent-done has already reset it,
    // leaving a stale true that would suppress the error banner for the
    // next task if it failed for a real (non-cancel) reason.
    cancelFlagRef.current = false;
    needHelpFlagRef.current = false;
    currentTaskRef.current = pending;
    lastRunTaskRef.current = pending;
    setIsRunning(true);
    setActivity([]);
    setTaskError(null);
    setTokenStats(null);
    setCancelling(false);
    setAutoFollowOutput(true);

    // Add user message to live history for chat bubble display
    setLiveHistory(prev => [...prev, { role: 'user', content: pending.text }]);

    try {
      await invoke('send_task', {
        task: pending.text,
        provider: pending.provider,
        projectRoot: (activeTab === 'code' && activeProjectPath) ? activeProjectPath : (settings.project_root || null),
        resumeSessionId: activeResumeSessionId,
      });
    } catch (err) {
      // kim-agent-done fires BEFORE invoke() rejects on process failure.
      // If the event already handled everything, skip the duplicate rejection.
      if (!doneHandledRef.current) {
        setIsRunning(false);
        setTaskError(friendlyError(String(err)));
        setLastFailedTask(pending);
        onTaskDoneRef.current(); // refresh sidebar even on invoke-level failures
      }
    }
  }, [activeResumeSessionId, settings.project_root, activeTab, activeProjectPath]);

  useEffect(() => {
    if (isRunning) return;

    if (interruptTask) {
      const next = interruptTask;
      setInterruptTask(null);
      void runPendingTask(next);
      return;
    }

    if (queuedTasks.length > 0) {
      const [next, ...rest] = queuedTasks;
      setQueuedTasks(rest);
      void runPendingTask(next);
    }
  }, [isRunning, interruptTask, queuedTasks, runPendingTask]);

  async function handleCancel() {
    if (!isRunning || cancelling) return;
    setCancelling(true);
    // Set the cancel flag BEFORE sending the signal. kim-agent-done fires
    // ~100ms before kim-agent-cancelled (Rust emits done in child.wait()),
    // so the flag must be true when done arrives or the error banner appears.
    cancelFlagRef.current = true;
    try {
      await invoke('cancel_task');
    } catch (err) {
      setCancelling(false);
      cancelFlagRef.current = false;
      setTaskError(friendlyError(String(err)));
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const task = taskInput.trim();
    if (!task) return;

    const pending = makePendingTask(task);

    setTaskInput('');
    if (textareaRef.current) textareaRef.current.style.height = 'auto';

    if (isRunning) {
      if (queueEnabled) {
        const nextCount = queuedTasks.length + 1;
        setQueuedTasks(prev => [...prev, pending]);
        toast(`Queued message #${nextCount}. Kim will run it automatically next.`, 'info', 3000);
      } else {
        setQueuedTasks([]);
        setInterruptTask(pending);
        toast('Interrupting current task and replacing it with your latest message.', 'warning', 4500);
        if (!cancelling) {
          await handleCancel();
        }
      }
      return;
    }

    await runPendingTask(pending);
  }

  async function handleRetryLast() {
    let taskToRetry = lastFailedTask ?? lastRunTaskRef.current;
    
    // If not in current lifecycle, find the last user message from the loaded history
    if (!taskToRetry && messages.length > 0) {
      for (let i = messages.length - 1; i >= 0; i--) {
        if (messages[i].role === 'user') {
          const msg = messages[i];
          let text = typeof msg.content === 'string'
            ? msg.content
            : msg.content.filter(b => b.type === 'text').map(b => (b as any).text).join('\n');
            
          if (text.startsWith('Task: ')) {
            text = text.substring(6).trim();
          }
          taskToRetry = { id: 0, text, provider: resolveProvider() };
          break;
        }
      }
    }
    
    if (!taskToRetry) return;
    const retryTask = makePendingTask(taskToRetry.text, resolveProvider());
    setTaskError(null);

    if (isRunning) {
      if (queueEnabled) {
        setQueuedTasks(prev => [...prev, retryTask]);
        toast('Retry queued. It will run after the current task.', 'info', 3000);
      } else {
        setQueuedTasks([]);
        setInterruptTask(retryTask);
        toast('Retry will run after current task is interrupted.', 'warning', 4000);
        if (!cancelling) {
          await handleCancel();
        }
      }
      return;
    }

    await runPendingTask(retryTask);
  }

  function handleBrowserProviderSelect(nextProvider: string) {
    if (nextProvider === browserProvider) return;
    const previous = browserProvider;
    setBrowserProvider(nextProvider);
    setLocalProvider(`browser:${nextProvider}`);

    if (activity.length > 0 || isRunning || queuedTasks.length > 0 || interruptTask) {
      toast(
        `Switched from ${providerLabel(`browser:${previous}`)} to ${providerLabel(`browser:${nextProvider}`)}. ` +
          'Next message keeps this chat memory and uses the new provider.',
        'info',
        7500,
      );
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
    // Highlight diff tokens only when they are standalone words (e.g. " +12 "),
    // not embedded math like "2+2".
    const parts = text.split(/(`[^`]+`|(?:^|\s)(?:[+-]\d+)(?=$|\s))/g);
    if (parts.length === 1) return text;
    return parts.map((p, i) => {
      if (p.startsWith('`') && p.endsWith('`'))
        return <code key={i}>{p.slice(1, -1)}</code>;
      if (/^\s*\+\d+\s*$/.test(p))
        return <span key={i} className="kim-diff-added">{p}</span>;
      if (/^\s*-\d+\s*$/.test(p))
        return <span key={i} className="kim-diff-removed">{p}</span>;
      return p;
    });
  }

  function renderActivityFeed() {
    if (activity.length === 0) return null;
    return (
      <div className="kim-msg-row kim-msg-row--assistant kim-msg-row--live">
        <div className="kim-bubble kim-bubble--assistant kim-bubble--live">
          <div className="kim-activity-feed">
            {activity.map(item => (
              <div key={item.id} className={`kim-activity-item kim-activity-item--${item.kind}`}>
                <span className="kim-activity-item__icon" aria-hidden="true">{item.icon}</span>
                <span className="kim-activity-item__text">{renderActivityText(item.text)}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    );
  }

  // ── Composer ─────────────────────────────────────────────────────────────────

  function renderComposer() {
    return (
      <form className="kim-composer" onSubmit={handleSubmit}>
        <div className="kim-composer__row">
        <div className={'kim-composer__box' + (isRunning ? ' kim-composer__box--running' : '')}>
          <textarea
            ref={textareaRef}
            value={taskInput}
            onChange={handleTextareaInput}
            placeholder={isRunning
              ? (queueEnabled ? 'Kim is working — type now, Send adds to queue' : 'Kim is working — Send interrupts current task')
              : 'Message Kim…'}
            rows={1}
            onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                void handleSubmit(e as unknown as React.FormEvent);
              }
            }}
            className="kim-composer__textarea"
          />

          <div className="kim-composer__actions">
            {isRunning && (
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
            )}
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
          </div>
        </div>
          <div className="kim-composer__bloop" aria-hidden="true">
            <div className="kim-composer__bloop-inner">
              <Bloop
                state={(taskError || cancelling
                  ? 'error'
                  : isRunning
                    ? 'processing'
                    : taskInput.trim()
                      ? 'thinking'
                      : 'idle') as BloopState}
              />
            </div>
          </div>
        </div>
        <div className="kim-composer__hint">
          <span>
            {isRunning
              ? (queueEnabled ? 'Send queues this message' : 'Send interrupts current task')
              : <><kbd>↵</kbd> to send</>}
          </span>
          <span className="kim-composer__hint-sep">·</span>
          <span><kbd>⇧</kbd>+<kbd>↵</kbd> for new line</span>
          <span className="kim-composer__hint-sep">·</span>
          <span className="kim-composer__provider-pill">
            <select 
              value={resolveProvider()} 
              onChange={async (e) => {
                const val = e.target.value;
                if (val.startsWith('browser:')) {
                  // Browser sub-provider: set both localProvider and browserProvider
                  setLocalProvider(val);
                  const sub = val.split(':')[1];
                  setBrowserProvider(sub);
                  // Navigate the existing browser window (if open) to the new provider
                  const urlMap: Record<string, string> = {
                    claude: 'https://claude.ai',
                    chatgpt: 'https://chatgpt.com',
                    gemini: 'https://gemini.google.com',
                    grok: 'https://grok.com',
                    deepseek: 'https://chat.deepseek.com',
                  };
                  const newUrl = urlMap[sub];
                  if (newUrl) {
                    try {
                      await invoke<boolean>('navigate_browser_window_if_open', { url: newUrl });
                    } catch (_) {}
                  }
                } else {
                  setLocalProvider(val);
                }
              }}
              className="kim-composer__provider-select"
            >
              <optgroup label="Browser (free — uses your sign-in)">
                <option value="browser:claude">Browser: Claude</option>
                <option value="browser:chatgpt">Browser: ChatGPT</option>
                <option value="browser:gemini">Browser: Gemini</option>
                <option value="browser:grok">Browser: Grok</option>
                <option value="browser:deepseek">Browser: DeepSeek</option>
              </optgroup>
              <optgroup label="API (requires API key)">
                <option value="claude">Claude API</option>
                <option value="openai">OpenAI API</option>
                <option value="gemini">Gemini API</option>
                <option value="deepseek">DeepSeek API</option>
              </optgroup>
            </select>
            <svg className="kim-composer__provider-chevron" viewBox="0 0 10 10" width="8" height="8" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
              <path d="M2 4l3 3 3-3" />
            </svg>
          </span>
          {resolveProvider().startsWith('browser:') && resolveProvider().split(':')[1] !== 'custom' && (
            <>
              <span className="kim-composer__hint-sep">·</span>
              <button
                type="button"
                className="kim-composer__signin-btn"
                onClick={async () => {
                  const p = resolveProvider().split(':')[1];
                  const providerName = p.charAt(0).toUpperCase() + p.slice(1);
                  let url = '';
                  if (p === 'claude') url = 'https://claude.ai';
                  else if (p === 'chatgpt') url = 'https://chatgpt.com';
                  else if (p === 'gemini') url = 'https://gemini.google.com';
                  else if (p === 'grok') url = 'https://grok.com';
                  else if (p === 'deepseek') url = 'https://chat.deepseek.com';
                  
                  if (url) {
                    try {
                      await invoke<string>('open_browser_signin_window', { url, providerName });
                      await invoke('show_browser_window').catch(() => {});
                      toast(`${providerName} opened! Close the window when you're done signing in.`, 'info', 7000);
                    } catch (err) {
                      toast(typeof err === 'string' ? err : `Could not open ${providerName}.`, 'error', 5000);
                    }
                  }
                }}
              >
                Sign into {resolveProvider().split(':')[1].charAt(0).toUpperCase() + resolveProvider().split(':')[1].slice(1)}
              </button>
            </>
          )}
          {(queuedTasks.length > 0 || interruptTask) && (
            <>
              <span className="kim-composer__hint-sep">·</span>
              <span>
                {interruptTask
                  ? '1 interrupt pending'
                  : `${queuedTasks.length} queued`}
              </span>
            </>
          )}
        </div>
      </form>
    );
  }

  // ── Empty welcome state ──────────────────────────────────────────────────────
  if (!newChatMode && !session) {
    return (
      <div className="kim-chat">
        <ChatChromaBackdrop />
        <div className="kim-empty-welcome">
          <div className="kim-greeting__text">
            {activeTab === 'code' ? 'Start a new Code session' : getGreeting(account.display_name.split(' ')[0])}
          </div>
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

  const hasStarted = isRunning || activity.length > 0 || !!taskError;

  // ── New chat mode ─────────────────────────────────────────────────────────────
  if (newChatMode) {
    return (
      <div className="kim-chat">
        <ChatChromaBackdrop />
        <div className="kim-chat__output" ref={outputRef}>


          {!hasStarted && (
            <div className="kim-new-chat-empty">
              <div className="kim-new-chat-empty__badge">
                <span className="kim-pulse-dot kim-pulse-dot--accent" />
                Ready
              </div>
              <div className="kim-new-chat-empty__title kim-greeting">
                {activeTab === 'code' ? 'Start a new Code session' : getGreeting(account.display_name)}
              </div>
              <div className="kim-new-chat-empty__subtitle">
                {activeTab === 'code'
                  ? 'Select a codebase from the sidebar, or just ask Kim to analyze a specific path.'
                  : 'Describe any task in plain English below — Kim will figure out how to do it.'}
              </div>

              {(localProvider?.startsWith('browser') || (!localProvider && settings.provider === 'browser')) && (
                <BrowserProviderPicker
                  selected={browserProvider}
                  onSelect={handleBrowserProviderSelect}
                />
              )}

              {/* Example prompts */}
              <div className="kim-examples" style={{ marginTop: (localProvider?.startsWith('browser') || (!localProvider && settings.provider === 'browser')) ? 24 : 0 }}>
                {EXAMPLE_PROMPTS.map((ex, i) => (
                  <button
                    key={i}
                    className="kim-example-card"
                    onClick={() => pickExample(ex.title)}
                  >
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

          {/* Live conversation history */}
          {collapseMessages(liveHistory).map(({msg, retries}, i) => {
            // Show activity feed right after the last user message (current task)
            const showActivityAfter = msg.role === 'user' && !liveHistory.slice(i + 1).some(m => m.role === 'assistant');
            return (
              <div key={`live-${i}`}>
                <MessageBubble
                  message={msg}
                  animate={i === liveHistory.length - 1}
                  typingAnimation={settings.typing_animation ?? 'none'}
                  onRetry={handleRetryLast}
                  retries={retries}
                />
                {showActivityAfter && renderActivityFeed()}
              </div>
            );
          })}

          {/* Error / retry — inside an assistant-aligned row */}
          {taskError && taskError !== 'agent-error' && (
            <div className="kim-msg-row kim-msg-row--assistant">
              <div className="kim-task-error" role="alert">
                <span className="kim-task-error__icon">⚠</span>
                <span>{taskError}</span>
                {lastRunTaskRef.current && (
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
                {lastRunTaskRef.current && (
                  <button type="button" className="kim-task-error__retry" onClick={() => void handleRetryLast()}>
                    Retry
                  </button>
                )}
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

          {/* Working indicator with blobby loader */}
          {isRunning && (
            <div className="kim-working-indicator">
              {/* Goo filter used by loaders 6, 15, 20 */}
              <svg width="0" height="0" style={{ position: 'absolute' }}>
                <defs>
                  <filter id="kim-goo">
                    <feGaussianBlur in="SourceGraphic" stdDeviation="3" result="blur" />
                    <feColorMatrix in="blur" values="1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 0 20 -9" result="goo" />
                    <feComposite in="SourceGraphic" in2="goo" operator="atop" />
                  </filter>
                </defs>
              </svg>
              <BlobLoader which={cancelling ? 3 : 15} />
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

        {renderComposer()}
      </div>
    );
  }

  // ── Existing session view ─────────────────────────────────────────────────────
  return (
    <div className="kim-chat">
      <ChatChromaBackdrop />
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
              {(() => {
                const match = session!.summary!.match(/^Task:.*?(?:\.\s*Result:\s*|\nResult:\s*)([\s\S]*)$/i);
                return match ? match[1].trim() : session!.summary;
              })()}
            </div>
          )}
        </div>
      </div>

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
            {collapseMessages(messages).map(({msg, retries}, i) => (
              <MessageBubble
                key={i}
                message={msg}
                animate={i === newestMsgIdx}
                typingAnimation={settings.typing_animation ?? 'none'}
                onRetry={handleRetryLast}
                retries={retries}
              />
            ))}

            {/* Newly added messages in this session */}
            {collapseMessages(liveHistory).map(({msg, retries}, i) => (
              <MessageBubble
                key={`live-${i}`}
                message={msg}
                animate={i === liveHistory.length - 1}
                typingAnimation={settings.typing_animation ?? 'none'}
                onRetry={handleRetryLast}
                retries={retries}
              />
            ))}
            
            {/* Activity feed and errors */}
            {renderActivityFeed()}

            {/* Working indicator with blobby loader */}
            {isRunning && (
              <div className="kim-working-indicator">
                {/* Goo filter used by loaders 6, 15, 20 */}
                <svg width="0" height="0" style={{ position: 'absolute' }}>
                  <defs>
                    <filter id="kim-goo">
                      <feGaussianBlur in="SourceGraphic" stdDeviation="3" result="blur" />
                      <feColorMatrix in="blur" values="1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 0 20 -9" result="goo" />
                      <feComposite in="SourceGraphic" in2="goo" operator="atop" />
                    </filter>
                  </defs>
                </svg>
                <BlobLoader which={cancelling ? 3 : 15} />
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

            {taskError && (
              <div className="kim-msg-row kim-msg-row--assistant">
                <div style={{ maxWidth: '78%', minWidth: 0 }}>
                  <SignalCard kind="error" text={taskError} onAction={handleRetryLast} actionLabel="Resend Task" />
                </div>
              </div>
            )}
          </>
        )}
        <div ref={bottomRef} />
      </div>

      {renderComposer()}
    </div>
  );
}

/**
 * Pure utility functions and constants for ChatView.
 *
 * Extracted from ChatView.tsx (Phase 3 restructure).
 * No JSX, no hooks, no React state — safe to test in isolation.
 *
 * Exported functions that other files already import from ChatView:
 *   collapseMessages, groupCodexMessages, friendlyError
 * ChatView.tsx re-exports these so callers are unchanged.
 */

import type { KimMessage, TextBlock, ToolUseBlock, ToolResultBlock, SessionInfo } from '../../types';
import type { ActivityItem, LivePlanParsed, TouchedFile, CodexRunGroup } from './types';
import { LogTags } from '../../types/events.gen'; // K5: tag vocabulary from generated manifest

// ── Simple helpers ────────────────────────────────────────────────────────────

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return s === 0 ? `${m}m` : `${m}m ${s}s`;
}

// Provider brands that must never surface in Kim's own narration. The assistant
// always speaks as "Kim", regardless of which browser model is actually behind it.
const PROVIDER_BRANDS = 'Gemini|Claude|ChatGPT|Grok|DeepSeek';

/**
 * Rewrite Kim-authored narration (status lines, thinking trace) so it reads as
 * "Kim" and never exposes the underlying browser model — the user shouldn't feel
 * like Kim is routing through Gemini/etc.
 *
 * NOTE: only use this on Kim's own framing text. Do NOT run it over a model's raw
 * ANSWER, which may legitimately mention these names (e.g. "tell me about Gemini").
 */
export function speakAsKimNarration(t: string): string {
  if (!t) return t;
  const B = PROVIDER_BRANDS;
  // Honesty limit (audit item 3): only rewrite Kim's OWN pipeline framing —
  // "<brand> is thinking…" spinners and "sending to <brand>" routing lines.
  // The old blanket rules ("<brand> said" → "Kim", possessives, and any bare
  // brand mention → "Kim") rewrote model-authored content (reasoning/status
  // text that legitimately mentions Claude/Gemini/etc., e.g. "Reading the
  // Claude API docs") and were removed as a dishonest content rewrite.
  return t
    // "Gemini is/still thinking… (3s)" → "Kim is thinking…"
    .replace(new RegExp(`\\b(?:${B})\\s+(?:is\\s+)?(?:still\\s+)?thinking(?:…|\\.\\.\\.)?(?:\\s+\\(\\d+s\\))?`, 'gi'), 'Kim is thinking…')
    // "sending to / routing through / powered by Gemini" → neutral. Narrow
    // verb list on purpose: generic "via/using/from <brand>" often appears in
    // real content ("install it via Gemini CLI") and must not be rewritten.
    .replace(new RegExp(`\\b(?:sending to|routed through|routing through|powered by)\\s+(?:${B})\\b`, 'gi'), 'Kim is working')
    // collapse a doubled "Kim Kim" the replacements can create
    .replace(/\bKim\s+Kim\b/g, 'Kim')
    .trim();
}

export function cleanActivityText(t: string): string {
  let cleaned = speakAsKimNarration(
    t
      .replace(/<think>[\s\S]*?<\/think>/gi, '')
      .replace(/(?:Gemini said|Claude said|Assistant said|ChatGPT said|Grok said|DeepSeek said):?\s*/ig, '')
  ).trim();
  try {
    if (cleaned.startsWith('{') && cleaned.endsWith('}')) {
      const parsed = JSON.parse(cleaned);
      if (typeof parsed.text === 'string') {
        cleaned = parsed.text;
      }
    }
  } catch {}
  return cleaned;
}

export function cleanAssistantAnswerText(t: string): string {
  let cleaned = t
    .replace(/<think>[\s\S]*?<\/think>/gi, '')
    .replace(/\s*\[END_OF_RESPONSE\]\s*$/i, '')
    .replace(/(?:Gemini said|Claude said|Assistant said|ChatGPT said|Grok said|DeepSeek said):?\s*/ig, '')
    .replace(/^\s*PLAN:\s*\d+\s*steps?\s*\n(?:\s*\d+[.)]\s+.+\n?)+/gim, '')
    .replace(/^\s*STEP\s*\d+\s*:\s*.+$/gim, '')
    .replace(/^\s*DONE\s*\d+\s*:\s*.+$/gim, '')
    .trim();

  for (let i = 0; i < 3; i++) {
    try {
      const parsed: unknown = JSON.parse(cleaned);
      if (typeof parsed === 'string') {
        cleaned = parsed.trim();
        continue;
      }
      if (parsed && typeof parsed === 'object' && typeof (parsed as { text?: unknown }).text === 'string') {
        cleaned = String((parsed as { text: string }).text).trim();
        continue;
      }
    } catch {
      break;
    }
    break;
  }

  if (/^\s*[\[{]/.test(cleaned) && /"(?:text|tool_calls)"\s*:/.test(cleaned)) {
    return '';
  }
  return cleaned;
}

export function parseAnswerLine(raw: string): string | null {
  const line = raw.startsWith('[err]') ? raw.slice(5).trimStart() : raw;
  const stripped = line.replace(/^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}[.,]?\d*\s+/, '');
  const markerIdx = stripped.indexOf(LogTags.ANSWER);
  if (markerIdx === -1) return null;

  const payload = stripped.slice(markerIdx + LogTags.ANSWER.length).trim();
  if (!payload) return '';

  try {
    const parsed: unknown = JSON.parse(payload);
    if (typeof parsed === 'string') return cleanAssistantAnswerText(parsed);
    if (parsed && typeof parsed === 'object' && typeof (parsed as { text?: unknown }).text === 'string') {
      return cleanAssistantAnswerText(String((parsed as { text: string }).text));
    }
  } catch {
    // Plain text fallback for older bridge output.
  }

  return cleanAssistantAnswerText(payload);
}

export function parseMaybeNestedJson(raw: string): Record<string, unknown> | null {
  try {
    let parsed = JSON.parse(raw);
    if (typeof parsed?.output === 'string') {
      try { parsed = JSON.parse(parsed.output); } catch { /* keep outer */ }
    }
    return (parsed && typeof parsed === 'object') ? parsed : null;
  } catch { return null; }
}

export function extractTouchedFiles(messages: KimMessage[]): TouchedFile[] {
  const fileMap = new Map<string, TouchedFile>();
  const touch = (path: string, added: number, removed: number) => {
    const existing = fileMap.get(path);
    if (existing) { existing.added += added; existing.removed += removed; }
    else fileMap.set(path, { path, added, removed });
  };

  for (const msg of messages) {
    if (!Array.isArray(msg.content)) continue;
    for (const block of msg.content) {
      if (block.type === 'tool_use') {
        const tb = block as ToolUseBlock;
        if (['write_file', 'edit_file', 'create_file'].includes(tb.name)) {
          const p = String(tb.input?.path ?? tb.input?.file_path ?? '');
          if (p) touch(p, 0, 0);
        }
      }
      if (block.type === 'tool_result') {
        const trb = block as ToolResultBlock;
        const raw = typeof trb.content === 'string' ? trb.content
          : trb.output ? String(trb.output) : '';
        if (!raw.trim()) continue;
        const parsed = parseMaybeNestedJson(raw);
        if (!parsed) continue;
        const filePath = String(parsed.filePath ?? (parsed.file as Record<string, unknown>)?.filePath ?? '');
        const patch = parsed.structuredPatch;
        if (filePath && Array.isArray(patch)) {
          let a = 0, r = 0;
          for (const hunk of patch) {
            if (Array.isArray((hunk as Record<string, unknown>)?.lines)) {
              for (const l of (hunk as { lines: string[] }).lines) {
                if (typeof l === 'string') { if (l.startsWith('+')) a++; else if (l.startsWith('-')) r++; }
              }
            }
          }
          touch(filePath, a, r);
        } else if (filePath) {
          const content = parsed.content;
          const lines = typeof content === 'string' ? Math.max(1, content.split(/\r?\n/).length) : 0;
          touch(filePath, lines, 0);
        }
      }
    }
  }
  return Array.from(fileMap.values());
}

// ── Message classification ────────────────────────────────────────────────────

/** A real user message has text content that isn't just tool results. */
export function isRealUserMessage(msg: KimMessage): boolean {
  if (msg.role !== 'user') return false;
  if (typeof msg.content === 'string') {
    return !msg.content.trim().startsWith('[Tool result:');
  }
  if (Array.isArray(msg.content)) {
    if (msg.content.every(b => b.type === 'tool_result')) return false;
    return msg.content.some(
      b => b.type === 'text' && !(b as TextBlock).text.trim().startsWith('[Tool result:')
    );
  }
  return false;
}

/** An assistant message with text but no tool_use blocks = potential final answer. */
export function isTextOnlyAssistant(msg: KimMessage): boolean {
  if (msg.role !== 'assistant') return false;
  if (typeof msg.content === 'string') return true;
  if (Array.isArray(msg.content)) {
    return msg.content.some(b => b.type === 'text') &&
           !msg.content.some(b => b.type === 'tool_use');
  }
  return false;
}

/**
 * Returns true if this is an intermediate Ollama tool-call message — an
 * assistant message whose entire content is a JSON `{"type":"tool_call",...}`
 * string. These are invisible in the chat; they appear in the WorkedForPill.
 */
export function isIntermediateToolCall(msg: KimMessage): boolean {
  if (msg.role !== 'assistant' || typeof msg.content !== 'string') return false;
  const raw = msg.content.trim();
  if (!raw.startsWith('{')) return false;
  try {
    const p = JSON.parse(raw) as Record<string, unknown>;
    return p.type === 'tool_call' || p.type === 'tool_use';
  } catch {
    return false;
  }
}

export function codexBridgeFiller(text: string): boolean {
  return /^Calling\s+[A-Za-z_][\w-]*\.$/.test(text.trim());
}

// ── Log suppression ───────────────────────────────────────────────────────────

/** Substring patterns that silently drop a line (case-insensitive match). */
export const HIDDEN_SUBSTRINGS = [
  // screenshot / internal commands
  'take_screenshot', 'screenshot', 'capture_screen',
  // model reasoning preambles
  'Thought for ',
  // raw text-JSON tool calls emitted by models that can't use native tool_calls
  '"tool":', '"args":',
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
  // Codex bridge internal output
  'Codex completed', 'Codex failed', 'LLM calls,', 'bridge_request', 'bridge_response',
  'relay #', 'sending to browser LLM', 'browser LLM',
  'CODEX_PROXY', 'codex binary:', 'codex-config',
  // Provider internal noise
  'sending to gemini', 'sending to claude', 'sending to chatgpt',
  'getattr(logger, level.lower(), logger.info)(message)',
  // InteractionPolicy diagnostics (legacy multi-line format that leaked internal state)
  'Last observe_ui generation', 'Last web_observe generation',
  'known UI IDs:', 'known web IDs:', 'last UI observe dirty:', 'last observe dirty:',
  'Suggested next action:', 'POLICY_WARNING', 'POLICY_BLOCK',
  // External tool stdin chatter (codex/claw inheriting a TTY)
  'stdin',
  // Backend routing/startup diagnostics that shouldn't render as thoughts
  'Routing to Codex',
  // Internal run lifecycle status — not a user-facing reasoning step
  'run ended:',
  // Native UI tool-result noise
  'No interactive controls', "title='", 'title="',
];

/** Regex patterns that silently drop a line. */
export const HIDDEN_REGEX: RegExp[] = [
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

export function isNoiseLine(raw: string): boolean {
  if (raw.startsWith(LogTags.STATUS) || raw.includes(LogTags.STATUS) || raw.startsWith(LogTags.ANSWER) || raw.includes(LogTags.ANSWER)) return false;
  if (raw.includes(LogTags.SUCCESS) || raw.includes(LogTags.FAILED) || raw.includes(LogTags.ERROR) || raw.startsWith(LogTags.TOOL)) return false;
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

// ── Tool map and related helpers ──────────────────────────────────────────────

export function basename(p: string): string {
  if (!p) return '';
  return p.split(/[/\\]/).pop() ?? p;
}

export function shorten(s: string, max: number): string {
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
  const cleaned = raw
    .replace(/\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2},?\d*\s*/g, '')
    .replace(/\[(ERROR|WARN|INFO|DEBUG|TOOL|CRITICAL)\]\s*/g, '')
    .replace(/orchestrator\.\w+:\s*/g, '')
    .trim();
  // Audit item 3 (B3/B4/C20): surface the REAL error. The old gate deleted any
  // message over 200 chars or containing a path/".py"/traceback fragment and
  // replaced it with "Something went wrong. Check your settings and try
  // again." — which destroyed NEED_HELP questions and real crash text
  // (ModuleNotFoundError etc.) while pointing the user at settings that were
  // fine. We now keep the cleaned text (length-capped) and only fall back to
  // the generic message when nothing usable remains.
  if (!cleaned) return 'Something went wrong. Check your settings and try again.';
  return cleaned.length > 600 ? cleaned.slice(0, 600) + '…' : cleaned;
}

/** Friendly names + icons for known tool calls */
export const TOOL_MAP: Record<string, { icon: string; label: (args: Record<string, unknown>) => string }> = {
  read_file:          { icon: '›', label: a => `Reading \`${basename(String(a.path ?? a.file_path ?? ''))}\`` },
  write_file:         { icon: '›', label: a => {
    const lines = Number(a.lines ?? 0);
    return `Writing \`${basename(String(a.path ?? a.file_path ?? ''))}\`${lines > 0 ? ` +${lines}` : ''}`;
  } },
  create_file:        { icon: '›', label: a => `Creating \`${basename(String(a.path ?? ''))}\`` },
  edit_file:          { icon: '›', label: a => `Editing \`${basename(String(a.path ?? a.file_path ?? ''))}\`` },
  append_file:        { icon: '›', label: a => `Appending to \`${basename(String(a.path ?? ''))}\`` },
  delete_file:        { icon: '›', label: a => `Deleting \`${basename(String(a.path ?? ''))}\`` },
  list_dir:           { icon: '›', label: a => `Listing \`${basename(String(a.path ?? a.directory ?? ''))}\`` },
  list_directory:     { icon: '›', label: a => `Listing \`${basename(String(a.path ?? a.directory ?? ''))}\`` },
  find_files:         { icon: '›', label: a => `Searching for \`${String(a.pattern ?? a.query ?? '')}\`` },
  search_files:       { icon: '›', label: a => `Searching for \`${String(a.pattern ?? a.query ?? '')}\`` },
  search_in_files:    { icon: '›', label: a => `Searching code for \`${String(a.pattern ?? '')}\`` },
  grep:               { icon: '›', label: a => `Searching code for \`${String(a.pattern ?? '')}\`` },
  run_command:        { icon: '›', label: a => `Running \`${shorten(String(a.command ?? a.cmd ?? ''), 60)}\`` },
  run_terminal:       { icon: '›', label: a => `Running \`${shorten(String(a.command ?? ''), 60)}\`` },
  type_text:          { icon: '›', label: _a => 'Typing text' },
  click:              { icon: '›', label: _a => 'Clicking' },
  double_click:       { icon: '›', label: _a => 'Double-clicking' },
  right_click:        { icon: '›', label: _a => 'Right-clicking' },
  scroll:             { icon: '›', label: _a => 'Scrolling' },
  move_mouse:         { icon: '›', label: _a => 'Moving mouse' },
  press_key:          { icon: '›', label: a => `Pressing \`${String(a.key ?? '')}\`` },
  hotkey:             { icon: '›', label: a => `Pressing \`${String(a.keys ?? a.key ?? '')}\`` },
  drag:               { icon: '›', label: _a => 'Dragging' },
  open_application:   { icon: '›', label: a => `Opening ${String(a.app_name ?? a.application ?? '')}` },
  close_application:  { icon: '›', label: a => `Closing ${String(a.app_name ?? '')}` },
  read_clipboard:     { icon: '›', label: _a => 'Reading clipboard' },
  write_clipboard:    { icon: '›', label: _a => 'Writing to clipboard' },
  take_screenshot:    { icon: '›', label: _a => 'Capturing screen' },
  take_annotated_screenshot: { icon: '›', label: _a => 'Capturing annotated screen' },
  observe_ui:         { icon: '›', label: a => `Reading UI${a.window_title ? ` · ${String(a.window_title)}` : ''}` },
  click_ui:           { icon: '›', label: a => `Clicking \`${String(a.element_id ?? 'element')}\`` },
  focus_window:       { icon: '›', label: a => `Focusing ${String(a.window_title ?? a.app ?? 'window')}` },
  web_open:           { icon: '›', label: a => `Opening ${String(a.url ?? 'page')}` },
  get_screen_text:    { icon: '›', label: _a => 'Reading screen text' },
  web_search:         { icon: '›', label: a => `Searching the web for "${String(a.query ?? '')}"` },
  open_url:           { icon: '›', label: a => `Opening ${String(a.url ?? 'a web page')}` },
  git_status:         { icon: '›', label: _a => 'Checking git status' },
  git_commit:         { icon: '›', label: a => `Git commit: "${shorten(String(a.message ?? ''), 50)}"` },
  git_diff:           { icon: '›', label: _a => 'Viewing git diff' },
  ask_user:           { icon: '›', label: a => `Asking: "${String(a.question ?? '')}"` },
  bash:               { icon: '›', label: a => {
    const cmd = String(a.command ?? a.cmd ?? '');
    return cmd.trim().startsWith('open ')
      ? `Opening \`${basename(cmd.trim().slice(5))}\``
      : `Running \`${shorten(cmd, 60)}\``;
  } },
  grep_search:        { icon: '›', label: a => `Searching for \`${String(a.pattern ?? a.query ?? '')}\`` },
  glob_search:        { icon: '›', label: a => `Searching for \`${String(a.pattern ?? a.glob ?? '')}\`` },
  list_files:         { icon: '›', label: a => `Listing \`${basename(String(a.path ?? a.directory ?? ''))}\`` },
  get_windows:        { icon: '›', label: _a => 'Listing open windows' },
  get_screen_info:    { icon: '›', label: _a => 'Reading screen info' },
};

export function parseLogLine(raw: string, id: number): ActivityItem | null {
  if (!raw.trim()) return null;

  if (raw.includes(LogTags.SUCCESS)) {
    let text = raw.replace(/.*\[SUCCESS\]\s*/, '').trim();
    if (/^Codex (?:completed|finished)/i.test(text) || /\bLLM calls\b/i.test(text)) {
      text = 'Task completed';
    }
    return { id, kind: 'success', icon: '✓', text: text || 'Task completed successfully' };
  }
  if (raw.includes(LogTags.FAILED) || (raw.includes(LogTags.ERROR) && !raw.startsWith('[err]'))) {
    const msg = raw.replace(/.*\[(FAILED|ERROR)\]\s*/, '').trim();
    return { id, kind: 'error', icon: '⚠', text: friendlyError(msg) };
  }

  const isErr = raw.startsWith('[err]');
  const line = isErr ? raw.slice(5).trim() : raw;
  const stripped = line.replace(/^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}[.,]?\d*\s+/, '');

  // M9: match the protocol directives BEFORE the noise filter — a NEED_HELP /
  // TASK_COMPLETE reason containing a hidden substring (e.g. "screenshot",
  // "stdin") used to be swallowed, ending the run with a generic error banner
  // instead of the agent's actual question.
  const taskCompleteMatch = stripped.match(/(?:^|\b)TASK_COMPLETE:\s*(.+)$/i);
  if (taskCompleteMatch) {
    const summary = taskCompleteMatch[1].trim();
    return { id, kind: 'success', icon: '✓', text: summary || 'Task completed' };
  }

  const needHelpMatch = stripped.match(/(?:^|\b)NEED_HELP:\s*(.+)$/i);
  if (needHelpMatch) {
    const reason = needHelpMatch[1].trim();
    return { id, kind: 'error', icon: '⚠', text: friendlyError(reason || 'Kim needs your help to continue.') };
  }

  // B4: a Python crash's FINAL line ("ModuleNotFoundError: No module named
  // 'mcp'") is the one clue the user needs, but the noise filter hides every
  // "XxxError:" line (it was added to suppress traceback bodies). Surface the
  // terminal exception line BEFORE the noise filter — excluding the
  // ExceptionGroup wrappers, whose message is boilerplate, not the cause.
  const crashMatch = stripped.match(/^([A-Za-z_][\w.]*(?:Error|Exception))\s*:\s*(.+)$/);
  if (crashMatch && !/(?:Base)?ExceptionGroup$/.test(crashMatch[1])) {
    return { id, kind: 'error', icon: '⚠', text: `${crashMatch[1]}: ${crashMatch[2].trim()}` };
  }

  if (isNoiseLine(raw)) return null;
  if (raw.startsWith('[truncated')) return null;

  if (raw.startsWith('⏹')) {
    return { id, kind: 'cancelled', icon: '⏹', text: 'Task stopped' };
  }

  if (stripped.startsWith(LogTags.STATUS) || raw.startsWith(LogTags.STATUS)) {
    const text = (stripped.startsWith(LogTags.STATUS) ? stripped : raw)
      .replace(/^\[STATUS\]\s*/, '').trim();
    if (text) return { id, kind: 'status', icon: '›', text };
    return null;
  }
  const embeddedStatusIdx = stripped.indexOf(LogTags.STATUS);
  if (isErr && embeddedStatusIdx !== -1) {
    const text = stripped.slice(embeddedStatusIdx + LogTags.STATUS.length).trim();
    if (text) return { id, kind: 'status', icon: '›', text };
  }

  // Use balanced-paren extraction so args longer than 200 chars are not truncated.
  const toolPrefixMatch = stripped.match(/\[TOOL\]\s+(?:[\w.]+:\s+)?(\w+)\(/);
  if (toolPrefixMatch) {
    const toolName = toolPrefixMatch[1];
    const openIdx = (toolPrefixMatch.index ?? 0) + toolPrefixMatch[0].length - 1;
    let depth = 0;
    let closeIdx = -1;
    let inString = false;
    let escape = false;
    for (let ci = openIdx; ci < stripped.length; ci++) {
      const ch = stripped[ci];
      if (escape) { escape = false; continue; }
      if (ch === '\\' && inString) { escape = true; continue; }
      if (ch === '"') { inString = !inString; continue; }
      if (!inString) {
        if (ch === '(') depth++;
        else if (ch === ')') { depth--; if (depth === 0) { closeIdx = ci; break; } }
      }
    }
    const argsRaw = closeIdx > openIdx ? stripped.slice(openIdx + 1, closeIdx) : '{}';
    let args: Record<string, unknown> = {};
    try { args = JSON.parse(argsRaw); } catch {
      const m = argsRaw.match(/"(\w+)":\s*"([^"]+)"/);
      if (m) args = { [m[1]]: m[2] };
    }
    const def = TOOL_MAP[toolName];
    if (def) return { id, kind: 'tool', icon: def.icon, text: def.label(args) };
    return { id, kind: 'tool', icon: '›', text: `Using tool: \`${toolName}\`` };
  }

  if (stripped.match(/\[(ERROR|CRITICAL)\]/)) {
    const msg = stripped.replace(/\[(ERROR|CRITICAL)\]\s+[\w.]*:\s*/, '').trim();
    return { id, kind: 'error', icon: '⚠', text: friendlyError(msg) };
  }

  if (raw.includes(LogTags.FAILED) || raw.includes(LogTags.ERROR)) {
    const msg = raw.replace(/.*\[(FAILED|ERROR)\]\s*/, '').trim();
    return { id, kind: 'error', icon: '⚠', text: friendlyError(msg) };
  }

  if (isErr) {
    if (stripped.match(/\[(INFO|DEBUG)\]/)) return null;
    const msg = stripped.replace(/\[[\w]+\]\s+[\w.]*:\s*/, '').trim();
    if (!msg || msg.length > 80) return null;
    if (/[/\\].+\.py/.test(msg)) return null;
    if (/^\s*at\s/.test(msg)) return null;
    if (/^(?:error|fatal):/i.test(msg) || /\b(?:codex|claw|kim) (?:encountered|ran into) an error\b/i.test(msg)) {
      return { id, kind: 'error', icon: '⚠', text: friendlyError(msg) };
    }
    return { id, kind: 'status', icon: '·', text: msg };
  }

  return null;
}

// ── Activity synthesis ────────────────────────────────────────────────────────

export function synthesizeActivityFromMessages(messages: KimMessage[], toolMap: typeof TOOL_MAP): ActivityItem[] {
  const items: ActivityItem[] = [];
  let id = 0;
  for (const msg of messages) {
    if (typeof msg.content === 'string') {
      if (msg.role === 'assistant') {
        const rawT = msg.content.trim();
        if (rawT.startsWith('{')) {
          try {
            const parsed = JSON.parse(rawT) as Record<string, unknown>;
            if (parsed.type === 'tool_call' && typeof parsed.tool === 'string') {
              const toolName = parsed.tool;
              const def = toolMap[toolName];
              const args = parsed.args && typeof parsed.args === 'object' ? parsed.args as Record<string, unknown> : {};
              items.push({ id: ++id, kind: 'tool', icon: def?.icon ?? '›', text: def ? def.label(args) : `Using \`${toolName}\`` });
              const thinking = typeof parsed.content === 'string' ? parsed.content.trim() : '';
              if (thinking) {
                const t = cleanActivityText(thinking);
                if (t) items.push({ id: ++id, kind: 'status', icon: '›', text: t.length > 120 ? t.slice(0, 120) + '…' : t });
              }
            } else if (typeof parsed.text === 'string' && parsed.text.trim()) {
              const t = cleanActivityText(parsed.text);
              if (t) items.push({ id: ++id, kind: 'status', icon: '›', text: t.length > 120 ? t.slice(0, 120) + '…' : t });
            }
          } catch {
            const cleaned = rawT.replace(/^TASK_COMPLETE:\s*/i, '');
            if (cleaned) {
              const t = cleanActivityText(cleaned);
              if (t) items.push({ id: ++id, kind: 'status', icon: '›', text: t.length > 120 ? t.slice(0, 120) + '…' : t });
            }
          }
          continue;
        }
        const cleaned = rawT.replace(/^TASK_COMPLETE:\s*/i, '');
        if (cleaned) {
          const t = cleanActivityText(cleaned);
          if (t) items.push({ id: ++id, kind: 'status', icon: '›', text: t.length > 120 ? t.slice(0, 120) + '…' : t });
        }
      }
      continue;
    }
    if (!Array.isArray(msg.content)) continue;
    for (const block of msg.content) {
      if (block.type === 'text') {
        const rawT = (block as TextBlock).text.trim();
        if (!rawT || rawT.startsWith('[Tool result:') || codexBridgeFiller(rawT)) continue;
        if (msg.role === 'assistant') {
          const t = cleanActivityText(rawT);
          items.push({ id: ++id, kind: 'status', icon: '›', text: t.length > 120 ? t.slice(0, 120) + '…' : t });
        }
      } else if (block.type === 'tool_use') {
        const tb = block as ToolUseBlock;
        const def = toolMap[tb.name];
        const args = (tb.input && typeof tb.input === 'object') ? tb.input as Record<string, unknown> : {};
        items.push({ id: ++id, kind: 'tool', icon: def?.icon ?? '›', text: def ? def.label(args) : `Using tool: \`${tb.name}\`` });
      } else if (block.type === 'tool_result') {
        const trb = block as ToolResultBlock;
        const raw = typeof trb.content === 'string' ? trb.content
          : String(trb.output ?? '');
        if (!raw.trim()) continue;
        const parsed = parseMaybeNestedJson(raw);
        if (parsed?.filePath) {
          const fp = basename(String(parsed.filePath));
          const patch = parsed.structuredPatch;
          if (Array.isArray(patch)) {
            let a = 0, r = 0;
            for (const h of patch) { if (Array.isArray((h as Record<string, unknown>)?.lines)) { for (const l of (h as { lines: string[] }).lines) { if (typeof l === 'string') { if (l.startsWith('+')) a++; else if (l.startsWith('-')) r++; } } } }
            items.push({ id: ++id, kind: 'tool', icon: '›', text: `Updated \`${fp}\` +${a} -${r}` });
          } else {
            items.push({ id: ++id, kind: 'tool', icon: '›', text: `Updated \`${fp}\`` });
          }
        }
      }
    }
  }
  return items;
}

/**
 * Synthesize activity items for the exchange that starts at the (userIdx+1)-th
 * real user message. Scans messages in that window and collects tool calls /
 * thoughts, excluding the last assistant message (the final answer).
 */
export function synthesizeExchangeActivity(
  allMsgs: KimMessage[],
  userIdx: number,
): ActivityItem[] {
  let seen = -1;
  let start = -1;
  for (let i = 0; i < allMsgs.length; i++) {
    if (isRealUserMessage(allMsgs[i])) {
      seen++;
      if (seen === userIdx) { start = i; break; }
    }
  }
  if (start === -1) return [];

  const slice: KimMessage[] = [];
  for (let i = start; i < allMsgs.length; i++) {
    if (i > start && isRealUserMessage(allMsgs[i])) break;
    slice.push(allMsgs[i]);
  }

  // Exclude only a final *text* answer — a trailing tool-call message
  // (block-array tool_use or string-JSON tool_call) is still activity.
  const lastAsstIdx = slice.reduce(
    (acc, m, i) => isTextOnlyAssistant(m) && !isIntermediateToolCall(m) ? i : acc,
    -1,
  );
  const actSlice = lastAsstIdx > 0 ? slice.slice(0, lastAsstIdx) : slice;
  return synthesizeActivityFromMessages(actSlice, TOOL_MAP);
}

function finalizeCodexRun(userMessage: KimMessage, intermediates: KimMessage[]): CodexRunGroup {
  let finalIdx = -1;
  for (let i = intermediates.length - 1; i >= 0; i--) {
    if (isTextOnlyAssistant(intermediates[i]) && !isIntermediateToolCall(intermediates[i])) { finalIdx = i; break; }
  }
  const finalAssistantMessage = finalIdx >= 0 ? intermediates[finalIdx] : null;
  const activityMessages = finalIdx >= 0
    ? [...intermediates.slice(0, finalIdx), ...intermediates.slice(finalIdx + 1)]
    : intermediates;
  return {
    userMessage,
    intermediateMessages: activityMessages,
    intermediateActivity: synthesizeActivityFromMessages(activityMessages, TOOL_MAP),
    finalAssistantMessage,
    touchedFiles: extractTouchedFiles(activityMessages),
    durationSec: 0,
  };
}

export function groupCodexMessages(messages: KimMessage[]): CodexRunGroup[] {
  const runs: CodexRunGroup[] = [];
  let currentUser: KimMessage | null = null;
  let currentIntermediate: KimMessage[] = [];
  for (const msg of messages) {
    if (isRealUserMessage(msg)) {
      if (currentUser) runs.push(finalizeCodexRun(currentUser, currentIntermediate));
      currentUser = msg;
      currentIntermediate = [];
    } else if (currentUser) {
      currentIntermediate.push(msg);
    }
  }
  if (currentUser) runs.push(finalizeCodexRun(currentUser, currentIntermediate));
  return runs;
}

// ── Plan extraction ───────────────────────────────────────────────────────────

export function parsePlanFromActivity(items: ActivityItem[]): LivePlanParsed | null {
  let structuredSteps: string[] | null = null;
  let structuredActive = 0;
  const structuredDone = new Set<number>();
  const orphanStructuredSteps = new Map<number, string>();
  for (const it of items) {
    const t = it.text;
    const planTag = t.indexOf(LogTags.PLAN + '{');
    if (planTag !== -1) {
      try {
        const json = t.slice(planTag + LogTags.PLAN.length);
        const parsed = JSON.parse(json) as { steps?: unknown };
        if (Array.isArray(parsed.steps)) {
          const arr = parsed.steps.filter(s => typeof s === 'string') as string[];
          if (arr.length >= 2) {
            structuredSteps = arr.slice(0, 12);
            structuredActive = 0;
            structuredDone.clear();
            orphanStructuredSteps.clear();
          }
        }
      } catch {}
      continue;
    }
    const stepTag = t.indexOf(LogTags.STEP + '{');
    if (stepTag !== -1) {
      try {
        const json = t.slice(stepTag + LogTags.STEP.length);
        const parsed = JSON.parse(json) as { index?: number; name?: unknown };
        if (typeof parsed.index === 'number' && parsed.index > 0) {
          if (structuredSteps) {
            structuredActive = Math.min(parsed.index, structuredSteps.length);
          } else if (typeof parsed.name === 'string' && parsed.name.trim()) {
            orphanStructuredSteps.set(parsed.index, parsed.name.trim().slice(0, 120));
            structuredActive = parsed.index;
          }
        }
      } catch {}
      continue;
    }
    const doneTag = t.indexOf(LogTags.DONE + '{');
    if (doneTag !== -1) {
      try {
        const json = t.slice(doneTag + LogTags.DONE.length);
        const parsed = JSON.parse(json) as { index?: number };
        if (typeof parsed.index === 'number' && parsed.index > 0) {
          structuredDone.add(parsed.index);
        }
      } catch {}
    }
  }
  if (structuredSteps) {
    return { steps: structuredSteps, activeStep: structuredActive, doneSteps: [...structuredDone], structured: true };
  }
  if (orphanStructuredSteps.size >= 2) {
    const indexes = [...orphanStructuredSteps.keys()].sort((a, b) => a - b).slice(0, 12);
    const steps = indexes.map(idx => orphanStructuredSteps.get(idx) || `Step ${idx}`);
    const activeMapped = indexes.includes(structuredActive) ? indexes.indexOf(structuredActive) + 1 : Math.min(structuredActive, steps.length);
    return {
      steps,
      activeStep: activeMapped,
      doneSteps: [...structuredDone].filter(idx => indexes.includes(idx)).map(idx => indexes.indexOf(idx) + 1),
      structured: true,
    };
  }

  // Heuristic fallback
  const steps: string[] = [];
  let collecting = false;
  for (const it of items) {
    const t = it.text;
    if (!collecting) {
      if (/here'?s my plan before|my plan before I start|plan before I start editing/i.test(t)) {
        collecting = true;
      }
      continue;
    }
    if (it.kind === 'tool') break;
    const trimmed = t.trim();
    if (/^Kim is thinking/i.test(trimmed)) continue;
    const bullet = trimmed.match(/^(?:[-•*]|\[[ xX]\])\s*(.+)$/);
    if (bullet) { steps.push(bullet[1].trim()); continue; }
    const num = trimmed.match(/^\d+\.\s*(.+)$/);
    if (num) { steps.push(num[1].trim()); continue; }
    if (it.kind === 'status' && trimmed.length >= 12 && trimmed.length < 260) {
      steps.push(trimmed);
    }
    if (steps.length >= 14) break;
  }
  if (steps.length >= 2) {
    return { steps: steps.slice(0, 12), activeStep: 0, doneSteps: [], structured: false };
  }

  const numbered: string[] = [];
  for (const it of items) {
    if (it.kind !== 'status') {
      if (numbered.length > 0) break;
      continue;
    }
    const m = it.text.match(/^\s*\d+\.\s*(.+)$/);
    if (m) numbered.push(m[1].trim());
    else if (numbered.length > 0) break;
  }
  return numbered.length >= 2
    ? { steps: numbered.slice(0, 12), activeStep: 0, doneSteps: [], structured: false }
    : null;
}

// ── Message collapsing ────────────────────────────────────────────────────────

export function collapseMessages(msgs: KimMessage[]) {
  // B2: carry `srcIdx` — the index of each kept message in the ORIGINAL array —
  // so callers can map a collapsed-array position back to the real message
  // (editing used the collapsed index against the uncollapsed array → wrong msg).
  const res: {msg: KimMessage, retries: number, srcIdx: number}[] = [];
  for (let i = 0; i < msgs.length; i++) {
    const msg = msgs[i];
    if (res.length > 0 && msg.role === 'assistant' && typeof msg.content === 'string') {
      const prev = res[res.length - 1];
      if (prev.msg.role === 'assistant' && typeof prev.msg.content === 'string') {
        const c1 = msg.content.replace(/(?:Gemini said|Claude said|Assistant said|ChatGPT said|Grok said):?\s*/ig, '').trim();
        const c2 = prev.msg.content.replace(/(?:Gemini said|Claude said|Assistant said|ChatGPT said|Grok said):?\s*/ig, '').trim();
        if (c1 === c2 && c1.startsWith('{')) {
          try {
            JSON.parse(c1);
            prev.retries += 1;
            continue;
          } catch {}
        }
      }
    }
    res.push({ msg, retries: 0, srcIdx: i });
  }
  return res;
}

// ── Provider / browser helpers ────────────────────────────────────────────────

export const PROVIDER_LABELS: Record<string, string> = {
  claude: 'Claude',
  openai: 'OpenAI',
  gemini: 'Gemini',
  deepseek: 'DeepSeek',
  ollama: 'Ollama',
  browser: 'Browser',
  'browser:claude': 'Browser Claude',
  'browser:chatgpt': 'Browser ChatGPT',
  'browser:gemini': 'Browser Gemini',
  'browser:grok': 'Browser Grok',
  'browser:deepseek': 'Browser DeepSeek',
  'browser:custom': 'Browser Custom',
};

export function makeConversationId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export function providerLabel(provider: string): string {
  return PROVIDER_LABELS[provider] ?? provider;
}

export const BROWSER_PROVIDER_URLS: Record<string, string> = {
  claude: 'https://claude.ai/new',
  chatgpt: 'https://chatgpt.com',
  gemini: 'https://gemini.google.com/app',
  grok: 'https://grok.com',
  deepseek: 'https://chat.deepseek.com',
};

export function normalizeBrowserSite(site?: string | null): string | null {
  const s = String(site ?? '').trim().toLowerCase();
  if (s === 'claude' || s === 'claude.ai' || s.includes('claude.ai')) return 'claude';
  if (s === 'chatgpt' || s === 'openai' || s === 'gpt' || s.includes('chatgpt.com') || s.includes('openai.com')) return 'chatgpt';
  if (s === 'gemini' || s === 'google' || s.includes('gemini.google.com')) return 'gemini';
  if (s === 'deepseek' || s.includes('deepseek.com')) return 'deepseek';
  if (s === 'grok' || s.includes('grok.com')) return 'grok';
  if (s === 'custom') return 'custom';
  return null;
}

export function browserSiteFromProvider(provider?: string | null): string | null {
  const p = String(provider ?? '').trim().toLowerCase();
  if (p.startsWith('browser:')) return normalizeBrowserSite(p.split(':')[1]);
  if (p === 'browser') return null;
  return null;
}

export function browserProviderFromSession(session?: SessionInfo | null): string | null {
  return browserSiteFromProvider(session?.last_llm_provider)
    ?? normalizeBrowserSite(session?.browser_last_site)
    ?? normalizeBrowserSite(Object.keys(session?.browser_threads ?? {})[0]);
}

export function getGreeting(name: string): string {
  const hour = new Date().getHours();
  if (hour < 5) return `Late night, ${name}`;
  if (hour < 12) return `Good morning, ${name}`;
  if (hour < 17) return `Good afternoon, ${name}`;
  if (hour < 21) return `Good evening, ${name}`;
  return `Evening, ${name}`;
}

export function formatNsDuration(ns?: number): string | null {
  if (!ns || ns <= 0) return null;
  const ms = ns / 1_000_000;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds >= 10 ? 0 : 1)}s`;
  const minutes = Math.floor(seconds / 60);
  const rem = Math.round(seconds % 60);
  return `${minutes}m ${rem}s`;
}

export function projectLabel(path?: string): string {
  if (!path) return 'Unknown project';
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

// ── Cost estimation ───────────────────────────────────────────────────────────

// USD cost per 1M tokens { input, output }. Zero for free/local providers.
// Rates are approximate; see provider docs for exact pricing.
// Last refreshed: 2025-Q2. Update when provider pricing changes.
const PRICE_PER_1M: Record<string, { input: number; output: number }> = {
  claude:   { input: 3.00,  output: 15.00  }, // claude-sonnet-4.x (Anthropic)
  openai:   { input: 2.50,  output: 10.00  }, // gpt-4o (OpenAI)
  gemini:   { input: 1.25,  output: 5.00   }, // gemini-1.5-pro (Google)
  deepseek: { input: 0.27,  output: 1.10   }, // deepseek-chat V3 (cache-miss)
  ollama:   { input: 0,     output: 0      }, // local
  browser:  { input: 0,     output: 0      }, // local browser session
};

/**
 * Returns the estimated USD cost for the given token counts, or `null` when the
 * provider is not in the known price table.  Returning null (instead of falling
 * back to an arbitrary rate) prevents fabricated cost figures appearing in the UI
 * for providers we have no pricing data for.
 */
export function estimateCostUsd(provider: string, inputTokens: number, outputTokens: number): number | null {
  // B5: browser providers stream as `browser:claude` / `browser:chatgpt` etc.
  // Those are free local sessions — normalize to the `browser` (zero-cost) key.
  const normalized = provider.trim().toLowerCase().startsWith('browser')
    ? 'browser'
    : provider.trim().toLowerCase();
  const rates = PRICE_PER_1M[normalized];
  if (!rates) return null; // Unknown provider — don't fabricate a cost
  return (inputTokens * rates.input + outputTokens * rates.output) / 1_000_000;
}

export function formatCostUsd(usd: number): string {
  if (usd === 0) return '$0.00';
  if (usd < 0.0001) return '<$0.0001';
  // B14: the old `usd < 0.01` branch was identical to the default (toFixed(4)).
  return `$${usd.toFixed(4)}`;
}

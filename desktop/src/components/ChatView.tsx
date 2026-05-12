import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import type { SessionInfo, KimMessage, Settings, KimAccount, TextBlock, ToolUseBlock, ToolResultBlock, BrowserRestoreResult } from '../types';
import { MessageBubble } from './MessageBubble';
import { SignalCard } from './ToolCallCard';
import { Bloop, type BloopState } from './Bloop';
import { toast } from './Toast';

const MAX_ACTIVITY_ITEMS = 300;

// ── Activity feed ─────────────────────────────────────────────────────────────

interface ActivityItem {
  id: number;
  kind: 'tool' | 'info' | 'error' | 'success' | 'cancelled' | 'status';
  icon: string;
  text: string;
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return s === 0 ? `${m}m` : `${m}m ${s}s`;
}

interface LivePlanParsed {
  steps: string[];
}

/**
 * Extract a checklist-style plan from streamed status lines (matches the
 * collapsible “Plan · N of M done” mockups).
 */
function parsePlanFromActivity(items: ActivityItem[]): LivePlanParsed | null {
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
    if (bullet) {
      steps.push(bullet[1].trim());
      continue;
    }
    const num = trimmed.match(/^\d+\.\s*(.+)$/);
    if (num) {
      steps.push(num[1].trim());
      continue;
    }

    if (it.kind === 'status' && trimmed.length >= 12 && trimmed.length < 260) {
      steps.push(trimmed);
    }

    if (steps.length >= 14) break;
  }

  if (steps.length >= 2) {
    return { steps: steps.slice(0, 12) };
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
  return numbered.length >= 2 ? { steps: numbered.slice(0, 12) } : null;
}

function livePlanProgressLabel(stepCount: number, items: ActivityItem[], running: boolean): string | undefined {
  if (stepCount < 2) return undefined;
  const nTools = items.filter(i => i.kind === 'tool').length;
  const done = Math.min(nTools, stepCount);
  let s = `${done} of ${stepCount} done`;
  if (running && done < stepCount) s += ' · 1 in flight';
  return s;
}


export function collapseMessages(msgs: KimMessage[]) {
  const res: {msg: KimMessage, retries: number}[] = [];
  for (const msg of msgs) {
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
    res.push({ msg, retries: 0 });
  }
  return res;
}

// ── Claw session grouping ─────────────────────────────────────────────────────

export interface TouchedFile {
  path: string;
  added: number;
  removed: number;
}

export interface ClawRunGroup {
  userMessage: KimMessage;
  intermediateMessages: KimMessage[];
  intermediateActivity: ActivityItem[];
  finalAssistantMessage: KimMessage | null;
  touchedFiles: TouchedFile[];
  durationSec: number;
}

/** A real user message has text content that isn't just tool results. */
function isRealUserMessage(msg: KimMessage): boolean {
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
function isTextOnlyAssistant(msg: KimMessage): boolean {
  if (msg.role !== 'assistant') return false;
  if (typeof msg.content === 'string') return true;
  if (Array.isArray(msg.content)) {
    return msg.content.some(b => b.type === 'text') &&
           !msg.content.some(b => b.type === 'tool_use');
  }
  return false;
}

function clawBridgeFiller(text: string): boolean {
  return /^Calling\s+[A-Za-z_][\w-]*\.$/.test(text.trim());
}

function parseMaybeNestedJson(raw: string): Record<string, unknown> | null {
  try {
    let parsed = JSON.parse(raw);
    if (typeof parsed?.output === 'string') {
      try { parsed = JSON.parse(parsed.output); } catch { /* keep outer */ }
    }
    return (parsed && typeof parsed === 'object') ? parsed : null;
  } catch { return null; }
}

function extractTouchedFiles(messages: KimMessage[]): TouchedFile[] {
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
          : (trb as unknown as { output?: string }).output
            ? String((trb as unknown as { output: string }).output) : '';
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

function cleanActivityText(t: string): string {
  let cleaned = t
    .replace(/<think>[\s\S]*?<\/think>/gi, '')
    .replace(/(?:Gemini said|Claude said|Assistant said|ChatGPT said|Grok said|DeepSeek said):?\s*/ig, '')
    .trim();
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

function cleanAssistantAnswerText(t: string): string {
  let cleaned = t
    .replace(/<think>[\s\S]*?<\/think>/gi, '')
    .replace(/\s*\[END_OF_RESPONSE\]\s*$/i, '')
    .replace(/(?:Gemini said|Claude said|Assistant said|ChatGPT said|Grok said|DeepSeek said):?\s*/ig, '')
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

function parseAnswerLine(raw: string): string | null {
  const line = raw.startsWith('[err]') ? raw.slice(5).trimStart() : raw;
  const stripped = line.replace(/^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}[.,]?\d*\s+/, '');
  const markerIdx = stripped.indexOf('[ANSWER]');
  if (markerIdx === -1) return null;

  const payload = stripped.slice(markerIdx + '[ANSWER]'.length).trim();
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

function synthesizeActivityFromMessages(messages: KimMessage[], toolMap: typeof TOOL_MAP): ActivityItem[] {
  const items: ActivityItem[] = [];
  let id = 0;
  for (const msg of messages) {
    if (typeof msg.content === 'string') {
      if (msg.role === 'assistant') {
        const rawT = msg.content.trim().replace(/^TASK_COMPLETE:\s*/i, '');
        if (rawT) {
          const t = cleanActivityText(rawT);
          items.push({ id: ++id, kind: 'status', icon: '›', text: t.length > 120 ? t.slice(0, 120) + '…' : t });
        }
      }
      continue;
    }
    if (!Array.isArray(msg.content)) continue;
    for (const block of msg.content) {
      if (block.type === 'text') {
        const rawT = (block as TextBlock).text.trim();
        if (!rawT || rawT.startsWith('[Tool result:') || clawBridgeFiller(rawT)) continue;
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
        const raw = typeof (block as ToolResultBlock).content === 'string' ? (block as ToolResultBlock).content as string
          : String((block as unknown as { output?: string }).output ?? '');
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

function finalizeClawRun(userMessage: KimMessage, intermediates: KimMessage[]): ClawRunGroup {
  let finalIdx = -1;
  for (let i = intermediates.length - 1; i >= 0; i--) {
    if (isTextOnlyAssistant(intermediates[i])) { finalIdx = i; break; }
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

export function groupClawMessages(messages: KimMessage[]): ClawRunGroup[] {
  const runs: ClawRunGroup[] = [];
  let currentUser: KimMessage | null = null;
  let currentIntermediate: KimMessage[] = [];
  for (const msg of messages) {
    if (isRealUserMessage(msg)) {
      if (currentUser) runs.push(finalizeClawRun(currentUser, currentIntermediate));
      currentUser = msg;
      currentIntermediate = [];
    } else if (currentUser) {
      currentIntermediate.push(msg);
    }
  }
  if (currentUser) runs.push(finalizeClawRun(currentUser, currentIntermediate));
  return runs;
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
  // Claw bridge internal output
  'Claw completed', 'Claw failed', 'LLM calls,', 'bridge_request', 'bridge_response',
  'relay #', 'sending to browser LLM', 'browser LLM',
  'CLAW_FILE_BRIDGE', 'CLAW_BRIDGE_DIR', 'claw binary:',
  // Provider internal noise
  'sending to gemini', 'sending to claude', 'sending to chatgpt',
  'Routing to Claw', 'Routing Claw',
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
  // Never suppress status lines — from stdout ("[STATUS] …") or embedded in
  // a stderr log record ("[err] … [STATUS] …"). These are user-facing signals.
  if (raw.startsWith('[STATUS]') || raw.includes('[STATUS]') || raw.startsWith('[ANSWER]') || raw.includes('[ANSWER]')) return false;
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
  write_file:         { icon: '›', label: a => {
    const lines = Number(a.lines ?? 0);
    return `Writing \`${basename(String(a.path ?? a.file_path ?? ''))}\`${lines > 0 ? ` +${lines}` : ''}`;
  } },
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
  // Claw tool names
  bash:               { icon: '›', label: a => {
    const cmd = String(a.command ?? a.cmd ?? '');
    return cmd.trim().startsWith('open ')
      ? `Opening \`${basename(cmd.trim().slice(5))}\``
      : `Running \`${shorten(cmd, 60)}\``;
  } },
  grep_search:        { icon: '›', label: a => `Searching for \`${String(a.pattern ?? a.query ?? '')}\`` },
  glob_search:        { icon: '›', label: a => `Searching for \`${String(a.pattern ?? a.glob ?? '')}\`` },
  list_files:         { icon: '›', label: a => `Listing \`${basename(String(a.path ?? a.directory ?? ''))}\`` },
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
    let text = raw.replace(/.*\[SUCCESS\]\s*/, '').trim();
    // Strip Claw-internal completion messages — replace with a clean summary.
    if (/^Claw (?:completed|finished)/i.test(text) || /\bLLM calls\b/i.test(text)) {
      text = 'Task completed';
    }
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

  // [STATUS] lines — emitted by agent/browser_provider for live feedback.
  // May arrive on stdout (raw starts with [STATUS]) or embedded inside a
  // stderr log line like "[INFO] orchestrator.providers.browser_provider: [STATUS] …"
  if (stripped.startsWith('[STATUS]') || raw.startsWith('[STATUS]')) {
    const text = (stripped.startsWith('[STATUS]') ? stripped : raw)
      .replace(/^\[STATUS\]\s*/, '').trim();
    if (text) return { id, kind: 'status', icon: '›', text };
    return null;
  }
  const embeddedStatusIdx = stripped.indexOf('[STATUS]');
  if (isErr && embeddedStatusIdx !== -1) {
    const text = stripped.slice(embeddedStatusIdx + '[STATUS]'.length).trim();
    if (text) return { id, kind: 'status', icon: '›', text };
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

const CONNECTORS = [
  {
    id: 'guc-cms',
    name: 'GUC CMS',
    description: 'Course content, announcements, enrollment, and CMS actions.',
    icon: 'CMS',
  },
  {
    id: 'guc-mail',
    name: 'GUC Mail',
    description: 'GUC inbox, sent mail, search, drafts, and email actions.',
    icon: '@',
  },
];

function makeConversationId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function providerLabel(provider: string): string {
  return PROVIDER_LABELS[provider] ?? provider;
}

const BROWSER_PROVIDER_URLS: Record<string, string> = {
  claude: 'https://claude.ai/new',
  chatgpt: 'https://chatgpt.com',
  gemini: 'https://gemini.google.com/app',
  grok: 'https://grok.com',
  deepseek: 'https://chat.deepseek.com',
};

function normalizeBrowserSite(site?: string | null): string | null {
  const s = String(site ?? '').trim().toLowerCase();
  if (s === 'claude' || s === 'claude.ai') return 'claude';
  if (s === 'chatgpt' || s === 'openai' || s === 'gpt') return 'chatgpt';
  if (s === 'gemini' || s === 'google') return 'gemini';
  if (s === 'deepseek') return 'deepseek';
  if (s === 'grok') return 'grok';
  return null;
}

function browserSiteFromProvider(provider?: string | null): string | null {
  const p = String(provider ?? '').trim().toLowerCase();
  if (p.startsWith('browser:')) return normalizeBrowserSite(p.split(':')[1]);
  if (p === 'browser') return null;
  return null;
}

function browserProviderFromSession(session?: SessionInfo | null): string | null {
  return normalizeBrowserSite(session?.browser_last_site)
    ?? normalizeBrowserSite(Object.keys(session?.browser_threads ?? {})[0]);
}

function getGreeting(name: string): string {
  const hour = new Date().getHours();
  if (hour < 5) return `Late night, ${name}`;
  if (hour < 12) return `Good morning, ${name}`;
  if (hour < 17) return `Good afternoon, ${name}`;
  if (hour < 21) return `Good evening, ${name}`;
  return `Evening, ${name}`;
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
  onTaskDone: (sessionId?: string, completedSession?: SessionInfo) => void;
  account: KimAccount;
  onAccountChange: (account: KimAccount) => Promise<void>;
  activeTab: 'chat' | 'code';
  activeProjectPath?: string | null;
}

// ── Component ─────────────────────────────────────────────────────────────────

export function ChatView({ session, newChatMode, settings, onTaskDone, account, activeTab, activeProjectPath }: Props) {
  const activityCounterRef = useRef(0);
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

  // stdout/stderr can arrive in bursts. Keep the authoritative activity feed in
  // a ref and commit it to React state at most every ~50ms, so the sidebar and
  // the rest of the app do not re-render once per streamed log line.
  const activityRef = useRef<ActivityItem[]>([]);
  const activityFlushTimerRef = useRef<number | null>(null);

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

  useEffect(() => {
    activityRef.current = activity;
  }, [activity]);

  useEffect(() => {
    return () => {
      if (activityFlushTimerRef.current !== null) {
        window.clearTimeout(activityFlushTimerRef.current);
      }
    };
  }, []);

  // Snapshots of completed runs, in order. Each successful task completion pushes
  // one entry; rendered as a collapsible "Worked for Xm Ys" badge before the
  // corresponding assistant turn in liveHistory.
  const [runHistory, setRunHistory] = useState<{ activity: ActivityItem[]; durationSec: number }[]>([]);
  const [expandedRunIdx, setExpandedRunIdx] = useState<number | null>(null);
  const [clawRuns, setClawRuns] = useState<ClawRunGroup[]>([]);
  const [taskError, setTaskError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [tokenStats, setTokenStats] = useState<{ input: number; output: number; total: number } | null>(null);
  const [queuedTasks, setQueuedTasks] = useState<PendingTask[]>([]);
  const [interruptTask, setInterruptTask] = useState<PendingTask | null>(null);
  const [lastFailedTask, setLastFailedTask] = useState<PendingTask | null>(null);
  const [autoFollowOutput, setAutoFollowOutput] = useState(true);
  const [connectorsOpen, setConnectorsOpen] = useState(false);
  const [connectorsClosing, setConnectorsClosing] = useState(false);
  const [connectorSearch, setConnectorSearch] = useState('');
  const [livePlanExpanded, setLivePlanExpanded] = useState(true);
  // Which browser AI provider is selected (only relevant when settings.provider === 'browser')
  const [browserProvider, setBrowserProvider] = useState('claude');
  const [conversationId] = useState(() => makeConversationId());
  const activeResumeSessionId = session?.session_id ?? conversationId;
  const activeResumeSessionIdRef = useRef(activeResumeSessionId);
  useEffect(() => { activeResumeSessionIdRef.current = activeResumeSessionId; }, [activeResumeSessionId]);

  // Live conversation history for new-chat mode — persists across task runs
  // and doesn't get wiped by the disk-based message reload.
  const [liveHistory, setLiveHistory] = useState<{role: 'user' | 'assistant'; content: string}[]>([]);

  const bottomRef = useRef<HTMLDivElement>(null);
  const outputRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const connectorsCloseTimerRef = useRef<number | null>(null);
  const startTimeRef = useRef<number | null>(null);
  const currentTaskRef = useRef<PendingTask | null>(null);
  // Tracks the most recently submitted task — never cleared, so retry always works
  // even when the task "succeeded" with a NEED_HELP (e.g., 409 sign-in required).
  const lastRunTaskRef = useRef<PendingTask | null>(null);
  const previousProviderRef = useRef(settings.provider);
  const restoreSeqRef = useRef(0);
  const lastRestoreKeyRef = useRef<string | null>(null);
  const isRunningRef = useRef(isRunning);
  useEffect(() => { isRunningRef.current = isRunning; }, [isRunning]);
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
  const completedCodeSessionRef = useRef<SessionInfo | null>(null);
  // Once the user sends their first message in a new-chat session, this stays
  // true for the lifetime of that session — even after the task completes and
  // isRunning/activity reset. This prevents the welcome section from
  // re-appearing (which also caused the visible scroll reset).
  const hasSentMessageRef = useRef(false);

  // ── Deduplication (per-session, not module-global) ───────────────────────
  // Python writes many lines to both stdout AND stderr, causing duplicates.
  // Normalize stderr prefixes before deduping so `[err] [STATUS] x` and
  // `[STATUS] x` do not render twice.
  const recentRawRef = useRef<Map<string, number>>(new Map());
  const recentActivityItemRef = useRef<Map<string, number>>(new Map());
  const answerReceivedThisRunRef = useRef(false);
  const isDuplicate = (raw: string): boolean => {
    const map = recentRawRef.current;
    const now = Date.now();
    const canonical = raw.startsWith('[err]') ? raw.slice(5).trimStart() : raw;
    const last = map.get(canonical);
    if (last !== undefined && now - last < 800) return true;
    map.set(canonical, now);
    // Prune stale entries to prevent unbounded memory growth
    if (map.size > 200) {
      const cutoff = now - 1600;
      for (const [k, v] of map) if (v < cutoff) map.delete(k);
    }
    return false;
  };
  const isDuplicateActivityItem = (item: ActivityItem): boolean => {
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
  };

  // Keep stable refs for long-lived event listeners.
  const onTaskDoneRef = useRef(onTaskDone);
  useEffect(() => { onTaskDoneRef.current = onTaskDone; }, [onTaskDone]);

  const sessionRef = useRef<SessionInfo | null>(session);
  useEffect(() => { sessionRef.current = session; }, [session]);

  const settingsRef = useRef(settings);
  useEffect(() => { settingsRef.current = settings; }, [settings]);

  const activeTabRef = useRef(activeTab);
  useEffect(() => { activeTabRef.current = activeTab; }, [activeTab]);

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

  // Defensive: whenever isRunning flips to false from any source (done,
  // cancelled, error), also tear down the floating cancel widget. The Rust
  // side already does this on `kim-agent-done`, but if that event is missed
  // (e.g. a previous run left isRunning stuck), this guard prevents the
  // overlay from getting orphaned. (issue #3 §5)
  useEffect(() => {
    if (!isRunning) {
      invoke('set_task_active_mode', { active: false }).catch(() => {});
    }
  }, [isRunning]);

  const filteredConnectors = CONNECTORS.filter(connector => {
    const q = connectorSearch.trim().toLowerCase();
    if (!q) return true;
    return (
      connector.name.toLowerCase().includes(q) ||
      connector.description.toLowerCase().includes(q)
    );
  });

  useEffect(() => {
    return () => {
      if (connectorsCloseTimerRef.current !== null) {
        window.clearTimeout(connectorsCloseTimerRef.current);
      }
    };
  }, []);

  const openConnectors = useCallback(() => {
    if (connectorsCloseTimerRef.current !== null) {
      window.clearTimeout(connectorsCloseTimerRef.current);
      connectorsCloseTimerRef.current = null;
    }
    setConnectorsClosing(false);
    setConnectorsOpen(true);
  }, []);

  const closeConnectors = useCallback(() => {
    if (!connectorsOpen || connectorsClosing) return;
    setConnectorsClosing(true);
    connectorsCloseTimerRef.current = window.setTimeout(() => {
      setConnectorsOpen(false);
      setConnectorsClosing(false);
      connectorsCloseTimerRef.current = null;
    }, 260);
  }, [connectorsClosing, connectorsOpen]);

  // Topbar's connectors button dispatches `kim-open-connectors`. Listen here
  // so the trigger lives in the topbar but the panel still renders from this
  // component. The panel itself is portaled to <body> so it overlays the
  // topbar and sidebar (instead of being clipped to .kim-chat).
  useEffect(() => {
    function onOpen() { openConnectors(); }
    window.addEventListener('kim-open-connectors', onOpen);
    return () => window.removeEventListener('kim-open-connectors', onOpen);
  }, [openConnectors]);

  function renderConnectorsChrome() {
    if (!connectorsOpen) return null;
    return createPortal(
      <div
        className={`kim-connectors-layer${connectorsClosing ? ' is-closing' : ''}`}
        aria-hidden={connectorsClosing}
      >
        <button
          type="button"
          className="kim-connectors-scrim"
          aria-label="Close connectors"
          onClick={closeConnectors}
        />
        <aside className="kim-connectors-panel" aria-label="Connectors">
              <div className="kim-connectors-panel__header">
                <div>
                  <div className="kim-connectors-panel__title">Connectors</div>
                  <div className="kim-connectors-panel__subtitle">Turn on tool packs for Kim</div>
                </div>
                <button
                  type="button"
                  className="kim-connectors-panel__close"
                  aria-label="Close connectors"
                  onClick={closeConnectors}
                >
                  ×
                </button>
              </div>

              <label className="kim-connectors-search">
                <span className="sr-only">Search connectors</span>
                <input
                  value={connectorSearch}
                  onChange={e => setConnectorSearch(e.target.value)}
                  placeholder="Search for connector..."
                  autoFocus
                />
              </label>

              <div className="kim-connectors-list">
                {filteredConnectors.map(connector => (
                  <div className="kim-connector-row" key={connector.id}>
                    <div className="kim-connector-row__icon" aria-hidden="true">
                      {connector.icon}
                    </div>
                    <div className="kim-connector-row__body">
                      <div className="kim-connector-row__name">{connector.name}</div>
                      <div className="kim-connector-row__desc">{connector.description}</div>
                    </div>
                    <div className="kim-connector-row__action">
                      <button type="button" className="kim-connector-connect" disabled>
                        <span>Connect</span>
                        <span className="kim-connector-connect__divider" />
                        <span className="kim-connector-connect__chevron" aria-hidden="true">⌄</span>
                      </button>
                      <div className="kim-connector-item__actions">
                        <span>Coming soon</span>
                      </div>
                    </div>
                  </div>
                ))}

                {filteredConnectors.length === 0 && (
                  <div className="kim-connectors-empty">No connectors found</div>
                )}
              </div>
        </aside>
      </div>,
      document.body,
    );
  }

  // ── Load messages ───────────────────────────────────────────────────────────
  // Two trigger paths:
  //   1. Session change (user picked a different chat)  → full reload + spinner.
  //   2. messageReloadNonce bump (task done in same chat) → SILENT refresh:
  //      no spinner, don't wipe liveHistory until new disk messages arrive,
  //      don't re-animate the latest bubble (liveHistory already animated it).
  const lastLoadedSessionIdRef = useRef<string | null>(null);
  useEffect(() => {
    if (!session) {
      setMessages([]);
      lastLoadedSessionIdRef.current = null;
      return;
    }
    const prevId = lastLoadedSessionIdRef.current;
    const isSessionChange = prevId !== session.session_id;
    // "Self transition": we were in newChatMode (no prior session) and the
    // newly-selected session is the conversation we just ran (its id matches
    // our in-flight conversationId). Treat this like a silent refresh — keep
    // liveHistory and skip the spinner so the chat doesn't appear to vanish
    // and then reappear (issue #3 §4).
    //
    // "Claw continuation": In Code tab, each follow-up task creates a new
    // Claw session (Claw doesn't support --resume). When the task completes,
    // handleTaskDone navigates to the new session, causing a session change.
    // Without this guard the UI would wipe liveHistory and show a spinner,
    // making it look like a "chat reset". We detect this by checking that
    // hasSentMessageRef is true (we were actively chatting, not clicking a
    // sidebar item) and the session is a claw session.
    const isClawContinuation =
      isSessionChange &&
      prevId !== null &&
      session.session_type === 'claw' &&
      hasSentMessageRef.current;
    const isSelfTransition =
      isSessionChange &&
      prevId === null &&
      session.session_id === activeResumeSessionIdRef.current;
    const isSeamlessTransition = isSelfTransition || isClawContinuation;
    lastLoadedSessionIdRef.current = session.session_id;

    if (isSessionChange && !isSeamlessTransition) {
      setLoadingMessages(true);
      setLiveHistory([]);
      // Reset run history and re-hydrate from the sidecar `<id>.runs.json` file.
      setRunHistory([]);
      setExpandedRunIdx(null);
      setClawRuns([]); 
      invoke<{ activity: ActivityItem[]; durationSec: number }[]>('load_run_history', {
        sessionId: session.session_id,
        sessionDate: session.date || null,
        kimDir: settings.kim_sessions_dir || null,
        clawDir: session.session_type === 'claw' && session.project_path
          ? `${session.project_path}/.claw/sessions`
          : settings.claw_sessions_dir || null,
      })
        .then(runs => {
          if (Array.isArray(runs)) {
            setRunHistory(runs);
            // Merge saved durations into clawRuns so the "Worked for X"
            // badge shows the correct time on old Claw sessions.
            if (runs.length > 0) {
              setClawRuns(prev => prev.map((run, i) =>
                i < runs.length && runs[i].durationSec > 0
                  ? { ...run, durationSec: runs[i].durationSec }
                  : run
              ));
            }
          }
        })
        .catch(() => { /* non-fatal */ });
    }
    invoke<KimMessage[]>('load_session_messages', {
      sessionId: session.session_id,
      sessionDate: session.date || null,
      kimDir: settings.kim_sessions_dir || null,
      clawDir: session.session_type === 'claw' && session.project_path
        ? `${session.project_path}/.claw/sessions`
        : settings.claw_sessions_dir || null,
    })
      .then(msgs => {
        const prev = prevMsgCountRef.current;
        const lastAssistantIdx = msgs.reduceRight((found, m, i) =>
          found === -1 && m.role === 'assistant' ? i : found, -1);
        // Animate only on session change. On silent refresh the bubble was
        // already shown (and animated) via liveHistory — re-animating it
        // looks like the chat is "refreshing".
        if (isSessionChange && !isSeamlessTransition && prev > 0 && msgs.length > prev && lastAssistantIdx >= prev) {
          setNewestMsgIdx(lastAssistantIdx);
        } else {
          setNewestMsgIdx(null);
        }
        prevMsgCountRef.current = msgs.length;
        setMessages(msgs);
        // Group Claw session messages into per-run structures so intermediate
        // tool calls are hidden behind "Worked on this" disclosures.
        if (session.session_type === 'claw') {
          setClawRuns(groupClawMessages(msgs));
        } else {
          setClawRuns([]);
        }
        // Silent refresh AND self-transition: clear liveHistory only after
        // disk messages are loaded so the chat doesn't blink empty.
        // BUT: when staying on an old Claw session after a task completes
        // (the new turn lives in a different session file), the disk
        // messages for this session haven't changed. Clearing liveHistory
        // would wipe the new response. Only clear when the disk data
        // actually absorbed the new content (msg count grew).
        if (!isSessionChange || isSeamlessTransition) {
          if (msgs.length > prev) {
            setLiveHistory([]);
          }
        }
      })
      .catch(err => console.error('Failed to load messages:', err))
      .finally(() => {
        if (isSessionChange && !isSeamlessTransition) setLoadingMessages(false);
      });
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
      clearActivityNow();
      setRunHistory([]);
      setExpandedRunIdx(null);
      setClawRuns([]);
      setTaskError(null);
      setTokenStats(null);
      setElapsed(0);
      hasSentMessageRef.current = false;
      // Note: do NOT set isRunning=false here — if a task is actually still
      // running we should show that state. But if not running, we want clean UI.
    }
  }, [newChatMode, clearActivityNow]);

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
    const id = ++activityCounterRef.current;

    // Handle [STATS] token lines — update token counter, don't add to feed
    const statsMatch = line.match(/\[STATS\]\s+input_tokens=(\d+)\s+output_tokens=(\d+)\s+total_tokens=(\d+)/);
    if (statsMatch) {
      setTokenStats({ input: parseInt(statsMatch[1]), output: parseInt(statsMatch[2]), total: parseInt(statsMatch[3]) });
      return;
    }

    // [ANSWER] is a final assistant message from the Claw browser bridge. It
    // should render as a chat bubble, never as an activity feed/status item.
    const answerText = parseAnswerLine(line);
    if (answerText !== null) {
      if (answerText) {
        answerReceivedThisRunRef.current = true;
        setLiveHistory(prev => {
          const last = prev[prev.length - 1];
          if (last?.role === 'assistant' && last.content.trim() === answerText.trim()) return prev;
          return [...prev, { role: 'assistant', content: answerText }];
        });
      }
      return;
    }

    // Surface Claw's structured JSON error output as a real error banner
    // instead of silently dropping the line and showing only "Agent Error".
    // Claw emits e.g. {"error":"missing Anthropic credentials …","type":"error"}
    // on stdout right before exiting non-zero.
    const stdoutLine = line.startsWith('[err]') ? line.slice(5).trimStart() : line;
    if (stdoutLine.startsWith('{') && stdoutLine.includes('"error"')) {
      try {
        const parsed = JSON.parse(stdoutLine) as { error?: string; type?: string; message?: string };
        const msg = (parsed.error ?? parsed.message ?? '').trim();
        if (msg && (parsed.type === 'error' || /credential|api[_ ]?key|unauthorized/i.test(msg))) {
          setTaskError(friendlyError(msg));
          if (lastRunTaskRef.current) setLastFailedTask(lastRunTaskRef.current);
          needHelpFlagRef.current = true; // reuse the flag so kim-agent-done suppresses the generic banner
          enqueueActivityUpdate(prev => {
            const next = [...prev, { id, kind: 'error' as const, icon: '⚠', text: friendlyError(msg) }];
            if (next.length > MAX_ACTIVITY_ITEMS) return next.slice(next.length - MAX_ACTIVITY_ITEMS);
            return next;
          });
          return;
        }
      } catch {
        /* not JSON, fall through */
      }
    }

    // Handle [DIFF] lines — annotate the previous file-write activity item
    const diffMatch = line.match(/\[DIFF\]\s+path=(\S+)\s+\+(\d+)\s+-(\d+)/);
    if (diffMatch) {
      const [, _path, added, removed] = diffMatch;
      enqueueActivityUpdate(prev => {
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
        // Only now hide the main window and show the cancel widget — the agent
        // is actually going to look at the screen. Simple tasks ("hi", etc.)
        // never emit this so they run with the window visible the whole time.
        invoke('set_task_active_mode', { active: true }).catch(() => {});
      }
      return;
    }
    if (item.kind === 'status' && /\b(?:gemini|claude|chatgpt|grok|deepseek)\s+(?:is\s+)?(?:still\s+)?thinking/i.test(item.text)) {
      item.text = item.text.replace(/\b(?:gemini|claude|chatgpt|grok|deepseek)\s+(?:is\s+)?(?:still\s+)?thinking(?:…|\.\.\.)?(?:\s+\(\d+s\))?/ig, 'Kim is thinking…');
    }
    // Replace any remaining provider brand mentions in status lines
    if (item.kind === 'status') {
      item.text = item.text
        .replace(/\b(?:Gemini|Claude|ChatGPT|Grok|DeepSeek)\s+(?:is|says?|returned?|respond(?:s|ed)?)/gi, 'Kim')
        .replace(/\bsending to (?:Gemini|Claude|ChatGPT|Grok|DeepSeek)\b/gi, 'Kim is working')
        .replace(/\b(?:Gemini|Claude|ChatGPT|Grok|DeepSeek) responded?\b/gi, 'Response received');
      // Strip JSON fragment leaks: lines that start with { or contain "text":
      if (/^\s*[{"\[]/.test(item.text) || /"text"\s*:/.test(item.text)) {
        // Try to extract readable text from JSON
        try {
          const parsed = JSON.parse(item.text);
          if (typeof parsed === 'object' && parsed !== null && typeof parsed.text === 'string') {
            item.text = parsed.text;
          }
        } catch {
          // Not valid JSON — drop lines that look like raw JSON artifacts
          if (/^\s*\{.*\}\s*$/.test(item.text)) return;
        }
      }
    }

    if (isDuplicateActivityItem(item)) {
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

    enqueueActivityUpdate(prev => {
      if (item.kind === 'success') return prev; // Skip adding to activity feed to avoid duplicating the assistant bubble
      // Collapse consecutive status items: replace the previous one so repeated
      // "gemini is thinking…" lines don't flood the feed.
      if (item.kind === 'status' && prev.length > 0 && prev[prev.length - 1].kind === 'status') {
        return [...prev.slice(0, -1), item];
      }
      const next = [...prev, item];
      if (next.length > MAX_ACTIVITY_ITEMS) return next.slice(next.length - MAX_ACTIVITY_ITEMS);
      return next;
    });

    // Capture success results as assistant bubbles in liveHistory.
    // This must also happen in Claw (Code-tab) mode: when the user sends a
    // follow-up from an old Claw session, we stay on that session to
    // preserve history. The new task's response appears via liveHistory.
    if (item.kind === 'success') {
      const genericSuccess = /^Task completed(?: successfully)?$/i.test(item.text.trim());
      if (!answerReceivedThisRunRef.current && !genericSuccess) {
        setLiveHistory(prev => [...prev, { role: 'assistant', content: item.text }]);
      }
    }
  }

  // ── Agent event listeners ───────────────────────────────────────────────────
  useEffect(() => {
    let unlistenOutput: (() => void) | undefined;
    let unlistenError: (() => void) | undefined;
    let unlistenDone: (() => void) | undefined;
    let unlistenCodeSession: (() => void) | undefined;
    let unlistenCancelled: (() => void) | undefined;

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
      cancelFlagRef.current = false; // reset for next task
      needHelpFlagRef.current = false; // reset
      setIsRunning(false);
      setCancelling(false);
      // Snapshot the activity into run history before clearing so the
      // "Worked for X" disclosure can replay it. Persist to disk so the
      // disclosure survives a chat reload.
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
              sessionDate: completedCodeSession?.date ?? session?.date ?? null,
              kimDir: settings.kim_sessions_dir || null,
              clawDir: completedCodeSession?.project_path
                ? `${completedCodeSession.project_path}/.claw/sessions`
                : settings.claw_sessions_dir || null,
              runs: next,
            }).catch(() => { /* non-fatal */ });
          }
          return next;
        });
      }
      clearActivityNow();
      // Existing session view is now interactive, so reload message history
      // after every run completion to reflect newly appended turns.
      setMessageReloadNonce(v => v + 1);
      // Persist the provider browser's current thread URL before refreshing
      // sessions. Bad/login/home URLs are filtered on the Rust side so this is
      // safe to call after every browser-backed run.
      const completedCodeSession = completedCodeSessionRef.current ?? undefined;
      const completedSessionId = completedCodeSession?.session_id ?? activeResumeSessionIdRef.current;
      const runProviderSite = browserSiteFromProvider(currentTaskRef.current?.provider);
      void commitCurrentBrowserUrl(runProviderSite, completedCodeSession ?? sessionRef.current, completedSessionId);

      // Always refresh sessions — failed runs still create session files.
      // Pass the session ID so App.tsx can auto-navigate to the completed session.
      onTaskDoneRef.current(completedSessionId, completedCodeSession);
      completedCodeSessionRef.current = null;
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

    listen<SessionInfo>('kim-agent-code-session', event => {
      completedCodeSessionRef.current = event.payload;
    }).then(fn => { unlistenCodeSession = fn; });

    listen<boolean>('kim-agent-cancelled', () => {
      invoke('set_task_active_mode', { active: false }).catch(() => {});
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
      unlistenCodeSession?.();
      unlistenCancelled?.();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Actions ─────────────────────────────────────────────────────────────────

  const queueEnabled = Boolean(settings.allow_message_queue);
  const keepBrowserVisible = Boolean(settings.keep_browser_visible);

  useEffect(() => {
    void invoke('set_browser_keep_visible', { keepVisible: keepBrowserVisible });
  }, [keepBrowserVisible]);

  const resolveProvider = useCallback((): string => {
    const p = localProvider ?? settings.provider;
    // If localProvider is already "browser:claude" etc., pass it through
    if (p.startsWith('browser:')) return p;
    if (p === 'browser') return `browser:${browserProvider}`;
    return p || 'browser';
  }, [localProvider, settings.provider, browserProvider]);

  const browserCommandArgs = useCallback((
    targetSession?: SessionInfo | null,
    overrideSessionId?: string | null,
  ) => {
    const s = targetSession ?? sessionRef.current;
    const st = settingsRef.current;
    const sessionType = s?.session_type ?? (activeTabRef.current === 'code' ? 'claw' : 'kim');
    return {
      sessionId: overrideSessionId ?? s?.session_id ?? activeResumeSessionIdRef.current,
      sessionDate: s?.date ?? null,
      sessionType,
      kimDir: st.kim_sessions_dir || null,
      clawDir: sessionType === 'claw' && s?.project_path
        ? `${s.project_path}/.claw/sessions`
        : st.claw_sessions_dir || null,
    };
  }, []);

  const commitCurrentBrowserUrl = useCallback(async (preferredSite?: string | null, targetSession?: SessionInfo | null, overrideSessionId?: string | null) => {
    const site = normalizeBrowserSite(preferredSite) ?? browserSiteFromProvider(currentTaskRef.current?.provider) ?? browserSiteFromProvider(resolveProvider());
    const args = browserCommandArgs(targetSession, overrideSessionId);
    if (!args.sessionId) return;
    try {
      await invoke('session_browser_url_commit', {
        ...args,
        preferredSite: site,
      });
    } catch {
      // Non-fatal: URL persistence must never break task completion.
    }
  }, [browserCommandArgs, resolveProvider]);

  const restoreBrowserForSession = useCallback(async (targetSession: SessionInfo, preferredSite?: string | null) => {
    const site = normalizeBrowserSite(preferredSite)
      ?? browserProviderFromSession(targetSession)
      ?? browserSiteFromProvider(resolveProvider())
      ?? browserProvider;
    if (!site) return null;
    try {
      return await invoke<BrowserRestoreResult>('restore_browser_for_session', {
        ...browserCommandArgs(targetSession),
        preferredSite: site,
      });
    } catch (err) {
      toast(`Could not restore the provider browser: ${friendlyError(String(err))}`, 'warning', 5000);
      return null;
    }
  }, [browserCommandArgs, resolveProvider, browserProvider]);

  // Restore the provider browser for every concrete session entry path
  // (sidebar select, auto-select after task, tab/session remount). A monotonically
  // increasing sequence number prevents stale restores from showing toasts after
  // the user has already switched to another session/provider.
  useEffect(() => {
    if (!session || newChatMode) return;

    const savedSite = browserProviderFromSession(session);
    const fallbackSite = browserSiteFromProvider(resolveProvider()) ?? browserProvider;
    const site = savedSite ?? fallbackSite;
    if (!site) return;

    if (savedSite) {
      setBrowserProvider(savedSite);
      setLocalProvider(`browser:${savedSite}`);
    }

    const restoreKey = `${session.session_type}:${session.date}:${session.session_id}:${site}`;
    if (lastRestoreKeyRef.current === restoreKey) return;
    lastRestoreKeyRef.current = restoreKey;

    const seq = restoreSeqRef.current + 1;
    restoreSeqRef.current = seq;

    void (async () => {
      const result = await restoreBrowserForSession(session, site);
      if (restoreSeqRef.current !== seq) return;
      if (!result) return;

      if (result.restored) {
        toast(`Restored ${providerLabel('browser:' + result.site)} for this session.`, 'info', 2500);
      } else if (result.reason === 'stored_url_rejected') {
        toast(result.message ?? 'Saved browser URL was invalid, so Kim opened a fresh provider page.', 'warning', 5000);
      }
    })();
  }, [
    session?.session_key,
    session?.session_id,
    session?.date,
    session?.session_type,
    session?.browser_last_site,
    session?.browser_threads_updated_at_ms,
    newChatMode,
    browserProvider,
    resolveProvider,
    restoreBrowserForSession,
  ]);

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
    answerReceivedThisRunRef.current = false;
    recentActivityItemRef.current.clear();
    hasSentMessageRef.current = true; // Hide welcome section permanently for this session
    currentTaskRef.current = pending;
    lastRunTaskRef.current = pending;
    setIsRunning(true);
    clearActivityNow();
    setTaskError(null);
    setTokenStats(null);
    setCancelling(false);
    setAutoFollowOutput(true);

    // Add user message to live history for chat bubble display
    setLiveHistory(prev => [...prev, { role: 'user', content: pending.text }]);

    try {
      // Resolve the session ID dynamically from refs to prevent race conditions
      // where a queued task starts before React has finished prop-drilling
      // the new session state from the previous task's completion.
      const resolvedSessionId = completedCodeSessionRef.current?.session_id ?? activeResumeSessionIdRef.current;
      const pendingBrowserSite = browserSiteFromProvider(pending.provider);
      if (pendingBrowserSite) {
        invoke('session_browser_meta_write', {
          ...browserCommandArgs(sessionRef.current, resolvedSessionId),
          browserLastSite: pendingBrowserSite,
          site: null,
          url: null,
        }).catch(() => {});
      }
      
      await invoke('send_task', {
        task: pending.text,
        provider: pending.provider,
        projectRoot: (activeTab === 'code' && activeProjectPath) ? activeProjectPath : (settings.project_root || null),
        resumeSessionId: resolvedSessionId,
      });
    } catch (err) {
      // kim-agent-done fires BEFORE invoke() rejects on process failure.
      // If the event already handled everything, skip the duplicate rejection.
      if (!doneHandledRef.current) {
        // Always restore the main window — it was hidden before send_task was called.
        invoke('set_task_active_mode', { active: false }).catch(() => {});
        setIsRunning(false);
        setTaskError(friendlyError(String(err)));
        setLastFailedTask(pending);
        
        const resolvedSessionId = completedCodeSessionRef.current?.session_id ?? activeResumeSessionIdRef.current;
        onTaskDoneRef.current(resolvedSessionId); // refresh sidebar even on invoke-level failures
      }
    }
  }, [settings.project_root, activeTab, activeProjectPath, clearActivityNow, browserCommandArgs]);

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

  // Edit a previously-sent live-history user message and resend. The edited
  // message replaces the original (and drops everything after it). If a task
  // is currently running we cancel it first; the new task then runs.
  function handleEditLiveMessage(idx: number, newText: string) {
    const trimmed = newText.trim();
    if (!trimmed) return;
    setLiveHistory(prev => prev.slice(0, idx)); // truncate; runPendingTask re-appends
    const pending = makePendingTask(trimmed);
    setTaskError(null);
    if (isRunning) {
      setQueuedTasks([]);
      setInterruptTask(pending);
      if (!cancelling) void handleCancel();
    } else {
      void runPendingTask(pending);
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
            : msg.content.filter(b => b.type === 'text').map(b => (b as TextBlock).text).join('\n');

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

  function handleTextareaInput(e: React.ChangeEvent<HTMLTextAreaElement>) {
    setTaskInput(e.target.value);
    const el = e.target;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 200) + 'px';
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
    const livePlan = parsePlanFromActivity(activity);
    const toolCalls = activity.filter(a => a.kind === 'tool').length;
    const streamStepCount = Math.max(toolCalls, activity.length);
    const planProgress =
      livePlan ? livePlanProgressLabel(livePlan.steps.length, activity, isRunning) : undefined;

    return (
      <div className="kim-msg-row kim-msg-row--assistant kim-msg-row--live">
        <div className="kim-bubble kim-bubble--assistant kim-bubble--live kim-bubble--live-stream">
          <div className="kim-thinking-header kim-thinking-header--stream">
            <span className="kim-thinking-header__lead">
              <span className="kim-thinking-header__dot" aria-hidden="true" />
              <span className="kim-thinking-header__label">Thinking</span>
            </span>
            <span className="kim-thinking-header__trail">
              {elapsed > 0 && (
                <span className="kim-thinking-header__timer">{formatDuration(elapsed)}</span>
              )}
              {elapsed > 0 && (
                <span className="kim-thinking-header__sep" aria-hidden="true">
                  ·
                </span>
              )}
              <span className="kim-thinking-header__steps">
                {streamStepCount} {streamStepCount === 1 ? 'step' : 'steps'}
              </span>
            </span>
          </div>
          {livePlan && (
            <div className="kim-plan-live">
              <button
                type="button"
                className={'kim-plan-live__toggle' + (livePlanExpanded ? ' is-expanded' : '')}
                onClick={() => setLivePlanExpanded(e => !e)}
                aria-expanded={livePlanExpanded}
              >
                <svg
                  className="kim-plan-live__chev"
                  viewBox="0 0 16 16"
                  width="12"
                  height="12"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <path d="M5 3l5 5-5 5" />
                </svg>
                <span className="kim-plan-live__title">Plan</span>
                {planProgress && (
                  <span className="kim-plan-live__progress">{planProgress}</span>
                )}
              </button>
              {livePlanExpanded && (
                <ol className="kim-plan-live__list">
                  {livePlan.steps.map((s, i) => (
                    <li key={i} className="kim-plan-live__step">
                      <span className="kim-plan-live__idx">{i + 1}</span>
                      <span className="kim-plan-live__text">{s}</span>
                    </li>
                  ))}
                </ol>
              )}
            </div>
          )}
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

  function renderWorkedFor(idx: number, run: { activity: ActivityItem[]; durationSec: number }) {
    const expanded = expandedRunIdx === idx;
    const durationLabel = run.durationSec > 0
      ? `Worked for ${formatDuration(run.durationSec)}`
      : 'Worked on this';
    return (
      <div className="kim-msg-row kim-msg-row--assistant">
        <div className="kim-worked-for">
          <button
            type="button"
            className={`kim-worked-for__chip${expanded ? ' is-expanded' : ''}`}
            onClick={() => setExpandedRunIdx(expanded ? null : idx)}
            aria-expanded={expanded}
          >
            <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="kim-worked-for__chevron">
              <path d="M5 3l5 5-5 5" />
            </svg>
            <span>{durationLabel}</span>
          </button>
          {expanded && (
            <div className="kim-worked-for__panel">
              <div className="kim-activity-feed kim-activity-feed--archive">
                {run.activity.map(item => (
                  <div key={item.id} className={`kim-activity-item kim-activity-item--${item.kind}`}>
                    <span className="kim-activity-item__icon" aria-hidden="true">{item.icon}</span>
                    <span className="kim-activity-item__text">{renderActivityText(item.text)}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
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

  // ── Composer ─────────────────────────────────────────────────────────────────

  function renderComposer() {
    const resolvedProvider = resolveProvider();
    const browserProviderId = resolvedProvider.startsWith('browser:') ? resolvedProvider.split(':')[1] : '';
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
            {isRunning && !cancelling && (
              <button
                type="button"
                onClick={handleCancel}
                disabled={cancelling}
                title="Stop task"
                className="kim-btn kim-btn--stop"
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
          {!(newChatMode && !hasSentMessageRef.current) && (
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
          )}
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
                const previousSite = browserSiteFromProvider(resolveProvider());

                if (isRunningRef.current) {
                  toast('Finish or cancel the current task before switching providers.', 'warning', 3500);
                  return;
                }

                if (val.startsWith('browser:')) {
                  const sub = normalizeBrowserSite(val.split(':')[1]);
                  if (!sub) return;

                  // Save the old thread before switching away from it. Rust
                  // filters generic home/login pages so this cannot corrupt a
                  // previously useful URL.
                  await commitCurrentBrowserUrl(previousSite);

                  restoreSeqRef.current += 1;
                  lastRestoreKeyRef.current = null;
                  setLocalProvider(`browser:${sub}`);
                  setBrowserProvider(sub);

                  const targetSession = sessionRef.current;
                  if (targetSession) {
                    try {
                      await invoke('session_browser_meta_write', {
                        ...browserCommandArgs(targetSession),
                        browserLastSite: sub,
                        site: null,
                        url: null,
                      });
                    } catch {
                      // Non-fatal.
                    }
                    const seq = restoreSeqRef.current + 1;
                    restoreSeqRef.current = seq;
                    const result = await restoreBrowserForSession(targetSession, sub);
                    if (restoreSeqRef.current === seq && result) {
                      if (result.restored) {
                        toast(`Restored ${providerLabel('browser:' + sub)} for this session.`, 'info', 3000);
                      } else if (result.reason === 'stored_url_rejected') {
                        toast(result.message ?? 'Saved browser URL was invalid, so Kim opened a fresh provider page.', 'warning', 5000);
                      }
                    }
                  } else {
                    const newUrl = BROWSER_PROVIDER_URLS[sub];
                    if (newUrl) {
                      try {
                        const navigated = await invoke<boolean>('navigate_browser_window_if_open', { url: newUrl });
                        if (!navigated) {
                          await invoke<string>('open_browser_signin_window', {
                            url: newUrl,
                            providerName: sub.charAt(0).toUpperCase() + sub.slice(1),
                          });
                        }
                      } catch (err) {
                        toast(`Could not switch browser provider: ${friendlyError(String(err))}`, 'warning', 5000);
                      }
                    }
                  }
                } else {
                  await commitCurrentBrowserUrl(previousSite);
                  restoreSeqRef.current += 1;
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
          {browserProviderId && browserProviderId !== 'custom' && (
            <>
              <span className="kim-composer__hint-sep">·</span>
              <button
                type="button"
                className="kim-composer__signin-btn"
                onClick={async () => {
                  const p = browserProviderId;
                  const providerName = p.charAt(0).toUpperCase() + p.slice(1);
                  let url = '';
                  if (p === 'claude') url = 'https://claude.ai';
                  else if (p === 'chatgpt') url = 'https://chatgpt.com';
                  else if (p === 'gemini') url = 'https://gemini.google.com/app';
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
                Sign into {browserProviderId.charAt(0).toUpperCase() + browserProviderId.slice(1)}
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
        {renderConnectorsChrome()}

        <div className="kim-empty-welcome kim-empty-welcome--launch">
          <div className="kim-greeting__text">
            {activeTab === 'code' ? 'Start a new Code session' : getGreeting(account.display_name.split(' ')[0])}
          </div>
          <div className="kim-empty-welcome__subtitle">
            {activeTab === 'code'
              ? 'Pick a project in the sidebar, or open a saved Code session.'
              : 'Pick up where you left off, or start fresh.'}
          </div>
          <div className="kim-launch-shortcuts" aria-label="Keyboard shortcuts">
            <span><kbd className="kim-kbd">⌘</kbd><kbd className="kim-kbd">N</kbd> new chat</span>
            <span><kbd className="kim-kbd">⌘</kbd><kbd className="kim-kbd">B</kbd> sidebar</span>
            <span><kbd className="kim-kbd">⌘</kbd><kbd className="kim-kbd">,</kbd> settings</span>
          </div>
        </div>
      </div>
    );
  }

  // ── New chat mode ─────────────────────────────────────────────────────────────
  if (newChatMode) {
    return (
      <div className="kim-chat">
        {renderConnectorsChrome()}

        <div className="kim-chat__output" ref={outputRef}>


          {!hasSentMessageRef.current && (
            <div className="kim-new-chat-empty kim-new-chat-empty--launch">
              <div className="kim-new-chat-empty__badge">
                <span className="kim-pulse-dot kim-pulse-dot--accent" />
                Ready
              </div>
              <div className="kim-new-chat-empty__title kim-greeting">
                {activeTab === 'code' ? 'Start a new Code session' : getGreeting(account.display_name)}
              </div>
              <div className="kim-new-chat-empty__subtitle">
                {activeTab === 'code'
                  ? 'Tell Kim what to inspect, fix, or build.'
                  : 'Pick up where you left off, or start fresh.'}
              </div>
              <div className="kim-launch-shortcuts kim-launch-shortcuts--center" aria-label="Keyboard shortcuts">
                <span><kbd className="kim-kbd">⌘</kbd><kbd className="kim-kbd">N</kbd> new chat</span>
                <span><kbd className="kim-kbd">⌘</kbd><kbd className="kim-kbd">B</kbd> sidebar</span>
                <span><kbd className="kim-kbd">⌘</kbd><kbd className="kim-kbd">,</kbd> settings</span>
              </div>
            </div>
          )}

          {/* Live conversation history */}
          {(() => {
            const collapsed = collapseMessages(liveHistory);
            let assistantIdx = -1;
            return collapsed.map(({msg, retries}, i) => {
              if (msg.role === 'assistant') assistantIdx += 1;
              const workedRun = msg.role === 'assistant' ? runHistory[assistantIdx] : null;
              const showActivityAfter = msg.role === 'user' && !liveHistory.slice(i + 1).some(m => m.role === 'assistant');
              return (
                <div key={`live-${i}`}>
                  {workedRun && renderWorkedFor(assistantIdx, workedRun)}
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
            {clawRuns.length > 0 ? (
              /* ── Claw grouped view: per-run user→disclosure→answer→pills ── */
              clawRuns.map((run, runIdx) => (
                <div key={`claw-run-${runIdx}`}>
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
                      animate={runIdx === clawRuns.length - 1}
                      typingAnimation={settings.typing_animation ?? 'none'}
                      onRetry={handleRetryLast}
                    />
                  )}
                  {renderFilePills(run.touchedFiles)}
                </div>
              ))
            ) : (
              /* ── Normal (Kim) message view ── */
              (() => {
                const collapsed = collapseMessages(messages);
                let assistantIdx = -1;
                return collapsed.map(({msg, retries}, i) => {
                  if (msg.role === 'assistant') assistantIdx += 1;
                  const workedRun = msg.role === 'assistant' ? runHistory[assistantIdx] : null;
                  return (
                    <div key={i}>
                      {workedRun && renderWorkedFor(assistantIdx, workedRun)}
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
              let assistantIdx = -1;
              return collapsed.map(({msg, retries}, i) => {
                if (msg.role === 'assistant') assistantIdx += 1;
                const workedRun = msg.role === 'assistant' ? runHistory[assistantIdx] : null;
                const showActivityAfter = msg.role === 'user' && !liveHistory.slice(i + 1).some(m => m.role === 'assistant');
                return (
                  <div key={`live-${i}`}>
                    {workedRun && renderWorkedFor(assistantIdx, workedRun)}
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

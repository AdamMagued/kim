import type { ActivityItem, LivePlanParsed, ProviderUsageState } from './types';
import type { TraceItem, WorkedForTraceItem, WorkedForToolKind } from '../kim-ui';
import { cleanActivityText, parseAnswerLine, friendlyError, parseLogLine } from './utils';

// ── Trace helpers (activity → ThinkingWithPlan format) ─────────────────────

export function parseToolVerb(text: string): { verb: string; target: string } | null {
  // Standard "Verb target" format produced by TOOL_MAP labels
  const verbMatch = text.match(
    /^(Reading|Writing|Running|Editing|Creating|Deleting|Listing|Searching|Opening|Clicking|Typing|Pressing|Checking|Viewing|Updating|Appending|Using|Asking|Dragging|Scrolling|Focusing|Observing|Navigating|Fetching|Committing|Installing|Moving|Copying|Renaming|Double-clicking|Right-clicking|Git)\s*(.*)/i
  );
  if (verbMatch) {
    return { verb: verbMatch[1], target: verbMatch[2].replace(/`/g, '').trim() };
  }
  // "Using tool: `tool_name`" raw fallback
  const usingMatch = text.match(/^Using tool:\s*`?([^`\n]+)`?/i);
  if (usingMatch) {
    const name = usingMatch[1].trim().replace(/_/g, ' ');
    const capitalized = name.charAt(0).toUpperCase() + name.slice(1);
    return { verb: 'Using', target: capitalized };
  }
  return null;
}

export function buildPlanTraceItem(livePlan: LivePlanParsed): TraceItem {
  return {
    kind: 'plan',
    title: 'Plan',
    items: livePlan.steps.map((s, i) => {
      const oneBased = i + 1;
      let status: 'done' | 'active' | 'pending' | 'todo' = 'pending';
      if (livePlan.structured) {
        if (livePlan.doneSteps.includes(oneBased) || oneBased < livePlan.activeStep) {
          status = 'done';
        } else if (oneBased === livePlan.activeStep) {
          status = 'active';
        }
      }
      return { text: s, status };
    }),
  };
}

export function buildThinkingTrace(items: ActivityItem[], livePlan: LivePlanParsed | null): TraceItem[] {
  const trace: TraceItem[] = [];
  let planInserted = false;

  for (const item of items) {
    const t = item.text;
    // Skip raw JSON envelope lines — they drive the plan widget
    if (/^\s*\[(?:PLAN|STEP|DONE)\]\{/.test(t)) {
      if (t.includes('[PLAN]{') && !planInserted && livePlan && livePlan.steps.length >= 2) {
        trace.push(buildPlanTraceItem(livePlan));
        planInserted = true;
      }
      continue;
    }
    if (item.kind === 'tool') {
      const parsed = parseToolVerb(t);
      if (parsed) {
        trace.push({ kind: 'tool', verb: parsed.verb, target: parsed.target });
      } else {
        trace.push({ kind: 'thought', text: t });
      }
    } else if (item.kind === 'status' || item.kind === 'info') {
      trace.push({ kind: 'thought', text: t });
    }
  }

  // If plan never appeared in-stream (unstructured), insert before first tool
  if (!planInserted && livePlan && livePlan.steps.length >= 2) {
    const firstToolIdx = trace.findIndex(t => t.kind === 'tool');
    const insertAt = firstToolIdx > 0 ? firstToolIdx : trace.length;
    trace.splice(insertAt, 0, buildPlanTraceItem(livePlan));
  }

  return trace;
}

export function traceToWorkedFor(trace: TraceItem[]): WorkedForTraceItem[] {
  const result: WorkedForTraceItem[] = [];
  for (const item of trace) {
    if (item.kind === 'plan') continue;
    if (item.kind === 'thought') {
      result.push({ kind: 'think', text: item.text });
    } else {
      const verb = item.verb.toLowerCase();
      let kind: WorkedForToolKind = 'run';
      if (verb === 'reading' || verb === 'checking') kind = 'read';
      else if (verb === 'writing' || verb === 'creating') kind = 'write';
      else if (verb === 'editing' || verb === 'updating' || verb === 'appending' || verb === 'moving' || verb === 'copying' || verb === 'renaming') kind = 'edit';
      else if (verb === 'listing') kind = 'ls';
      else if (verb === 'searching') kind = 'grep';
      else if (verb === 'fetching' || verb === 'navigating' || verb === 'opening') kind = 'fetch';
      else if (verb === 'viewing') kind = 'screenshot';
      result.push({ kind, target: item.target });
    }
  }
  return result;
}

// ── State Parsing Helpers ──────────────────────────────────────────────────

export interface ContextState {
  cumulative_input: number;
  budget: number;
  phase: string;
  percent: number;
  last_input: number;
  last_output: number;
  source: string;
  estimate: boolean;
}

export interface TokenStats {
  input: number;
  output: number;
  total: number;
}

export type ParsedAgentLine =
  | { type: 'stats'; payload: TokenStats }
  | { type: 'context'; payload: ContextState }
  | { type: 'usage'; payload: ProviderUsageState }
  | { type: 'answer'; payload: string }
  | { type: 'codex_agent_message'; payload: string }
  | { type: 'codex_reasoning'; payload: string }
  | { type: 'codex_shell_call'; payload: string }
  | { type: 'codex_ignored' }
  | { type: 'error'; payload: string }
  | { type: 'diff'; payload: { path: string; added: number; removed: number } }
  | { type: 'screenshot_flash' }
  | { type: 'show_window' }
  | { type: 'need_help'; payload: string }
  | { type: 'activity_item'; payload: ActivityItem }
  | { type: 'none' };

export function parseAgentLine(line: string, id: number): ParsedAgentLine {
  // Handle [STATS] token lines — update token counter, don't add to feed
  const statsMatch = line.match(/\[STATS\]\s+input_tokens=(\d+)\s+output_tokens=(\d+)\s+total_tokens=(\d+)/);
  if (statsMatch) {
    return {
      type: 'stats',
      payload: {
        input: parseInt(statsMatch[1]),
        output: parseInt(statsMatch[2]),
        total: parseInt(statsMatch[3]),
      },
    };
  }

  const ctxMatch = line.match(/\[CONTEXT\]\s+cumulative_input=(\d+)\s+budget=(\d+)\s+phase=(\w+)\s+percent=(\d+)\s+last_input=(\d+)\s+last_output=(\d+)\s+source=([a-zA-Z0-9_\-]+)\s+estimate=(\d)/);
  if (ctxMatch) {
    return {
      type: 'context',
      payload: {
        cumulative_input: parseInt(ctxMatch[1]),
        budget: parseInt(ctxMatch[2]),
        phase: ctxMatch[3],
        percent: parseInt(ctxMatch[4]),
        last_input: parseInt(ctxMatch[5]),
        last_output: parseInt(ctxMatch[6]),
        source: ctxMatch[7],
        estimate: ctxMatch[8] === '1',
      },
    };
  }

  if (line.startsWith('[USAGE] ')) {
    try {
      const parsed = JSON.parse(line.slice(8)) as Record<string, unknown>;
      const input = typeof parsed.input === 'number' ? parsed.input : undefined;
      const output = typeof parsed.output === 'number' ? parsed.output : undefined;
      return {
        type: 'usage',
        payload: {
          provider: String(parsed.provider ?? parsed.source ?? 'unknown'),
          model: typeof parsed.model === 'string' ? parsed.model : undefined,
          mode: typeof parsed.mode === 'string' ? parsed.mode : undefined,
          input,
          output,
          total: typeof input === 'number' && typeof output === 'number' ? input + output : undefined,
          usage_available: Boolean(parsed.usage_available),
          tokens_per_second: typeof parsed.tokens_per_second === 'number' ? parsed.tokens_per_second : undefined,
          context_limit: typeof parsed.context_limit === 'number' ? parsed.context_limit : undefined,
          context_limit_source: typeof parsed.context_limit_source === 'string' ? parsed.context_limit_source : undefined,
          billing: typeof parsed.billing === 'string' ? parsed.billing : undefined,
          total_duration: typeof parsed.total_duration === 'number' ? parsed.total_duration : undefined,
          load_duration: typeof parsed.load_duration === 'number' ? parsed.load_duration : undefined,
          prompt_eval_duration: typeof parsed.prompt_eval_duration === 'number' ? parsed.prompt_eval_duration : undefined,
          eval_duration: typeof parsed.eval_duration === 'number' ? parsed.eval_duration : undefined,
        },
      };
    } catch {
      // Ignore malformed usage lines.
    }
  }

  // [ANSWER] is a final assistant message from the Codex browser bridge.
  const answerText = parseAnswerLine(line);
  if (answerText !== null) {
    return { type: 'answer', payload: answerText };
  }

  // Surface structured one-shot coding-agent JSON
  const stdoutLine = line.startsWith('[err]') ? line.slice(5).trimStart() : line;
  if (stdoutLine.startsWith('{')) {
    try {
      const parsed = JSON.parse(stdoutLine) as {
        error?: string;
        type?: string;
        message?: string;
        tool_uses?: unknown[];
        tool_results?: unknown[];
        iterations?: number;
        item?: { type?: string; text?: string; action?: { command?: string } };
      };

      // New Codex CLI JSONL format (codex exec --json)
      if (parsed.type === 'item.completed' && parsed.item) {
        const item = parsed.item;
        if (item.type === 'agent_message' && item.text?.trim()) {
          return { type: 'codex_agent_message', payload: item.text.trim() };
        } else if (item.type === 'reasoning' && item.text?.trim()) {
          return { type: 'codex_reasoning', payload: cleanActivityText(item.text.trim()) };
        } else if (item.type === 'local_shell_call' && item.action?.command) {
          return { type: 'codex_shell_call', payload: item.action.command };
        }
        return { type: 'codex_ignored' };
      }
      if (parsed.type === 'thread.started' || parsed.type === 'turn.started' || parsed.type === 'turn.completed') {
        return { type: 'codex_ignored' };
      }

      const errorMsg = (parsed.error ?? '').trim();
      if (errorMsg) {
        return { type: 'error', payload: friendlyError(errorMsg) };
      }

      const msg = (parsed.message ?? '').trim();
      const looksLikeResult =
        msg &&
        (typeof parsed.iterations === 'number' ||
          Array.isArray(parsed.tool_uses) ||
          Array.isArray(parsed.tool_results) ||
          parsed.type === 'result');
      if (looksLikeResult) {
        return { type: 'answer', payload: msg };
      }

      if (msg && (parsed.type === 'error' || /credential|api[_ ]?key|unauthorized/i.test(msg))) {
        return { type: 'error', payload: friendlyError(msg) };
      }
    } catch {
      /* not JSON, fall through */
    }
  }

  // Handle [DIFF] lines — annotate the previous file-write activity item
  const diffMatch = line.match(/\[DIFF\]\s+path=(\S+)\s+\+(\d+)\s+-(\d+)/);
  if (diffMatch) {
    return {
      type: 'diff',
      payload: {
        path: diffMatch[1],
        added: parseInt(diffMatch[2]),
        removed: parseInt(diffMatch[3]),
      },
    };
  }

  if (line.includes('[UI] SCREENSHOT_FLASH')) {
    return { type: 'screenshot_flash' };
  }
  if (line.includes('[UI] SHOW')) {
    return { type: 'show_window' };
  }

  const item = parseLogLine(line, id);
  if (!item) {
    return { type: 'none' };
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
      try {
        const parsed = JSON.parse(item.text);
        if (typeof parsed === 'object' && parsed !== null && typeof parsed.text === 'string') {
          item.text = parsed.text;
        }
      } catch {
        // Not valid JSON — drop lines that look like raw JSON artifacts
        if (/^\s*\{.*\}\s*$/.test(item.text)) return { type: 'none' };
      }
    }
  }

  const needHelpMatch = line.match(/(?:^|\b)NEED_HELP:\s*(.+)$/i);
  if (needHelpMatch) {
    return {
      type: 'need_help',
      payload: needHelpMatch[1].trim() || 'Kim needs your help to continue.',
    };
  }

  if (item.kind === 'error') {
    return { type: 'error', payload: item.text };
  }

  return { type: 'activity_item', payload: item };
}

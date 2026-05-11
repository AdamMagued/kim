import { useState } from 'react';
import type { ToolUseBlock, ToolResultBlock } from '../types';

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      className={`kim-tool-card__chevron${open ? ' kim-tool-card__chevron--open' : ''}`}
      viewBox="0 0 16 16"
      width="11"
      height="11"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M4 6l4 4 4-4" />
    </svg>
  );
}

function WrenchIcon() {
  return (
    <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M10.5 2a3.5 3.5 0 0 0-3.3 4.7L2 11.9 4.1 14l5.2-5.2A3.5 3.5 0 1 0 10.5 2z" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 8.5l3 3 7-7" />
    </svg>
  );
}

function basename(path: string): string {
  return path.split(/[/\\]/).pop() || path;
}

function stringifyCompact(value: unknown): string {
  if (typeof value === 'string') return value;
  return JSON.stringify(value, null, 2);
}

type ParsedToolResult = {
  label: string;
  body: string;
  stats?: { added: number; removed: number };
  diffLines?: string[];
  code?: boolean;
};

function parseMaybeJson(value: unknown): unknown {
  if (typeof value !== 'string') return value;
  try { return JSON.parse(value); } catch { return value; }
}

function diffStatsFromPatch(patch: unknown): { added: number; removed: number; lines: string[] } | null {
  if (!Array.isArray(patch)) return null;
  let added = 0;
  let removed = 0;
  const lines: string[] = [];

  for (const hunk of patch) {
    if (!hunk || typeof hunk !== 'object') continue;
    const rawLines = (hunk as { lines?: unknown }).lines;
    if (!Array.isArray(rawLines)) continue;
    for (const rawLine of rawLines) {
      if (typeof rawLine !== 'string') continue;
      if (rawLine.startsWith('+')) {
        added += 1;
        lines.push(rawLine);
      } else if (rawLine.startsWith('-')) {
        removed += 1;
        lines.push(rawLine);
      }
    }
  }

  if (added === 0 && removed === 0 && lines.length === 0) return null;
  return { added, removed, lines };
}

function parseToolResult(content: string, fallbackToolName = 'Search'): ParsedToolResult {
  try {
    const parsed = JSON.parse(content) as { output?: unknown; filePath?: string; type?: string; tool_name?: string; toolName?: string };
    const output = parsed && typeof parsed === 'object' && 'output' in parsed
      ? parseMaybeJson(parsed.output)
      : parsed;
    if (output && typeof output === 'object') {
      const out = output as {
        filePath?: string;
        content?: string;
        type?: string;
        stdout?: string;
        stderr?: string;
        noOutputExpected?: boolean;
        structuredPatch?: unknown;
        file?: { filePath?: string; content?: string; startLine?: number; numLines?: number; totalLines?: number };
        numFiles?: number;
        numMatches?: number;
        filenames?: string[];
      };
      const patch = diffStatsFromPatch(out.structuredPatch);
      if (typeof out.content === 'string') {
        const filename = out.filePath ? basename(out.filePath) : 'file';
        if (patch) {
          return {
            label: `${out.type === 'create' ? 'Created' : 'Updated'} ${filename}`,
            body: out.content,
            stats: { added: patch.added, removed: patch.removed },
            diffLines: patch.lines,
            code: true,
          };
        }
        return {
          label: out.filePath ? `Updated ${filename}` : 'File updated',
          body: out.content,
          stats: { added: Math.max(1, out.content.split(/\r?\n/).length), removed: 0 },
          code: true,
        };
      }
      if (out.file && typeof out.file.content === 'string') {
        const file = out.file;
        const fileContent = String(file.content ?? '');
        const start = file.startLine ?? 1;
        const count = file.numLines ?? fileContent.split(/\r?\n/).length;
        const total = file.totalLines ?? count;
        return {
          label: `Read ${basename(file.filePath ?? 'file')} · lines ${start}-${Math.max(start, start + count - 1)} of ${total}`,
          body: fileContent,
          code: true,
        };
      }
      if (typeof out.numMatches === 'number' || typeof out.numFiles === 'number') {
        const name = parsed.tool_name ?? parsed.toolName ?? fallbackToolName;
        const files = Array.isArray(out.filenames) ? out.filenames.slice(0, 8).join('\n') : '';
        const summary = typeof out.numMatches === 'number'
          ? `${out.numMatches} matches across ${out.numFiles ?? 0} files`
          : `${out.numFiles ?? 0} files matched`;
        return {
          label: `${name} · ${summary}`,
          body: [typeof out.content === 'string' ? out.content : '', files].filter(Boolean).join('\n\n'),
        };
      }
      if (out.noOutputExpected && !out.stdout && !out.stderr) {
        return { label: 'Command completed', body: '' };
      }
      if (typeof out.stdout === 'string' || typeof out.stderr === 'string') {
        return {
          label: out.stderr ? 'Command output' : 'Command completed',
          body: [out.stdout, out.stderr].filter(Boolean).join('\n').trim(),
        };
      }
    }
    if (typeof parsed.filePath === 'string') {
      return { label: `Updated ${basename(parsed.filePath)}`, body: stringifyCompact(parsed) };
    }
  } catch {
    // Fall back to raw output.
  }
  return { label: 'Tool result', body: content };
}

// ── Tool-use card ─────────────────────────────────────────────────────────────

interface ToolUseCardProps {
  block: ToolUseBlock;
  result?: string;
}

export function ToolUseCard({ block, result }: ToolUseCardProps) {
  const [open, setOpen] = useState(false);
  const input = block.input && typeof block.input === 'object' && !Array.isArray(block.input)
    ? block.input
    : {};
  const path = typeof input.path === 'string' ? input.path : '';
  const content = typeof input.content === 'string' ? input.content : '';
  const isFileWrite = block.name === 'write_file' && content;
  const lineCount = isFileWrite ? Math.max(1, content.split(/\r?\n/).length) : 0;
  const argsStr = isFileWrite
    ? content
    : JSON.stringify(input, null, 2);

  let displayName = block.name;
  if (isFileWrite) {
    displayName = `write_file · ${basename(path)}`;
  } else if (block.name === 'bash' && typeof input.command === 'string' && input.command.trim().startsWith('open ')) {
    displayName = `Opened ${basename(input.command.trim().slice(5))}`;
  } else if (block.name === 'run_command') {
    displayName = 'Ran a command';
  } else if (block.name === 'bash') {
    displayName = 'Ran a command';
  }

  return (
    <div className="kim-tool-card">
      <button onClick={() => setOpen(o => !o)} className="kim-tool-card__header">
        <span className="kim-tool-card__icon" aria-hidden>
          <WrenchIcon />
        </span>
        <span className="kim-tool-card__name">{displayName}</span>
        {isFileWrite && <span className="kim-tool-card__meta">+{lineCount}</span>}
        <Chevron open={open} />
      </button>

      {open && (
        <div className="kim-tool-card__body">
          <div style={{ marginBottom: 10 }}>
            <div className="kim-tool-card__section-label">{isFileWrite ? `Code written to ${basename(path)}` : 'Input'}</div>
            <pre className={`kim-tool-card__pre${isFileWrite ? ' kim-tool-card__pre--code' : ''}`}>
              <code>{argsStr}</code>
            </pre>
          </div>

          {result !== undefined && (
            <div>
              <div className="kim-tool-card__section-label">Result</div>
              <pre className="kim-tool-card__pre">{result}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Tool-result card ──────────────────────────────────────────────────────────

interface ToolResultCardProps {
  block: ToolResultBlock;
}

export function ToolResultCard({ block }: ToolResultCardProps) {
  const [open, setOpen] = useState(false);
  const rawBlock = block as ToolResultBlock & { output?: unknown; tool_name?: string };
  const rawContent = rawBlock.content ?? rawBlock.output ?? '';
  const content =
    typeof rawContent === 'string' ? rawContent : JSON.stringify(rawContent, null, 2);
  const result = parseToolResult(content, rawBlock.tool_name ?? 'Search');
  if (!result.body.trim() && result.label === 'Command completed') {
    return null;
  }

  return (
    <div className="kim-tool-card">
      <button onClick={() => setOpen(o => !o)} className="kim-tool-card__header">
        <span className="kim-tool-card__icon kim-tool-card__icon--result" aria-hidden>
          <CheckIcon />
        </span>
        <span className="kim-tool-card__name kim-tool-card__name--neutral">{result.label}</span>
        {result.stats && (
          <span className="kim-tool-card__stats" aria-label={`${result.stats.added} lines added, ${result.stats.removed} lines removed`}>
            <span className="kim-tool-card__stat-add">+{result.stats.added}</span>
            {result.stats.removed > 0 && <span className="kim-tool-card__stat-del">-{result.stats.removed}</span>}
          </span>
        )}
        <Chevron open={open} />
      </button>
      {open && (
        <div className="kim-tool-card__body">
          {result.diffLines && result.diffLines.length > 0 && (
            <div style={{ marginBottom: 10 }}>
              <div className="kim-tool-card__section-label">Diff</div>
              <pre className="kim-tool-card__pre kim-tool-card__pre--diff">
                <code>
                  {result.diffLines.slice(0, 160).map((line, i) => (
                    <span
                      key={i}
                      className={
                        line.startsWith('+')
                          ? 'kim-tool-card__diff-add'
                          : line.startsWith('-')
                            ? 'kim-tool-card__diff-del'
                            : undefined
                      }
                    >
                      {line}
                      {'\n'}
                    </span>
                  ))}
                  {result.diffLines.length > 160 && <span className="kim-tool-card__diff-muted">... diff truncated for display</span>}
                </code>
              </pre>
            </div>
          )}
          {!result.diffLines && (
            <>
              <div className="kim-tool-card__section-label">{result.code ? 'Content' : 'Output'}</div>
              <pre className={`kim-tool-card__pre${result.code ? ' kim-tool-card__pre--code' : ''}`}><code>{result.body}</code></pre>
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ── Signal card (for NEED_HELP and TASK_COMPLETE) ─────────────────────────────

interface SignalCardProps {
  kind: 'success' | 'error';
  text: string;
  onAction?: () => void;
  actionLabel?: string;
}

export function SignalCard({ kind, text, onAction, actionLabel }: SignalCardProps) {
  const [open, setOpen] = useState(false);
  const isError = kind === 'error';

  return (
    <div className={`kim-tool-card ${isError ? 'kim-tool-card--error' : ''}`}>
      <button onClick={() => setOpen(o => !o)} className="kim-tool-card__header">
        <span className={`kim-tool-card__icon ${isError ? 'kim-tool-card__icon--error' : 'kim-tool-card__icon--result'}`} aria-hidden>
          {isError ? '⚠' : <CheckIcon />}
        </span>
        <span className={`kim-tool-card__name ${isError ? 'kim-tool-card__name--error' : 'kim-tool-card__name--neutral'}`}>
          {isError ? 'Needs Help' : 'Task Complete'}
        </span>
        <Chevron open={open} />
      </button>
      {open && (
        <div className="kim-tool-card__body">
          <pre className="kim-tool-card__pre" style={{ whiteSpace: 'pre-wrap' }}>{text}</pre>
          {onAction && actionLabel && (
            <div style={{ marginTop: '8px' }}>
              <button 
                type="button" 
                className="kim-task-error__retry" 
                onClick={onAction}
              >
                {actionLabel}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

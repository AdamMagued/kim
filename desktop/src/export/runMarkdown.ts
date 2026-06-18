/**
 * K10: build a Markdown export of a run.
 *
 * Pure function (no DOM, no Tauri) so it is snapshot-testable. The host maps its
 * own message/activity types onto `RunExport` before calling.
 */

export interface RunExportMessage {
  role: 'user' | 'assistant';
  text: string;
}

export interface RunExportTouchedFile {
  path: string;
  added: number;
  removed: number;
}

export interface RunExport {
  title?: string;
  provider?: string;
  model?: string;
  durationSec?: number;
  costUsd?: number;
  messages: RunExportMessage[];
  /** Collapsed activity lines (status/tool summaries). */
  activity?: string[];
  touchedFiles?: RunExportTouchedFile[];
  timestamp?: string;
}

function fmtDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return s === 0 ? `${m}m` : `${m}m ${s}s`;
}

export function buildRunMarkdown(run: RunExport): string {
  const lines: string[] = [];
  lines.push(`# ${run.title?.trim() || 'Kim run'}`);
  lines.push('');

  const meta: string[] = [];
  if (run.provider) meta.push(`**Provider:** ${run.provider}`);
  if (run.model) meta.push(`**Model:** ${run.model}`);
  if (typeof run.durationSec === 'number') meta.push(`**Duration:** ${fmtDuration(run.durationSec)}`);
  if (typeof run.costUsd === 'number') meta.push(`**Cost:** $${run.costUsd.toFixed(4)}`);
  if (run.timestamp) meta.push(`**When:** ${run.timestamp}`);
  if (meta.length) {
    lines.push(meta.join('  ·  '));
    lines.push('');
  }

  lines.push('## Conversation');
  lines.push('');
  for (const m of run.messages) {
    const who = m.role === 'user' ? 'User' : 'Kim';
    lines.push(`### ${who}`);
    lines.push('');
    lines.push(m.text.trim() || '_(empty)_');
    lines.push('');
  }

  if (run.activity && run.activity.length) {
    lines.push('## Activity');
    lines.push('');
    for (const a of run.activity) lines.push(`- ${a}`);
    lines.push('');
  }

  if (run.touchedFiles && run.touchedFiles.length) {
    lines.push('## Files touched');
    lines.push('');
    for (const f of run.touchedFiles) {
      lines.push(`- \`${f.path}\` (+${f.added}/-${f.removed})`);
    }
    lines.push('');
  }

  // Single trailing newline, no double blank at EOF.
  return lines.join('\n').replace(/\n+$/, '\n');
}

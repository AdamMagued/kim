import { describe, it, expect } from 'vitest';
import { estimateCostUsd, formatCostUsd, collapseMessages, friendlyError, parseLogLine } from '../utils';

describe('estimateCostUsd', () => {
  it('returns 0 for ollama (local)', () => {
    expect(estimateCostUsd('ollama', 100_000, 50_000)).toBe(0);
  });

  it('returns 0 for browser (local)', () => {
    expect(estimateCostUsd('browser', 100_000, 50_000)).toBe(0);
  });

  it('returns 0 for browser:* session providers (B5 — not claude rates)', () => {
    expect(estimateCostUsd('browser:claude', 100_000, 50_000)).toBe(0);
    expect(estimateCostUsd('browser:chatgpt', 1_000_000, 1_000_000)).toBe(0);
    expect(estimateCostUsd('BROWSER:Gemini', 500_000, 500_000)).toBe(0);
  });

  it('calculates cost for claude at known rates', () => {
    // claude: $3/1M input, $15/1M output
    const cost = estimateCostUsd('claude', 1_000_000, 1_000_000);
    expect(cost).toBeCloseTo(18.0, 5);
  });

  it('calculates cost for deepseek', () => {
    const cost = estimateCostUsd('deepseek', 1_000_000, 0);
    expect(cost).toBeCloseTo(0.27, 5);
  });

  it('returns null for unknown provider (no fabricated cost)', () => {
    expect(estimateCostUsd('unknown-provider', 500_000, 200_000)).toBeNull();
  });

  it('handles zero tokens', () => {
    expect(estimateCostUsd('claude', 0, 0)).toBe(0);
  });
});

describe('formatCostUsd', () => {
  it('formats zero as $0.00', () => {
    expect(formatCostUsd(0)).toBe('$0.00');
  });

  it('formats tiny value as <$0.0001', () => {
    expect(formatCostUsd(0.00001)).toBe('<$0.0001');
  });

  it('formats small value to 4 decimal places', () => {
    expect(formatCostUsd(0.001234)).toBe('$0.0012');
  });

  it('formats larger value to 4 decimal places', () => {
    expect(formatCostUsd(1.23456)).toBe('$1.2346');
  });
});

describe('collapseMessages srcIdx (B2)', () => {
  it('maps each collapsed entry back to its ORIGINAL index after retry-collapse', () => {
    const msgs = [
      { role: 'user', content: 'q1' },
      { role: 'assistant', content: '{"text":"a"}' },
      { role: 'assistant', content: '{"text":"a"}' }, // duplicate retry → collapses
      { role: 'user', content: 'q2' },
    ] as any;

    const collapsed = collapseMessages(msgs);

    expect(collapsed).toHaveLength(3);
    expect(collapsed[0].srcIdx).toBe(0);
    expect(collapsed[1].retries).toBe(1);
    expect(collapsed[1].srcIdx).toBe(1);
    // The bug: editing q2 used collapsed index 2, hitting msgs[2] (the duplicate
    // assistant). srcIdx must be its real index, 3.
    expect(collapsed[2].srcIdx).toBe(3);
    expect((collapsed[2].msg as any).content).toBe('q2');
  });

  it('srcIdx is identity when nothing collapses', () => {
    const msgs = [
      { role: 'user', content: 'a' },
      { role: 'assistant', content: 'plain' },
      { role: 'user', content: 'b' },
    ] as any;
    const collapsed = collapseMessages(msgs);
    expect(collapsed.map(c => c.srcIdx)).toEqual([0, 1, 2]);
  });
});

const GENERIC_FALLBACK = 'Something went wrong. Check your settings and try again.';

describe('friendlyError — real crash text is surfaced (audit B3/B4/C20)', () => {
  // The old scrubber deleted any error containing a path / ".py" / traceback
  // fragment and replaced it with the generic fallback — destroying the one
  // clue the user needs (ModuleNotFoundError, NEED_HELP questions, etc.). The
  // fix surfaces the cleaned text; it must NOT collapse to the generic string.
  it('surfaces a ModuleNotFoundError verbatim', () => {
    const msg = "ModuleNotFoundError: No module named 'mcp'";
    expect(friendlyError(msg)).toBe(msg);
  });

  it('keeps a message that mentions a file path', () => {
    const out = friendlyError('process died at /home/user/project/run');
    expect(out).toBe('process died at /home/user/project/run');
    expect(out).not.toBe(GENERIC_FALLBACK);
  });

  it('keeps a .py reference instead of deleting it', () => {
    const out = friendlyError('Exception raised in runner.py:88');
    expect(out).toContain('runner.py');
    expect(out).not.toBe(GENERIC_FALLBACK);
  });

  it('keeps a NEED_HELP-style long question intact', () => {
    const q = 'I need to know which database you want me to use before I can '
      + 'continue — Postgres, SQLite, or something else? Please reply with one.';
    expect(friendlyError(q)).toBe(q);
  });
});

describe('friendlyError — short clean message passthrough', () => {
  it('friendlyError_keeps_short_clean_message: plain short message is returned verbatim', () => {
    expect(friendlyError('Workspace folder not found')).toBe('Workspace folder not found');
  });

  it('friendlyError_keeps_short_clean_message: [INFO]/orchestrator prefix is stripped', () => {
    // The cleaner strips [LEVEL] tags and orchestrator.xxx: prefixes before returning.
    const result = friendlyError('[INFO] orchestrator.runner: Task is complete');
    expect(result).toBe('Task is complete');
    expect(result).not.toContain('[INFO]');
    expect(result).not.toContain('orchestrator');
  });

  it('friendlyError_keeps_short_clean_message: [ERROR] prefix is stripped', () => {
    const result = friendlyError('[ERROR] Something specific happened');
    expect(result).toBe('Something specific happened');
  });

  it('friendlyError_caps_very_long_message_with_ellipsis: >600 chars truncates but is not discarded', () => {
    const long = 'A'.repeat(700);
    const out = friendlyError(long);
    expect(out).not.toBe(GENERIC_FALLBACK);
    expect(out.endsWith('…')).toBe(true);
    expect(out.length).toBe(601); // 600 chars + ellipsis
  });

  it('friendlyError_falls_back_only_when_nothing_usable_remains', () => {
    expect(friendlyError('   ')).toBe(GENERIC_FALLBACK);
  });
});

describe('parseLogLine — balanced-paren arg extraction', () => {
  it('parseLogLine_balanced_paren_args: extracts full args when JSON exceeds 200 chars', () => {
    // Build a [TOOL] line whose JSON args string is well over 200 chars.
    // If extraction naively truncated at 200 chars the JSON.parse would fail and
    // the label would fall back to `Reading \`\`` (empty basename).
    const longDir = 'a'.repeat(180);
    const longPath = `/some/${longDir}/deep/target_file.txt`;
    const argsJson = JSON.stringify({ path: longPath });
    // Sanity-check that the args themselves are over 200 chars so the test is meaningful.
    expect(argsJson.length).toBeGreaterThan(200);

    const rawLine = `[TOOL] read_file(${argsJson})`;
    const result = parseLogLine(rawLine, 1);
    expect(result).not.toBeNull();
    expect(result?.kind).toBe('tool');
    // The label uses basename(), so it should contain the filename, not empty string.
    expect(result?.text).toContain('target_file.txt');
  });

  it('parseLogLine_balanced_paren_args: nested parens in string args do not confuse scanner', () => {
    // A command that contains parentheses inside a string should still be balanced correctly.
    const cmd = 'echo "result (ok)"';
    const argsJson = JSON.stringify({ command: cmd });
    const rawLine = `[TOOL] run_command(${argsJson})`;
    const result = parseLogLine(rawLine, 2);
    expect(result).not.toBeNull();
    expect(result?.kind).toBe('tool');
    expect(result?.text).toContain('echo');
  });
});

describe('estimateCostUsd — provider normalization regression', () => {
  it('estimateCostUsd_null_for_unknown_provider: mystery provider returns null', () => {
    expect(estimateCostUsd('mystery', 1_000_000, 1_000_000)).toBeNull();
    expect(estimateCostUsd('gpt-4-turbo', 500_000, 200_000)).toBeNull();
  });

  it('estimateCostUsd_null_for_unknown_provider: browser:claude normalizes to zero-cost', () => {
    // browser:* providers are free local sessions and must not be billed at claude rates.
    expect(estimateCostUsd('browser:claude', 2_000_000, 2_000_000)).toBe(0);
    expect(estimateCostUsd('BROWSER:CLAUDE', 1_000_000, 1_000_000)).toBe(0);
  });

  it('estimateCostUsd_null_for_unknown_provider: known providers compute a positive number', () => {
    // openai: $2.50/1M input, $10.00/1M output
    const openaiCost = estimateCostUsd('openai', 1_000_000, 1_000_000);
    expect(openaiCost).not.toBeNull();
    expect(openaiCost!).toBeCloseTo(12.5, 5);

    // gemini: $1.25/1M input, $5.00/1M output
    const geminiCost = estimateCostUsd('gemini', 1_000_000, 1_000_000);
    expect(geminiCost).not.toBeNull();
    expect(geminiCost!).toBeCloseTo(6.25, 5);
  });
});

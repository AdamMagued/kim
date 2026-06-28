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

describe('friendlyError — sensitive detail rejection', () => {
  it('friendlyError_rejects_sensitive_detail: file path triggers fallback', () => {
    // A slash-separated path segment is sensitive and must never surface in the UI.
    expect(friendlyError('process died at /home/user/project/run')).toBe(GENERIC_FALLBACK);
  });

  it('friendlyError_rejects_sensitive_detail: Traceback keyword triggers fallback', () => {
    expect(friendlyError('Traceback (most recent call last)')).toBe(GENERIC_FALLBACK);
  });

  it('friendlyError_rejects_sensitive_detail: File "..." reference triggers fallback', () => {
    expect(friendlyError('File "/opt/project/main.py", line 42, in run')).toBe(GENERIC_FALLBACK);
  });

  it('friendlyError_rejects_sensitive_detail: .py reference triggers fallback', () => {
    expect(friendlyError('Exception raised in runner.py:88')).toBe(GENERIC_FALLBACK);
  });

  it('friendlyError_rejects_sensitive_detail: JS stack frame triggers fallback', () => {
    // Matches /^\s*at\s+\S+\s+\(/ after cleaning prefixes.
    expect(friendlyError('at Object.callFn (bundle.js:1:100)')).toBe(GENERIC_FALLBACK);
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

  it('friendlyError_keeps_short_clean_message: messages >=200 chars fall back to generic', () => {
    const long = 'A'.repeat(200);
    expect(friendlyError(long)).toBe(GENERIC_FALLBACK);
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

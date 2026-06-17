import { describe, it, expect } from 'vitest';
import { estimateCostUsd, formatCostUsd, collapseMessages } from '../utils';

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

  it('falls back to claude rates for unknown provider', () => {
    const knownCost = estimateCostUsd('claude', 500_000, 200_000);
    const unknownCost = estimateCostUsd('unknown-provider', 500_000, 200_000);
    expect(unknownCost).toBeCloseTo(knownCost, 10);
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

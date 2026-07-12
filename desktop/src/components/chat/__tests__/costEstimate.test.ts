import { describe, it, expect } from 'vitest';
import { estimateCostUsd, costBasisLabel, PRICE_LAST_REFRESHED } from '../utils';
import { ALL_PROVIDERS } from '../../../types';

// F-F-7: the price table was self-admittedly stale ("2025-Q2") with model IDs
// that no longer match what the app dispatches, and the cost chip showed a
// confident USD with no in-UI "estimate" caveat. These tests pin: (1) every
// dispatchable provider has a cost basis (or the UI shows nothing, never a
// fabricated number), (2) the estimate is explicitly qualified, (3) local
// providers are zero-cost, (4) browser:* variants normalize to the free basis.
describe('cost estimation (F-F-7)', () => {
  it('every dispatchable provider has a cost basis label', () => {
    for (const p of ALL_PROVIDERS) {
      expect(costBasisLabel(p.value), `basis for ${p.value}`).not.toBeNull();
    }
  });

  it('paid providers qualify the figure as an estimate with a dated basis', () => {
    for (const p of ['claude', 'openai', 'gemini', 'deepseek']) {
      const label = costBasisLabel(p);
      expect(label).toContain('est.');
      expect(label).toContain(PRICE_LAST_REFRESHED);
    }
    // The refresh marker must not be the stale 2025-Q2 value the finding flagged.
    expect(PRICE_LAST_REFRESHED).not.toBe('2025-Q2');
  });

  it('local providers are zero-cost and browser variants normalize to free', () => {
    expect(estimateCostUsd('ollama', 1_000_000, 1_000_000)).toBe(0);
    expect(estimateCostUsd('browser', 1_000_000, 1_000_000)).toBe(0);
    expect(estimateCostUsd('browser:claude', 500, 500)).toBe(0);
    expect(costBasisLabel('browser:chatgpt')).toContain('$0');
  });

  it('unknown providers yield null (no fabricated cost)', () => {
    expect(estimateCostUsd('mystery-llm', 1000, 1000)).toBeNull();
    expect(costBasisLabel('mystery-llm')).toBeNull();
  });

  it('claude estimate math is correct (3/1M in, 15/1M out)', () => {
    // 1M input + 1M output → $3 + $15 = $18.
    expect(estimateCostUsd('claude', 1_000_000, 1_000_000)).toBeCloseTo(18, 6);
  });
});

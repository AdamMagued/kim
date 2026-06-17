import { describe, it, expect } from 'vitest';
import { estimateCostUsd, formatCostUsd } from '../utils';

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

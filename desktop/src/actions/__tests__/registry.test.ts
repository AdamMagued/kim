import { describe, it, expect, vi } from 'vitest';
import { buildActions, filterActions, isSubsequence } from '../registry';

describe('buildActions (K8)', () => {
  it('omits actions whose callback is not provided', () => {
    const actions = buildActions({ newChat: () => {} });
    expect(actions.map(a => a.id)).toContain('new-chat');
    expect(actions.map(a => a.id)).not.toContain('cancel-run');
  });

  it('expands one provider action per provider', () => {
    const actions = buildActions({ switchProvider: () => {}, providers: ['claude', 'ollama'] });
    expect(actions.map(a => a.id)).toEqual(
      expect.arrayContaining(['provider-claude', 'provider-ollama'])
    );
  });

  it('wires run() to the supplied callback', () => {
    const spy = vi.fn();
    const actions = buildActions({ cancelRun: spy, isRunning: true });
    actions.find(a => a.id === 'cancel-run')!.run();
    expect(spy).toHaveBeenCalledOnce();
  });
});

describe('filterActions (K8)', () => {
  const actions = buildActions({
    newChat: () => {},
    newCodeSession: () => {},
    openSettings: () => {},
    togglePrivacyPause: () => {},
  });

  it('empty query returns all enabled', () => {
    expect(filterActions(actions, '').length).toBe(actions.length);
  });

  it('ranks title prefix above keyword-only match', () => {
    const res = filterActions(actions, 'new');
    expect(res[0].title.toLowerCase().startsWith('new')).toBe(true);
  });

  it('matches via keywords', () => {
    const res = filterActions(actions, 'screenshot'); // privacy pause keyword
    expect(res.map(a => a.id)).toContain('privacy-pause');
  });

  it('subsequence fallback finds non-contiguous matches', () => {
    const res = filterActions(actions, 'ncs'); // n(ew) c(ode) s(ession)
    expect(res.map(a => a.id)).toContain('new-code');
  });

  it('hides disabled actions', () => {
    const a = buildActions({ cancelRun: () => {}, isRunning: false });
    expect(filterActions(a, '').map(x => x.id)).not.toContain('cancel-run');
  });
});

describe('isSubsequence', () => {
  it('true for in-order chars', () => {
    expect(isSubsequence('ace', 'abcde')).toBe(true);
  });
  it('false for out-of-order', () => {
    expect(isSubsequence('aec', 'abcde')).toBe(false);
  });
});

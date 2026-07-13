import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/react';
import * as parsers from '../parsers';
import { StreamRenderer, type StreamRendererProps } from '../StreamRenderer';
import { DEFAULT_SETTINGS, type KimMessage, type KimAccount } from '../../../types';
import type { ActivityItem } from '../types';

// F-F-11: a kim:stats tick used to churn renderWorkedFor's identity (its only
// useCallback dep was tokenStats), which forced the savedMsgNodes memo to rebuild
// the ENTIRE saved history — re-running buildThinkingTrace / parsePlanFromActivity
// for every saved row — even though saved rows never read cost. tokenStats is now
// read from a ref, so a stats-only change must NOT recompute the saved history.

const toolActivity: ActivityItem[] = [
  { id: 1, kind: 'tool', icon: '📖', text: 'Reading `a.txt`' },
];

function makeProps(tokenStats: { input: number; output: number; total: number }): StreamRendererProps {
  const messages: KimMessage[] = [
    { role: 'user', content: 'do the thing' },
    { role: 'assistant', content: 'done' },
  ];
  const noopRef = { current: null };
  return {
    messages,
    loadingMessages: false,
    loadError: null,
    liveHistory: [],
    runHistory: [{ activity: toolActivity, durationSec: 5, provider: 'claude' }],
    codexRuns: [],
    taskError: null,
    hitlApprovalStatus: null,
    onHitlRespond: vi.fn(),
    codexTurn: null,
    runFailure: null,
    rateLimitedState: null,
    settings: DEFAULT_SETTINGS,
    newChatMode: false,
    activity: [],
    isRunning: false,
    autoFollowOutput: false,
    setAutoFollowOutput: vi.fn(),
    bottomRef: noopRef,
    outputRef: noopRef,
    newestMsgIdx: null,
    queuedTasks: [],
    lastRunTask: null,
    elapsed: 0,
    handleRetryLast: vi.fn(),
    handleEditLiveMessage: vi.fn(),
    empty: false,
    renderComposer: () => null,
    renderConnectorsChrome: () => null,
    account: { display_name: 'Ada' } as KimAccount,
    activeTab: 'chat',
    activeProjectPath: null,
    textareaRef: noopRef,
    setTaskInput: vi.fn(),
    resolveProvider: () => 'claude',
    tokenStats,
    contextState: null,
  };
}

describe('StreamRenderer saved-history recompute (F-F-11)', () => {
  it('a tokenStats-only change does not rebuild the saved worked-for history', () => {
    const spy = vi.spyOn(parsers, 'buildThinkingTrace');
    // Build props ONCE so messages / runHistory / refs keep stable identities —
    // only tokenStats changes between the two renders (a pure kim:stats tick).
    const baseProps = makeProps({ input: 100, output: 50, total: 150 });
    const { rerender } = render(<StreamRenderer {...baseProps} />);

    // Sanity: the saved row's worked-for pill was built at least once.
    const afterInitial = spy.mock.calls.length;
    expect(afterInitial).toBeGreaterThan(0);

    // Same props, new tokenStats object only.
    rerender(<StreamRenderer {...baseProps} tokenStats={{ input: 200, output: 90, total: 290 }} />);

    // Saved history must NOT have been re-traced — the memo stayed stable.
    expect(spy.mock.calls.length).toBe(afterInitial);
    spy.mockRestore();
  });
});

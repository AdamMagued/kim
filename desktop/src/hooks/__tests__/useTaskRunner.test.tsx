import { renderHook, act, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { useTaskRunner } from '../useTaskRunner';

vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn(() => Promise.resolve()) }));
vi.mock('../../components/Toast', () => ({ toast: vi.fn() }));

import { invoke } from '@tauri-apps/api/core';
const invokeMock = invoke as unknown as ReturnType<typeof vi.fn>;

function ref<T>(value: T) {
  return { current: value };
}

function makeStream(isRunning: boolean) {
  return {
    isRunning,
    doneHandledRef: ref(false),
    cancelFlagRef: ref(false),
    needHelpFlagRef: ref(false),
    terminationReasonRef: ref<string | null>(null),
    lastProviderErrorCodeRef: ref<string | null>(null),
    answerReceivedThisRunRef: ref(false),
    recentActivityItemRef: ref(new Set<string>()),
    hasSentMessageRef: ref(false),
    currentTaskRef: ref<unknown>(null),
    lastRunTaskRef: ref<unknown>(null),
    completedCodeSessionRef: ref<unknown>(null),
    runOwnerSessionIdRef: ref<string | null>(null), // RUN-IDENTITY
    currentRunIdRef: ref<string | null>(null),
    activityRef: ref<unknown[]>([]),
    activityCounterRef: ref(0),
    lastFailedTask: null,
    setIsRunning: vi.fn(),
    clearActivityNow: vi.fn(),
    setLiveHistory: vi.fn(),
    setTaskError: vi.fn(),
    setTokenStats: vi.fn(),
    setHitlApprovalStatus: vi.fn(),
    setRunFailure: vi.fn(),
    setRateLimitedState: vi.fn(),
    setCancelling: vi.fn(),
    setActivity: vi.fn(),
    setLastFailedTask: vi.fn(),
  } as unknown as Parameters<typeof useTaskRunner>[0]['stream'];
}

function baseProps(stream: ReturnType<typeof makeStream>) {
  return {
    session: null,
    settings: {
      project_root: null,
      ollama: {
        mode: 'local',
        base_url: '',
        local_model: '',
        cloud_model: '',
        context_limit_override: null,
      },
      permission_mode: 'full_auto',
    },
    activeTab: 'chat',
    activeProjectPath: null,
    conversationId: 'conv-1',
    onTaskDone: vi.fn(),
    resolveProvider: () => 'claude',
    browserCommandArgs: () => ({}),
    stream,
    scroll: { setAutoFollowOutput: vi.fn() },
  } as unknown as Parameters<typeof useTaskRunner>[0];
}

describe('useTaskRunner queue drain (B1)', () => {
  beforeEach(() => {
    invokeMock.mockClear();
    invokeMock.mockResolvedValue(undefined);
  });

  it('runs a queued task automatically when the active run finishes', async () => {
    const stream = makeStream(true); // a run is in progress
    const { result, rerender } = renderHook(p => useTaskRunner(p), {
      initialProps: baseProps(stream),
    });

    // Submitting while running enqueues rather than runs.
    act(() => {
      result.current.handleSubmit('queued msg');
    });
    expect(result.current.queuedTasks).toHaveLength(1);
    expect(invokeMock).not.toHaveBeenCalledWith('send_task', expect.anything());

    // Run completes: isRunning flips false → queue should drain.
    stream.isRunning = false;
    rerender(baseProps(stream));

    await waitFor(() => {
      expect(invokeMock).toHaveBeenCalledWith(
        'send_task',
        expect.objectContaining({ task: 'queued msg', provider: 'claude' })
      );
    });
    expect(result.current.queuedTasks).toHaveLength(0);
  });

  it('runs immediately (no queue) when nothing is in progress', async () => {
    const stream = makeStream(false);
    const { result } = renderHook(p => useTaskRunner(p), {
      initialProps: baseProps(stream),
    });

    act(() => {
      result.current.handleSubmit('immediate msg');
    });

    await waitFor(() => {
      expect(invokeMock).toHaveBeenCalledWith(
        'send_task',
        expect.objectContaining({ task: 'immediate msg' })
      );
    });
    expect(result.current.queuedTasks).toHaveLength(0);
  });
});

describe('useTaskRunner — dead-setting wiring', () => {
  beforeEach(() => {
    invokeMock.mockClear();
    invokeMock.mockResolvedValue(undefined);
  });

  it('D-C1: does NOT queue when allow_message_queue is off', () => {
    const stream = makeStream(true); // run in progress
    const props = baseProps(stream);
    (props.settings as unknown as { allow_message_queue: boolean }).allow_message_queue = false;
    const { result } = renderHook(p => useTaskRunner(p), { initialProps: props });

    act(() => {
      result.current.handleSubmit('should not queue');
    });
    // Message is neither queued nor sent — the user is told to wait/steer.
    expect(result.current.queuedTasks).toHaveLength(0);
    expect(invokeMock).not.toHaveBeenCalledWith('send_task', expect.anything());
  });

  it('D-C3: forwards context_budget_tokens to send_task', async () => {
    const stream = makeStream(false);
    const props = baseProps(stream);
    (props.settings as unknown as { context_budget_tokens: number }).context_budget_tokens = 123_456;
    const { result } = renderHook(p => useTaskRunner(p), { initialProps: props });

    act(() => {
      result.current.handleSubmit('go');
    });
    await waitFor(() => {
      expect(invokeMock).toHaveBeenCalledWith(
        'send_task',
        expect.objectContaining({ contextBudgetTokens: 123_456 })
      );
    });
  });
});

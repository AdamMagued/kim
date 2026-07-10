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

  it('V-audit #2: does NOT queue a retry when allow_message_queue is off', () => {
    const stream = makeStream(true); // run in progress
    stream.lastRunTaskRef.current = { id: 1, text: 'earlier task', provider: 'claude' };
    const props = baseProps(stream);
    (props.settings as unknown as { allow_message_queue: boolean }).allow_message_queue = false;
    const { result } = renderHook(p => useTaskRunner(p), { initialProps: props });

    act(() => {
      result.current.handleRetryLast();
    });
    expect(result.current.queuedTasks).toHaveLength(0);
    expect(invokeMock).not.toHaveBeenCalledWith('send_task', expect.anything());
  });
});

// V-audit #1: queuedTasks lifted above the ChatView remount boundary. A real
// ChatView remount destroys this hook instance entirely and creates a new
// one; App.tsx's queuedTasksStore state is what's supposed to survive that.
// This test simulates the same thing without mounting the full App/ChatView
// tree: a plain external store object (mirroring a useState value+setter
// pair) is shared across two SEPARATE renderHook() instances — the second
// mount stands in for the post-remount ChatView.
describe('useTaskRunner queued-task store survives a remount (V-audit #1)', () => {
  beforeEach(() => {
    invokeMock.mockClear();
    invokeMock.mockResolvedValue(undefined);
  });

  function makeSharedStore() {
    let value: Record<string, { id: number; text: string; provider: string }[]> = {};
    return {
      get: () => value,
      set: (
        updater:
          | Record<string, { id: number; text: string; provider: string }[]>
          | ((
              prev: Record<string, { id: number; text: string; provider: string }[]>
            ) => Record<string, { id: number; text: string; provider: string }[]>)
      ) => {
        value = typeof updater === 'function' ? updater(value) : updater;
      },
    };
  }

  it('a message queued before an unmount is still there after a same-session remount', () => {
    const store = makeSharedStore();
    const streamA = makeStream(true); // run in progress
    const mountA = renderHook(p => useTaskRunner(p), {
      initialProps: {
        ...baseProps(streamA),
        queuedTasksStore: store.get(),
        setQueuedTasksStore: store.set,
      },
    });

    act(() => {
      mountA.result.current.handleSubmit('queued before remount');
    });
    // setQueuedTasksStore here is a plain external mutator (standing in for
    // React's real useState setter, which App.tsx uses and which triggers a
    // normal re-render on its own) — rerender with the fresh store snapshot
    // to observe it, exactly as the existing queue-drain test above does
    // after mutating `stream.isRunning` directly.
    mountA.rerender({ ...baseProps(streamA), queuedTasksStore: store.get(), setQueuedTasksStore: store.set });
    expect(mountA.result.current.queuedTasks).toHaveLength(1);
    expect(store.get()['conv-1']).toHaveLength(1);

    // Simulate New Chat / session switch: the OLD ChatView (and this hook
    // instance) is destroyed. Before the fix, queuedTasks lived in local
    // useState and this would be gone for good.
    mountA.unmount();

    // A brand-new hook instance mounts for the SAME session (conversationId
    // 'conv-1' from baseProps) — e.g. the user switched back, or a fresh
    // ChatView mounted while the run for conv-1 is still active elsewhere.
    // It's handed the SAME store object App.tsx would have kept alive.
    const streamB = makeStream(true);
    const mountB = renderHook(p => useTaskRunner(p), {
      initialProps: {
        ...baseProps(streamB),
        queuedTasksStore: store.get(),
        setQueuedTasksStore: store.set,
      },
    });

    // The queued message survived the remount instead of silently vanishing.
    expect(mountB.result.current.queuedTasks).toHaveLength(1);
    expect(mountB.result.current.queuedTasks[0].text).toBe('queued before remount');
  });

  it('a DIFFERENT session mounted after the switch does not see a foreign queue', () => {
    const store = makeSharedStore();
    const streamA = makeStream(true);
    const mountA = renderHook(p => useTaskRunner(p), {
      initialProps: {
        ...baseProps(streamA), // conversationId: 'conv-1'
        queuedTasksStore: store.get(),
        setQueuedTasksStore: store.set,
      },
    });
    act(() => { mountA.result.current.handleSubmit('queued for conv-1'); });
    mountA.unmount();

    // New Chat opens a fresh session (different conversationId) while
    // conv-1's run (and its queue) is still pending in the shared store.
    const streamB = makeStream(false);
    const propsB = { ...baseProps(streamB), conversationId: 'conv-2' };
    const mountB = renderHook(p => useTaskRunner(p), {
      initialProps: {
        ...propsB,
        queuedTasksStore: store.get(),
        setQueuedTasksStore: store.set,
      },
    });

    // conv-2's view must NOT show conv-1's queued message.
    expect(mountB.result.current.queuedTasks).toHaveLength(0);
    // ...but it's not lost either — still parked under conv-1 in the store.
    expect(store.get()['conv-1']).toHaveLength(1);
  });
});

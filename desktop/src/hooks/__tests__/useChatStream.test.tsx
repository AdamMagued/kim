import { renderHook, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { useChatStream } from '../useChatStream';
import { __testOnlyPendingRunSnapshots } from '../runSnapshotStore';
import { DEFAULT_SETTINGS } from '../../types';
import type { TraceItem } from '../../components/kim-ui/ThinkingWithPlan';

// ── Tauri mocks ───────────────────────────────────────────────────────────────
// listen() stores handlers in a map keyed by event name; emit() replays a
// payload into every registered handler, mimicking a Tauri event dispatch.
const { listeners } = vi.hoisted(() => ({
  listeners: new Map<string, Array<(e: { payload: unknown }) => void>>(),
}));

vi.mock('@tauri-apps/api/event', () => ({
  listen: vi.fn((event: string, handler: (e: { payload: unknown }) => void) => {
    const arr = listeners.get(event) ?? [];
    arr.push(handler);
    listeners.set(event, arr);
    return Promise.resolve(() => {
      const cur = listeners.get(event) ?? [];
      const idx = cur.indexOf(handler);
      if (idx >= 0) cur.splice(idx, 1);
    });
  }),
}));

vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn(() => Promise.resolve()) }));

// F-F-10: spy on Toast so we can assert the hook surfaces a save_run_history
// failure instead of silently swallowing it.
const toastMock = vi.fn();
vi.mock('../../components/Toast', () => ({ toast: (...a: unknown[]) => toastMock(...a) }));

import { invoke } from '@tauri-apps/api/core';
const invokeMock = invoke as unknown as ReturnType<typeof vi.fn>;

function emit(event: string, payload?: unknown) {
  act(() => {
    for (const h of [...(listeners.get(event) ?? [])]) h({ payload });
  });
}

function makeProps() {
  return {
    session: null,
    settings: DEFAULT_SETTINGS,
    onTaskDone: vi.fn(),
    commitCurrentBrowserUrl: vi.fn(() => Promise.resolve()),
    setMessageReloadNonce: vi.fn(),
    conversationId: 'conv-1',
  };
}

async function renderStream() {
  const props = makeProps();
  const utils = renderHook(() => useChatStream(props));
  // Listener registration happens synchronously inside the wiring effect, but
  // flush microtasks so the listen().then(unlisten) assignments settle too.
  await act(async () => {});
  return { ...utils, props };
}

function planTrace(traceItems: TraceItem[]) {
  return traceItems.find(t => t.kind === 'plan') as Extract<TraceItem, { kind: 'plan' }> | undefined;
}

beforeEach(() => {
  listeners.clear();
  invokeMock.mockClear();
  invokeMock.mockResolvedValue(undefined);
  toastMock.mockClear();
  __testOnlyPendingRunSnapshots.clear();
});

afterEach(() => {
  vi.useRealTimers();
});

// ── 1. HITL approval flow ─────────────────────────────────────────────────────
describe('useChatStream HITL approval flow', () => {
  it('kim:hitl-approval-request sets hitlApprovalStatus with approved: null', async () => {
    const { result } = await renderStream();
    emit('kim:hitl-approval-request', {
      tool: 'run_shell',
      risk: 'high',
      reason: 'arbitrary_code_execution',
      preview: 'rm -rf build/',
    });
    expect(result.current.hitlApprovalStatus).toEqual({
      tool: 'run_shell',
      risk: 'high',
      reason: 'arbitrary_code_execution',
      preview: 'rm -rf build/',
      approved: null,
    });
  });

  it('kim:hitl-approval-result updates approved while preserving the preview for the same tool', async () => {
    const { result } = await renderStream();
    emit('kim:hitl-approval-request', {
      tool: 'run_shell',
      risk: 'medium',
      reason: 'shell',
      preview: 'ls -la',
    });
    emit('kim:hitl-approval-result', { tool: 'run_shell', approved: true });
    expect(result.current.hitlApprovalStatus).toEqual({
      tool: 'run_shell',
      risk: 'medium',
      reason: 'shell',
      preview: 'ls -la',
      approved: true,
    });
  });

  it('kim:hitl-approval-result for a different tool falls back to defaults (no stale preview)', async () => {
    const { result } = await renderStream();
    emit('kim:hitl-approval-request', {
      tool: 'run_shell',
      risk: 'medium',
      reason: 'shell',
      preview: 'ls -la',
    });
    emit('kim:hitl-approval-result', { tool: 'browser_click', approved: false });
    expect(result.current.hitlApprovalStatus).toEqual({
      tool: 'browser_click',
      risk: 'high',
      reason: 'approval_result',
      preview: undefined,
      approved: false,
    });
  });

  it('hitlRespond invokes hitl_respond_approval with derived and explicit decisions', async () => {
    const { result } = await renderStream();
    await act(async () => { await result.current.hitlRespond(true); });
    expect(invokeMock).toHaveBeenCalledWith('hitl_respond_approval', {
      approved: true,
      decision: 'accept',
    });
    await act(async () => { await result.current.hitlRespond(false); });
    expect(invokeMock).toHaveBeenCalledWith('hitl_respond_approval', {
      approved: false,
      decision: 'decline',
    });
    await act(async () => { await result.current.hitlRespond(true, 'acceptForSession'); });
    expect(invokeMock).toHaveBeenCalledWith('hitl_respond_approval', {
      approved: true,
      decision: 'acceptForSession',
    });
  });

  // T1: the correlation id from the pending request must be echoed back on the
  // decision so Python voids a late click on an already-timed-out prompt.
  it('hitlRespond echoes the pending request id back to hitl_respond_approval', async () => {
    const { result } = await renderStream();
    emit('kim:hitl-approval-request', {
      tool: 'run_shell',
      risk: 'high',
      reason: 'arbitrary_code_execution',
      preview: 'rm -rf build/',
      id: 'req-77',
    });
    expect(result.current.hitlApprovalStatus?.id).toBe('req-77');
    await act(async () => { await result.current.hitlRespond(true); });
    expect(invokeMock).toHaveBeenCalledWith('hitl_respond_approval', {
      approved: true,
      decision: 'accept',
      id: 'req-77',
    });
  });
});

// ── 2. Activity dedup ─────────────────────────────────────────────────────────
describe('useChatStream activity dedup', () => {
  it('drops identical raw lines within the 800ms window; distinct lines pass', async () => {
    vi.useFakeTimers();
    const { result } = await renderStream();
    // F-F-2: raw lines only append while THIS view owns the active run.
    act(() => { result.current.setIsRunning(true); });

    emit('kim-agent-output', '[TOOL] read_file({"path":"a.txt"})');
    emit('kim-agent-output', '[TOOL] read_file({"path":"a.txt"})'); // duplicate raw within 800ms
    emit('kim-agent-output', '[TOOL] read_file({"path":"b.txt"})'); // distinct — passes
    act(() => { result.current.flushActivityNow(); });

    expect(result.current.activity.map(a => a.text)).toEqual([
      'Reading `a.txt`',
      'Reading `b.txt`',
    ]);
  });

  it('canonicalizes the [err] prefix so a stderr echo of a stdout line is deduped', async () => {
    vi.useFakeTimers();
    const { result } = await renderStream();
    // F-F-2: raw lines only append while THIS view owns the active run.
    act(() => { result.current.setIsRunning(true); });

    emit('kim-agent-output', '[TOOL] read_file({"path":"a.txt"})');
    // kim-agent-error prepends "[err] " before appendRaw; isDuplicate strips it.
    emit('kim-agent-error', '[TOOL] read_file({"path":"a.txt"})');
    act(() => { result.current.flushActivityNow(); });

    expect(result.current.activity).toHaveLength(1);
  });

  it('after 800ms the raw window expires but the 2000ms activity-item dedup still drops it', async () => {
    vi.useFakeTimers();
    const { result } = await renderStream();
    // F-F-2: raw lines only append while THIS view owns the active run.
    act(() => { result.current.setIsRunning(true); });
    const line = '[TOOL] read_file({"path":"a.txt"})';

    emit('kim-agent-output', line);
    act(() => { vi.advanceTimersByTime(900); }); // raw window (800ms) expired
    emit('kim-agent-output', line); // passes recentRawRef, caught by recentActivityItemRef
    act(() => { result.current.flushActivityNow(); });
    expect(result.current.activity).toHaveLength(1);

    act(() => { vi.advanceTimersByTime(2100); }); // both windows expired
    emit('kim-agent-output', line);
    act(() => { result.current.flushActivityNow(); });
    expect(result.current.activity).toHaveLength(2);
  });

  it('dedups different raw lines that parse to the same activity item (2000ms window)', async () => {
    vi.useFakeTimers();
    const { result } = await renderStream();
    // F-F-2: raw lines only append while THIS view owns the active run.
    act(() => { result.current.setIsRunning(true); });

    // Same parsed item ("Reading `a.txt`") from two different raw strings — the
    // timestamp prefix is stripped by parseLogLine, so only the activity-item
    // dedup can catch the second one.
    emit('kim-agent-output', '[TOOL] read_file({"path":"a.txt"})');
    emit('kim-agent-output', '2026-07-06 10:00:00 [TOOL] read_file({"path":"a.txt"})');
    act(() => { result.current.flushActivityNow(); });

    expect(result.current.activity).toHaveLength(1);
  });

  it('batches activity flushes on a 50ms timer', async () => {
    vi.useFakeTimers();
    const { result } = await renderStream();
    // F-F-2: raw lines only append while THIS view owns the active run.
    act(() => { result.current.setIsRunning(true); });

    emit('kim-agent-output', '[TOOL] read_file({"path":"a.txt"})');
    expect(result.current.activity).toHaveLength(0); // not flushed yet
    act(() => { vi.advanceTimersByTime(50); });
    expect(result.current.activity).toHaveLength(1);
  });
});

// ── 2b. Cross-session raw-stream bleed (F-F-2) ────────────────────────────────
describe('useChatStream raw-stream ownership guard (F-F-2)', () => {
  it('a view that does NOT own the active run ignores un-enveloped raw output/error', async () => {
    vi.useFakeTimers();
    const { result } = await renderStream(); // conv-1, not running, owns no run

    // A run started under a DIFFERENT session keeps streaming un-enveloped raw
    // lines after the user switched to this view. They must not bleed in.
    emit('kim-agent-output', '[TOOL] read_file({"path":"secret.txt"})');
    emit('kim-agent-error', 'boom: something failed in the other run');
    act(() => { result.current.flushActivityNow(); });

    expect(result.current.activity).toHaveLength(0);
    // …and the foreign [err] line must NOT raise a Retry banner for a task
    // this view never ran.
    expect(result.current.taskError).toBeNull();
    expect(result.current.lastFailedTask).toBeNull();
  });

  it('a view that owns the run still receives its raw output', async () => {
    vi.useFakeTimers();
    const { result } = await renderStream();
    act(() => { result.current.setIsRunning(true); }); // this view owns the run

    emit('kim-agent-output', '[TOOL] read_file({"path":"mine.txt"})');
    act(() => { result.current.flushActivityNow(); });

    expect(result.current.activity.map(a => a.text)).toEqual(['Reading `mine.txt`']);
  });
});

// ── 3. Context meter ──────────────────────────────────────────────────────────
describe('useChatStream context meter', () => {
  it('kim:context populates contextState fully and drives contextUsage', async () => {
    const { result } = await renderStream();
    const payload = {
      cumulative_input: 12345,
      budget: 200000,
      phase: 'warn',
      percent: 62,
      last_input: 900,
      last_output: 150,
      source: 'claude',
      estimate: true,
    };
    emit('kim:context', payload);
    expect(result.current.contextState).toEqual(payload);
    expect(result.current.contextUsage).toBe(62);
  });
});

// ── 4. Typed event handling ───────────────────────────────────────────────────
describe('useChatStream typed events', () => {
  it('kim:status sets typedStatus (lastStatus) and appends a status activity item', async () => {
    const { result } = await renderStream();
    emit('kim:status', { message: 'Opening the browser' });
    act(() => { result.current.flushActivityNow(); });
    expect(result.current.lastStatus).toBe('Opening the browser');
    expect(result.current.activity).toEqual([
      expect.objectContaining({ kind: 'status', text: 'Opening the browser' }),
    ]);
  });

  it('kim:plan with >=2 steps sets the live plan; step statuses start pending', async () => {
    const { result } = await renderStream();
    emit('kim:plan', { steps: ['open site', 'fill form', 'submit'] });
    expect(result.current.planSteps).toEqual(['open site', 'fill form', 'submit']);
    const plan = planTrace(result.current.traceItems);
    expect(plan).toBeDefined();
    expect(plan!.items.map(i => i.status)).toEqual(['pending', 'pending', 'pending']);
  });

  it('kim:plan ignores <2 steps, filters non-strings, and caps at 12 steps', async () => {
    const { result } = await renderStream();
    emit('kim:plan', { steps: ['only one'] });
    expect(result.current.planSteps).toEqual([]);

    emit('kim:plan', { steps: ['a', 42, 'b', null] });
    expect(result.current.planSteps).toEqual(['a', 'b']);

    const many = Array.from({ length: 15 }, (_, i) => `step ${i + 1}`);
    emit('kim:plan', { steps: many });
    expect(result.current.planSteps).toHaveLength(12);
  });

  it('kim:step advances the active step; kim:done marks steps done', async () => {
    const { result } = await renderStream();
    emit('kim:plan', { steps: ['one', 'two', 'three'] });

    emit('kim:step', { n: 2, data: {} });
    let plan = planTrace(result.current.traceItems)!;
    expect(plan.items.map(i => i.status)).toEqual(['done', 'active', 'pending']);

    emit('kim:done', { n: 2 });
    plan = planTrace(result.current.traceItems)!;
    expect(plan.items[1].status).toBe('done');
  });

  it('a second PLAN replaces the previous plan and resets progress (F-F-4)', async () => {
    const { result } = await renderStream();
    emit('kim:plan', { steps: ['one', 'two', 'three'] });
    emit('kim:step', { n: 2, data: {} });
    emit('kim:done', { n: 1 });
    expect(planTrace(result.current.traceItems)!.items.map(i => i.status)).toEqual(['done', 'active', 'pending']);

    // Re-plan with new steps: the stale card must NOT persist — the new plan
    // starts fresh with all steps pending.
    emit('kim:plan', { steps: ['alpha', 'beta'] });
    const plan = planTrace(result.current.traceItems)!;
    expect(result.current.planSteps).toEqual(['alpha', 'beta']);
    expect(plan.items.map(i => i.status)).toEqual(['pending', 'pending']);
  });

  it('a <2-step re-PLAN clears the previous plan instead of leaving a stale card (F-F-4)', async () => {
    const { result } = await renderStream();
    emit('kim:plan', { steps: ['one', 'two', 'three'] });
    expect(result.current.planSteps).toHaveLength(3);

    emit('kim:plan', { steps: ['just one'] }); // too few to render
    expect(result.current.planSteps).toEqual([]);
    expect(planTrace(result.current.traceItems)).toBeUndefined();
  });

  it('STEP/DONE indices beyond steps.length are rejected, not clamped (F-F-4)', async () => {
    const { result } = await renderStream();
    emit('kim:plan', { steps: ['one', 'two', 'three'] });

    // n=9 (> 3 steps): must be ignored, not clamped to the last step.
    emit('kim:step', { n: 9, data: {} });
    expect(planTrace(result.current.traceItems)!.items.map(i => i.status)).toEqual(['pending', 'pending', 'pending']);

    // DONE n=9 must not mark any step complete.
    emit('kim:done', { n: 9 });
    expect(planTrace(result.current.traceItems)!.items.every(i => i.status !== 'done')).toBe(true);

    // n=0 (below 1-based range) is likewise ignored.
    emit('kim:step', { n: 0, data: {} });
    expect(planTrace(result.current.traceItems)!.items.map(i => i.status)).toEqual(['pending', 'pending', 'pending']);
  });

  it('kim:stats sets tokenStats', async () => {
    const { result } = await renderStream();
    emit('kim:stats', { input: 100, output: 40, total: 140 });
    expect(result.current.tokenStats).toEqual({ input: 100, output: 40, total: 140 });
  });

  it('kim:tool appends a tool activity item (mapped and unmapped names)', async () => {
    const { result } = await renderStream();
    emit('kim:tool', { name: 'read_file', args: { path: '/tmp/a.txt' } });
    emit('kim:tool', { name: 'totally_unknown_tool', args: {} });
    act(() => { result.current.flushActivityNow(); });
    expect(result.current.activity).toEqual([
      expect.objectContaining({ kind: 'tool', text: 'Reading `a.txt`' }),
      expect.objectContaining({ kind: 'tool', text: 'Using tool: `totally_unknown_tool`' }),
    ]);
  });

  it('kim:answer appends to liveHistory, skips blanks and consecutive duplicates', async () => {
    const { result } = await renderStream();
    emit('kim:answer', { text: '  ' }); // blank — ignored
    emit('kim:answer', { text: 'Here is the result' });
    emit('kim:answer', { text: 'Here is the result' }); // consecutive dup — ignored
    emit('kim:answer', { text: 'Second answer' });
    expect(result.current.liveHistory).toEqual([
      { role: 'assistant', content: 'Here is the result' },
      { role: 'assistant', content: 'Second answer' },
    ]);
  });

  // M7: streamed assistant deltas accumulate into one bubble, flushed on the
  // 50ms timer, then replaced by the final kim:answer (no duplicate).
  it('kim:assistant-delta streams into one bubble that kim:answer replaces', async () => {
    vi.useFakeTimers();
    const { result } = await renderStream();
    emit('kim:assistant-delta', { chunk: 'Hel' });
    emit('kim:assistant-delta', { chunk: 'lo wor' });
    emit('kim:assistant-delta', { chunk: 'ld' });
    act(() => { vi.advanceTimersByTime(60); }); // flush the assistant-delta timer
    expect(result.current.liveHistory).toEqual([
      { role: 'assistant', content: 'Hello world' },
    ]);
    // Final answer replaces the streaming bubble rather than appending a dup.
    emit('kim:answer', { text: 'Hello world!' });
    expect(result.current.liveHistory).toEqual([
      { role: 'assistant', content: 'Hello world!' },
    ]);
  });

  // M7: reasoning deltas surface completed lines into the activity feed.
  it('kim:reasoning-delta surfaces completed lines into the activity feed', async () => {
    vi.useFakeTimers();
    const { result } = await renderStream();
    emit('kim:reasoning-delta', { chunk: 'first thought\nsecond th' });
    emit('kim:reasoning-delta', { chunk: 'ought\n' });
    act(() => { vi.advanceTimersByTime(60); }); // flush the activity timer
    const texts = result.current.activity.map(a => a.text);
    expect(texts).toContain('first thought');
    expect(texts).toContain('second thought');
  });

  // M8: a [SUCCESS] activity with no prior answer becomes an assistant bubble
  // (raw-path parity — success-without-answer must render something).
  it('kim:activity success with no answer creates an assistant bubble', async () => {
    const { result } = await renderStream();
    emit('kim:activity', { kind: 'success', text: 'Booked the flight for Tuesday.' });
    expect(result.current.liveHistory).toEqual([
      { role: 'assistant', content: 'Booked the flight for Tuesday.' },
    ]);
  });

  it('kim:activity generic success does NOT create a bubble', async () => {
    const { result } = await renderStream();
    emit('kim:activity', { kind: 'success', text: 'Task completed' });
    expect(result.current.liveHistory).toEqual([]);
  });

  it('kim:activity success is suppressed as a bubble when an answer already arrived', async () => {
    const { result } = await renderStream();
    emit('kim:answer', { text: 'The real answer' });
    emit('kim:activity', { kind: 'success', text: 'A summary line' });
    expect(result.current.liveHistory).toEqual([
      { role: 'assistant', content: 'The real answer' },
    ]);
  });
});

// ── 5. Lifecycle ──────────────────────────────────────────────────────────────
describe('useChatStream lifecycle', () => {
  it('kim-agent-done (success) finalizes the run: stops, records duration, persists, notifies', async () => {
    vi.useFakeTimers();
    const { result, props } = await renderStream();

    act(() => { result.current.setIsRunning(true); });
    emit('kim:status', { message: 'working' }); // some activity so the run is persisted
    emit('kim:hitl-approval-request', { tool: 't', risk: 'high', reason: 'r', preview: '' });
    act(() => { vi.advanceTimersByTime(3000); });

    emit('kim-agent-done', true);

    expect(result.current.isRunning).toBe(false);
    expect(result.current.isDone).toBe(true);
    expect(result.current.cancelling).toBe(false);
    expect(result.current.hitlApprovalStatus).toBeNull(); // B7: no dead approval card
    expect(result.current.runHistory).toHaveLength(1);
    expect(result.current.runHistory[0].durationSec).toBe(3);
    expect(result.current.activity).toEqual([]); // cleared for the next run
    expect(invokeMock).toHaveBeenCalledWith(
      'save_run_history',
      expect.objectContaining({ sessionId: 'conv-1' }),
    );
    expect(props.onTaskDone).toHaveBeenCalledWith('conv-1', undefined);
    expect(props.commitCurrentBrowserUrl).toHaveBeenCalled();
    expect(props.setMessageReloadNonce).toHaveBeenCalled();
    expect(result.current.taskError).toBeNull();
    expect(result.current.lastFailedTask).toBeNull();
  });

  it('toasts when save_run_history rejects instead of silently losing the run (F-F-10)', async () => {
    vi.useFakeTimers();
    invokeMock.mockImplementation((name: string) =>
      name === 'save_run_history'
        ? Promise.reject(new Error('disk full'))
        : Promise.resolve(undefined),
    );
    const { result } = await renderStream();

    act(() => { result.current.setIsRunning(true); });
    emit('kim:status', { message: 'working' }); // activity so the run is persisted
    act(() => { vi.advanceTimersByTime(1000); });

    emit('kim-agent-done', true);
    // Let the rejected save_run_history promise settle.
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(invokeMock).toHaveBeenCalledWith('save_run_history', expect.objectContaining({ sessionId: 'conv-1' }));
    const surfaced = toastMock.mock.calls.some(
      ([msg, kind]) => typeof msg === 'string' && /history/i.test(msg) && kind === 'error',
    );
    expect(surfaced).toBe(true);
  });

  it('kim-agent-done (failure) sets taskError and strips the failed run assistant output', async () => {
    const { result } = await renderStream();
    // RUN-IDENTITY: this view owns the in-flight run (as runPendingTask would set).
    act(() => { result.current.setIsRunning(true); });
    act(() => {
      result.current.setLiveHistory([
        { role: 'assistant', content: 'old good answer' },
        { role: 'user', content: 'do the thing' },
        { role: 'assistant', content: 'partial output from failed run' },
      ]);
    });

    emit('kim-agent-done', false);

    expect(result.current.taskError).toBe('agent-error');
    expect(result.current.liveHistory).toEqual([
      { role: 'assistant', content: 'old good answer' },
      { role: 'user', content: 'do the thing' },
    ]);
    expect(result.current.runHistory).toHaveLength(0); // failed runs are not persisted
  });

  it('failure taskError prefers the typed provider-error code over the generic message', async () => {
    const { result } = await renderStream();
    act(() => { result.current.setIsRunning(true); }); // RUN-IDENTITY: view owns the run
    emit('kim:provider-error', { code: 'auth', retryable: false });
    emit('kim-agent-done', false);
    expect(result.current.taskError).toBe(
      'Provider authentication failed. Check the selected provider sign-in or API key.',
    );
  });

  it('failure taskError falls back to the kim:run-done termination reason', async () => {
    const { result } = await renderStream();
    act(() => { result.current.setIsRunning(true); }); // RUN-IDENTITY: view owns the run
    emit('kim:run-done', { termination: 'max_iterations', success: false });
    emit('kim-agent-done', false);
    expect(result.current.taskError).toMatch(/maximum iteration limit/);
  });

  it('kim-agent-cancelled sets cancel flags and appends the cancel activity item', async () => {
    const { result } = await renderStream();
    act(() => { result.current.setIsRunning(true); });

    emit('kim-agent-cancelled', true);
    act(() => { result.current.flushActivityNow(); });

    expect(result.current.isCancelled).toBe(true);
    expect(result.current.cancelFlagRef.current).toBe(true);
    expect(result.current.isRunning).toBe(false);
    expect(result.current.cancelling).toBe(false);
    expect(result.current.hitlApprovalStatus).toBeNull();
    expect(result.current.activity).toEqual([
      expect.objectContaining({ kind: 'cancelled', text: 'Task stopped' }),
    ]);
  });

  it('kim:run-failed populates runFailure and kim-run-id captures the checkpoint id', async () => {
    const { result } = await renderStream();
    emit('kim:run-failed', { reason: 'provider_auth', recoverable: true, suggestion: 'Sign in again' });
    // RUN-IDENTITY: kim-run-id now carries { run_id, session_id }; session_id
    // must match this view (conv-1) for the run to be adopted.
    emit('kim-run-id', { run_id: 'run-abc-123', session_id: 'conv-1' });
    expect(result.current.runFailure).toEqual({
      reason: 'provider_auth',
      recoverable: true,
      suggestion: 'Sign in again',
    });
    expect(result.current.lastRunId).toBe('run-abc-123');
  });

  it('kim:run-failed is terminal: clears the spinner so the recovery banner shows (F-F-5)', async () => {
    const { result } = await renderStream();
    act(() => { result.current.setIsRunning(true); }); // this view owns a running run
    expect(result.current.isRunning).toBe(true);

    // Backend signals failure but (simulating a subprocess kill / bridge crash)
    // never emits the terminal bare-bool kim-agent-done.
    emit('kim:run-failed', {
      reason: 'provider_failed',
      recoverable: true,
      suggestion: 'Retry',
      session_id: 'conv-1',
    });

    // Spinner + Stop must clear (no infinite "thinking…"), and isDone flips true
    // so StreamRenderer stops returning null for the failure card.
    expect(result.current.isRunning).toBe(false);
    expect(result.current.isDone).toBe(true);
    expect(result.current.cancelling).toBe(false);
    expect(result.current.runFailure).toMatchObject({ reason: 'provider_failed', recoverable: true });
  });
});

// ── 5b. Typed enveloped lifecycle CLEAR events (F-H-1) ────────────────────────
describe('useChatStream typed lifecycle CLEAR events (F-H-1)', () => {
  it('typed kim:agent-error clears the spinner and surfaces the error attributably', async () => {
    const { result } = await renderStream();
    act(() => { result.current.setIsRunning(true); });

    emit('kim:agent-error', { error: 'orchestrator process crashed', session_id: 'conv-1' });

    expect(result.current.isRunning).toBe(false);
    expect(result.current.taskError).toBeTruthy();
  });

  it('typed kim:agent-done clears isRunning attributably (safety net for a missing bare done)', async () => {
    const { result } = await renderStream();
    act(() => { result.current.setIsRunning(true); });

    emit('kim:agent-done', { success: true, session_id: 'conv-1' });

    expect(result.current.isRunning).toBe(false);
    expect(result.current.cancelling).toBe(false);
  });

  it('typed kim:agent-cancelled sets cancel flags attributably', async () => {
    const { result } = await renderStream();
    act(() => { result.current.setIsRunning(true); });

    emit('kim:agent-cancelled', { success: false, session_id: 'conv-1' });

    expect(result.current.isRunning).toBe(false);
    expect(result.current.isCancelled).toBe(true);
  });

  it('a foreign-session typed lifecycle event does NOT clear this view', async () => {
    const { result } = await renderStream();
    act(() => { result.current.setIsRunning(true); });

    // A run owned by a different session terminates while this view is mounted.
    emit('kim:agent-error', { error: 'the other run failed', session_id: 'a-different-session' });

    expect(result.current.isRunning).toBe(true); // untouched — not our run
    expect(result.current.taskError).toBeNull();
  });
});

// ── 6. Rate limit ─────────────────────────────────────────────────────────────
describe('useChatStream rate limit', () => {
  it('kim:rate-limited sets state and auto-clears after (delay + 1) seconds', async () => {
    vi.useFakeTimers();
    const { result } = await renderStream();

    emit('kim:rate-limited', { delay: 2, attempt: 1, max_retries: 3 });
    expect(result.current.rateLimitedState).toEqual({ delay: 2, attempt: 1, max_retries: 3 });

    act(() => { vi.advanceTimersByTime(2999); });
    expect(result.current.rateLimitedState).not.toBeNull();

    act(() => { vi.advanceTimersByTime(1); }); // (2 + 1) * 1000 = 3000ms
    expect(result.current.rateLimitedState).toBeNull();
  });

  it('a second rate-limit event cancels the previous auto-clear timer (B9)', async () => {
    vi.useFakeTimers();
    const { result } = await renderStream();

    emit('kim:rate-limited', { delay: 2, attempt: 1, max_retries: 3 });
    act(() => { vi.advanceTimersByTime(1000); });
    emit('kim:rate-limited', { delay: 5, attempt: 2, max_retries: 3 });

    // 4000ms in: the first timer (3000ms) would have fired if not cancelled.
    act(() => { vi.advanceTimersByTime(3000); });
    expect(result.current.rateLimitedState).toEqual({ delay: 5, attempt: 2, max_retries: 3 });

    // Second timer fires at 1000 + 6000 = 7000ms.
    act(() => { vi.advanceTimersByTime(3000); });
    expect(result.current.rateLimitedState).toBeNull();
  });
});

// ── RUN-IDENTITY (D1/B4/B5): route/file by run, not by current view ──────────
describe('useChatStream run identity', () => {
  it('drops run-scoped events tagged for a DIFFERENT session (run A never mutates view B)', async () => {
    const { result } = await renderStream(); // this view = conv-1
    // A run started under another session is still streaming after a switch.
    emit('kim:answer', { text: 'foreign answer', session_id: 'other-session' });
    emit('kim:status', { message: 'foreign status', session_id: 'other-session' });
    act(() => { result.current.flushActivityNow(); });
    expect(result.current.liveHistory).toEqual([]);
    expect(result.current.lastStatus).toBe('');
  });

  it('accepts events for THIS view session and legacy events with no envelope', async () => {
    const { result } = await renderStream(); // conv-1
    emit('kim:answer', { text: 'mine', session_id: 'conv-1' });
    emit('kim:answer', { text: 'legacy no-session' }); // undefined session_id -> allowed
    expect(result.current.liveHistory).toEqual([
      { role: 'assistant', content: 'mine' },
      { role: 'assistant', content: 'legacy no-session' },
    ]);
  });

  it('files run history under the run OWNER, not the on-screen session', async () => {
    vi.useFakeTimers();
    const props = makeProps();
    const { result } = renderHook(() => useChatStream(props));
    await act(async () => {});
    act(() => {
      result.current.setIsRunning(true);
      // The run belongs to a different session than the one on screen (conv-1).
      result.current.runOwnerSessionIdRef.current = 'owner-session-9';
    });
    emit('kim:status', { message: 'working' }); // legacy no-session -> counted as this run's
    act(() => { vi.advanceTimersByTime(1000); });
    emit('kim-agent-done', true);
    expect(invokeMock).toHaveBeenCalledWith(
      'save_run_history',
      expect.objectContaining({ sessionId: 'owner-session-9' }),
    );
    expect(props.onTaskDone).toHaveBeenCalledWith('owner-session-9', undefined);
  });

  it('a view that never owned the run ignores a foreign kim-agent-done (no error banner)', async () => {
    const { result } = await renderStream(); // fresh view, never started a run
    emit('kim-agent-done', false); // a foreign run failed while this view is open
    expect(result.current.taskError).toBeNull();
    expect(result.current.lastFailedTask).toBeNull();
    expect(result.current.runHistory).toHaveLength(0);
  });

  it('re-derives isRunning=true when remounted for the active run session (switch-back)', async () => {
    const props = { ...makeProps(), activeRunSessionId: 'conv-1', activeRunId: 'conv-1-123' };
    const { result } = renderHook(() => useChatStream(props));
    await act(async () => {});
    expect(result.current.isRunning).toBe(true);
    expect(result.current.runOwnerSessionIdRef.current).toBe('conv-1');
  });

  it('does NOT claim the run when the active run belongs to another session', async () => {
    const props = { ...makeProps(), activeRunSessionId: 'other', activeRunId: 'other-1' };
    const { result } = renderHook(() => useChatStream(props));
    await act(async () => {});
    expect(result.current.isRunning).toBe(false);
    expect(result.current.runOwnerSessionIdRef.current).toBeNull();
  });
});

// ── V-audit #6: liveHistory size cap (mirrors the 300-item activity cap) ──────
describe('useChatStream liveHistory cap', () => {
  it('caps liveHistory at 300 entries, dropping the oldest first', async () => {
    const { result } = await renderStream();
    act(() => {
      for (let i = 0; i < 305; i++) {
        result.current.setLiveHistory(prev => [...prev, { role: 'user', content: `msg ${i}` }]);
      }
    });
    expect(result.current.liveHistory).toHaveLength(300);
    // The 5 oldest (msg 0..4) were trimmed; the newest (msg 304) survives.
    expect(result.current.liveHistory[0].content).toBe('msg 5');
    expect(result.current.liveHistory[299].content).toBe('msg 304');
  });

  it('a direct (non-updater) setLiveHistory call is also capped', async () => {
    const { result } = await renderStream();
    const many = Array.from({ length: 320 }, (_, i) => ({ role: 'user' as const, content: `m${i}` }));
    act(() => { result.current.setLiveHistory(many); });
    expect(result.current.liveHistory).toHaveLength(300);
    expect(result.current.liveHistory[0].content).toBe('m20');
  });
});

// ── V-audit #4: a run that outlives its view still gets persisted ────────────
describe('useChatStream orphaned-run persistence (V-audit #4)', () => {
  it('a view that unmounts mid-run parks a snapshot that a LATER view flushes on kim-agent-done', async () => {
    // View A owns an in-flight run for its session (conv-1).
    const propsA = makeProps(); // conversationId: 'conv-1'
    const viewA = renderHook(() => useChatStream(propsA));
    await act(async () => {});
    act(() => { viewA.result.current.setIsRunning(true); });
    emit('kim:status', { message: 'working' }); // accumulate activity for the run
    act(() => { viewA.result.current.flushActivityNow(); });
    expect(viewA.result.current.activity.length).toBeGreaterThan(0);

    // User switches away (New Chat) — view A unmounts mid-run. In the real
    // app this destroys A's hook instance entirely; the run is still going
    // in the backend.
    viewA.unmount();
    expect(__testOnlyPendingRunSnapshots.has('conv-1')).toBe(true);

    // A different view (conv-2) is now mounted — e.g. the user started a new
    // chat, or switched to another existing session. It never touched conv-1.
    const propsB = { ...makeProps(), conversationId: 'conv-2' };
    const viewB = renderHook(() => useChatStream(propsB));
    await act(async () => {});

    // The backend's run for conv-1 finishes. Only view B is mounted, so only
    // its listener fires — but the orphaned conv-1 snapshot must still reach
    // save_run_history instead of being silently dropped.
    emit('kim-agent-done', true);

    expect(invokeMock).toHaveBeenCalledWith(
      'save_run_history',
      expect.objectContaining({ sessionId: 'conv-1' }),
    );
    // View B itself never owned a run — its own state must stay untouched
    // (no error banner, no history entry filed under conv-2).
    expect(viewB.result.current.taskError).toBeNull();
    expect(viewB.result.current.runHistory).toHaveLength(0);
    expect(__testOnlyPendingRunSnapshots.size).toBe(0);
  });

  it('does NOT double-save when the SAME session remounts and finishes normally (no race)', async () => {
    const propsA = makeProps(); // conv-1
    const viewA = renderHook(() => useChatStream(propsA));
    await act(async () => {});
    act(() => { viewA.result.current.setIsRunning(true); });
    emit('kim:status', { message: 'working' });
    act(() => { viewA.result.current.flushActivityNow(); });
    viewA.unmount();
    expect(__testOnlyPendingRunSnapshots.has('conv-1')).toBe(true);

    // Same session remounts before the run finishes (switch-back / re-attach).
    const propsA2 = { ...makeProps(), activeRunSessionId: 'conv-1', activeRunId: 'run-1' };
    const viewA2 = renderHook(() => useChatStream(propsA2));
    await act(async () => {});
    expect(viewA2.result.current.isRunning).toBe(true); // re-derived via RUN-IDENTITY

    // The re-attached view starts receiving this session's events again (the
    // ones that happened while unmounted are gone, but new ones flow fine).
    emit('kim:status', { message: 'still working' });
    act(() => { viewA2.result.current.flushActivityNow(); });

    emit('kim-agent-done', true);

    // Exactly one save_run_history call for conv-1 (the normal ownsRun path),
    // not two (which would race against the stale orphan snapshot).
    const conv1Saves = invokeMock.mock.calls.filter(
      ([name, args]) => name === 'save_run_history' && (args as { sessionId?: string })?.sessionId === 'conv-1'
    );
    expect(conv1Saves).toHaveLength(1);
    expect(__testOnlyPendingRunSnapshots.size).toBe(0);
  });
});

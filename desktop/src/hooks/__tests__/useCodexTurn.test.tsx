import { renderHook, act } from '@testing-library/react';
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { useCodexTurn } from '../useCodexTurn';
import { ApprovalCard, CodexTurnPanel, diffSummary } from '../../components/chat/CodexTurnPanel';

// ── Tauri mocks (same pattern as useChatStream.test) ─────────────────────────
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

import { invoke } from '@tauri-apps/api/core';
const invokeMock = invoke as unknown as ReturnType<typeof vi.fn>;

function emit(event: string, payload?: unknown) {
  act(() => {
    for (const h of [...(listeners.get(event) ?? [])]) h({ payload });
  });
}

async function renderTurn() {
  const utils = renderHook(() => useCodexTurn());
  await act(async () => {});
  return utils;
}

beforeEach(() => {
  listeners.clear();
  invokeMock.mockClear();
  invokeMock.mockResolvedValue(undefined);
});

describe('useCodexTurn — native approvals', () => {
  it('command approval request populates pending state', async () => {
    const { result } = await renderTurn();
    emit('kim:command-approval-request', {
      id: '5',
      command: 'npx playwright install',
      cwd: '/proj',
      reason: 'needs browsers',
      risk: 'network',
      network: true,
      amendment: [],
    });
    expect(result.current.approval).toMatchObject({
      id: '5',
      kind: 'command',
      command: 'npx playwright install',
      cwd: '/proj',
      network: true,
      resolved: null,
    });
  });

  it('respond() invokes respond_approval_decision and marks resolved', async () => {
    const { result } = await renderTurn();
    emit('kim:command-approval-request', {
      id: '9', command: 'touch x', cwd: '/p', reason: '', risk: '', network: false, amendment: [],
    });
    act(() => result.current.respond('acceptForSession'));
    expect(invokeMock).toHaveBeenCalledWith('respond_approval_decision', {
      id: '9',
      decision: 'acceptForSession',
    });
    expect(result.current.approval?.resolved).toBe('acceptForSession');
    // Double-click cannot double-send.
    act(() => result.current.respond('decline'));
    expect(invokeMock).toHaveBeenCalledTimes(1);
  });

  it('file-change approval summarizes files', async () => {
    const { result } = await renderTurn();
    emit('kim:file-change-approval-request', {
      id: '2',
      files: [{ path: 'a.rs', kind: 'edit' }, { path: 'b.rs', kind: 'add' }],
      reason: 'patch',
    });
    expect(result.current.approval).toMatchObject({
      kind: 'fileChange',
      command: 'a.rs, b.rs',
      reason: 'patch',
    });
  });
});

describe('useCodexTurn — user input (C4)', () => {
  it('kim:user-input-request populates the question state', async () => {
    const { result } = await renderTurn();
    emit('kim:user-input-request', {
      id: 'req-1', kind: 'questions', item_id: 'it-1',
      questions: [{ id: 'q1', header: 'DB', question: 'Which database?', options: [{ label: 'Postgres' }] }],
      message: '',
    });
    expect(result.current.userInput).toMatchObject({
      id: 'req-1', kind: 'questions', resolved: false,
    });
    expect(result.current.userInput?.questions[0]).toMatchObject({ id: 'q1', question: 'Which database?' });
  });

  it('respondUserInput invokes respond_user_input verbatim and marks resolved', async () => {
    const { result } = await renderTurn();
    emit('kim:user-input-request', {
      id: 'req-9', kind: 'questions', item_id: '', questions: [], message: '',
    });
    act(() => result.current.respondUserInput({ q1: { answers: ['Postgres'] } }));
    expect(invokeMock).toHaveBeenCalledWith('respond_user_input', {
      id: 'req-9', answers: { q1: { answers: ['Postgres'] } },
    });
    expect(result.current.userInput?.resolved).toBe(true);
    // A second answer cannot double-send.
    act(() => result.current.respondUserInput({ q1: { answers: ['x'] } }));
    expect(invokeMock).toHaveBeenCalledTimes(1);
  });

  it('an elicitation is dismissed locally without invoking the backend', async () => {
    const { result } = await renderTurn();
    emit('kim:user-input-request', {
      id: 'e1', kind: 'elicitation', item_id: '', questions: [], message: 'form',
    });
    act(() => result.current.respondUserInput({}));
    expect(invokeMock).not.toHaveBeenCalledWith('respond_user_input', expect.anything());
    expect(result.current.userInput?.resolved).toBe(true);
  });

  it('a terminal turn phase clears an unanswered question', async () => {
    const { result } = await renderTurn();
    emit('kim:user-input-request', {
      id: 'req-2', kind: 'questions', item_id: '', questions: [], message: '',
    });
    emit('kim:turn-lifecycle', { phase: 'completed', turn_id: 't1' });
    expect(result.current.userInput).toBeNull();
  });
});

describe('useCodexTurn — streams', () => {
  it('accumulates command output and caps it', async () => {
    const { result } = await renderTurn();
    emit('kim:command-output', { item_id: 'i1', chunk: 'line one\n' });
    emit('kim:command-output', { item_id: 'i1', chunk: 'line two\n' });
    expect(result.current.commandOutput).toBe('line one\nline two\n');
    emit('kim:command-output', { item_id: 'i1', chunk: 'x'.repeat(30_000) });
    expect(result.current.commandOutput.length).toBeLessThanOrEqual(20_000);
  });

  it('plan, diff and token usage map through', async () => {
    const { result } = await renderTurn();
    emit('kim:plan-update', {
      steps: [{ step: 'write pong.html', status: 'inProgress' }, { step: 'open it', status: 'pending' }],
    });
    emit('kim:diff-update', { unified_diff: '+++ b/pong.html\n+<html>' });
    emit('kim:token-usage', { input: 100, output: 20, total: 120 });
    expect(result.current.plan).toEqual([
      { step: 'write pong.html', status: 'inProgress' },
      { step: 'open it', status: 'pending' },
    ]);
    expect(result.current.diff).toContain('pong.html');
    expect(result.current.tokenUsage).toEqual({ input: 100, output: 20, total: 120 });
  });

  // M5: when respond_approval_decision rejects, the card must re-open (drop the
  // optimistic resolved) so the user can retry — otherwise the turn hangs.
  it('respond() re-opens the card when the invoke rejects', async () => {
    const { result } = await renderTurn();
    emit('kim:command-approval-request', {
      id: '7', command: 'rm x', cwd: '/p', reason: '', risk: '', network: false, amendment: [],
    });
    invokeMock.mockRejectedValueOnce(new Error('dead run'));
    await act(async () => {
      result.current.respond('accept');
      await Promise.resolve();
    });
    expect(result.current.approval?.resolved).toBeNull();
    // Retry now works because the resolved-guard is cleared.
    invokeMock.mockResolvedValueOnce(undefined);
    act(() => result.current.respond('decline'));
    expect(result.current.approval?.resolved).toBe('decline');
  });

  // M6: a terminal turn phase clears a still-pending approval (dead approval id).
  it('a terminal turn phase clears a pending approval card', async () => {
    const { result } = await renderTurn();
    emit('kim:command-approval-request', {
      id: '3', command: 'c', cwd: '', reason: '', risk: '', network: false, amendment: [],
    });
    expect(result.current.approval?.resolved).toBeNull();
    emit('kim:turn-lifecycle', { phase: 'failed', turn_id: 't1' });
    expect(result.current.approval).toBeNull();
  });

  // M6: kim-agent-done (legacy run end) also clears a pending approval.
  it('kim-agent-done clears a pending approval card', async () => {
    const { result } = await renderTurn();
    emit('kim:command-approval-request', {
      id: '4', command: 'c', cwd: '', reason: '', risk: '', network: false, amendment: [],
    });
    emit('kim-agent-done', true);
    expect(result.current.approval).toBeNull();
  });

  // M6: a RESOLVED card survives a terminal phase (kept as a record).
  it('a resolved approval card survives a terminal phase', async () => {
    const { result } = await renderTurn();
    emit('kim:command-approval-request', {
      id: '8', command: 'c', cwd: '', reason: '', risk: '', network: false, amendment: [],
    });
    act(() => result.current.respond('accept'));
    emit('kim:turn-lifecycle', { phase: 'completed', turn_id: 't1' });
    expect(result.current.approval?.resolved).toBe('accept');
  });

  it('a new turn clears the previous turn state', async () => {
    const { result } = await renderTurn();
    emit('kim:plan-update', { steps: [{ step: 's', status: 'pending' }] });
    emit('kim:command-output', { item_id: 'i', chunk: 'out' });
    emit('kim:command-approval-request', {
      id: '1', command: 'c', cwd: '', reason: '', risk: '', network: false, amendment: [],
    });
    emit('kim:turn-lifecycle', { phase: 'started', turn_id: 't2' });
    expect(result.current.plan).toEqual([]);
    expect(result.current.commandOutput).toBe('');
    expect(result.current.approval).toBeNull();
    expect(result.current.turnPhase).toBe('started');
  });
});

describe('CodexTurnPanel components', () => {
  it('ApprovalCard renders three decision buttons and fires decisions', () => {
    const onDecision = vi.fn();
    render(
      <ApprovalCard
        approval={{
          id: '5', kind: 'command', command: 'npx playwright install', cwd: '/proj',
          reason: 'needs browsers', network: true, files: [], resolved: null,
        }}
        onDecision={onDecision}
      />
    );
    expect(screen.getByText(/npx playwright install/)).toBeTruthy();
    expect(screen.getByText(/network access/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Always allow for this task' }));
    expect(onDecision).toHaveBeenCalledWith('acceptForSession');
    fireEvent.click(screen.getByRole('button', { name: 'Deny' }));
    expect(onDecision).toHaveBeenCalledWith('decline');
  });

  it('resolved ApprovalCard hides the buttons', () => {
    render(
      <ApprovalCard
        approval={{
          id: '5', kind: 'command', command: 'touch x', cwd: '', reason: '',
          network: false, files: [], resolved: 'accept',
        }}
        onDecision={() => undefined}
      />
    );
    expect(screen.queryByRole('button')).toBeNull();
    expect(screen.getByText('Approved once')).toBeTruthy();
  });

  it('CodexTurnPanel renders plan, output and diff together', () => {
    render(
      <CodexTurnPanel
        turn={{
          approval: null,
          plan: [{ step: 'write file', status: 'completed' }],
          commandOutput: 'hello output',
          diff: '+++ b/a.txt\n+x',
          tokenUsage: null,
          turnPhase: 'started',
          respond: () => undefined,
          userInput: null,
          respondUserInput: () => undefined,
          dismissUserInput: () => undefined,
        }}
      />
    );
    expect(screen.getByTestId('codex-plan').textContent).toContain('write file');
    expect(screen.getByTestId('codex-command-output').textContent).toContain('hello output');
    expect(screen.getByTestId('codex-diff').textContent).toContain('diff: 1 file(s), +1 −0');
  });

  it('CodexTurnPanel renders nothing when the turn has no content', () => {
    const { container } = render(
      <CodexTurnPanel
        turn={{
          approval: null, plan: [], commandOutput: '', diff: '',
          tokenUsage: null, turnPhase: null, respond: () => undefined,
          userInput: null, respondUserInput: () => undefined, dismissUserInput: () => undefined,
        }}
      />
    );
    expect(container.innerHTML).toBe('');
  });

  it('diffSummary counts files and lines', () => {
    const diff = '--- a/x\n+++ b/x\n+one\n+two\n-gone\n--- a/y\n+++ b/y\n+three\n';
    expect(diffSummary(diff)).toBe('diff: 2 file(s), +3 −1');
  });
});

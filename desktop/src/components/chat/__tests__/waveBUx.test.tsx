import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { useRef, useState } from 'react';
import { matchSlashCommands, ChatComposer } from '../ChatComposer';
import { ContextMeter } from '../ContextMeter';
import { QuestionCard } from '../CodexTurnPanel';
import type { PendingUserInput } from '../../../hooks/useCodexTurn';

vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn(() => Promise.resolve('')) }));
vi.mock('../../Toast', () => ({ toast: vi.fn() }));
// ProviderPicker pulls tauri listeners we don't care about here.
vi.mock('../../ProviderPicker', () => ({ ProviderPicker: () => null }));

// ── matchSlashCommands (audit B1/6.1) ─────────────────────────────────────────
describe('matchSlashCommands', () => {
  it('matches /compact from a leading-slash prefix', () => {
    expect(matchSlashCommands('/com').map(c => c.name)).toEqual(['/compact']);
    expect(matchSlashCommands('/').map(c => c.name)).toContain('/compact');
    expect(matchSlashCommands('/COMPACT').map(c => c.name)).toEqual(['/compact']);
  });
  it('returns nothing once a space is typed (no longer a command name)', () => {
    expect(matchSlashCommands('/compact now')).toEqual([]);
  });
  it('returns nothing for plain text', () => {
    expect(matchSlashCommands('hello')).toEqual([]);
    expect(matchSlashCommands('')).toEqual([]);
  });
});

// ── Slash menu in the composer ────────────────────────────────────────────────
const composerSettings = {
  project_root: '/x',
  ollama: { mode: 'local', base_url: '', local_model: '', cloud_model: '', context_limit_override: null },
} as unknown as React.ComponentProps<typeof ChatComposer>['settings'];

function ComposerHarness({ onSubmit }: { onSubmit: (t: string) => void }) {
  const [taskInput, setTaskInput] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  return (
    <ChatComposer
      textareaRef={textareaRef}
      taskInput={taskInput}
      setTaskInput={setTaskInput}
      isRunning={false}
      cancelling={false}
      handleCancel={() => {}}
      onSubmit={onSubmit}
      activeTab="chat"
      settings={composerSettings}
      resolveProvider={() => 'claude'}
      handleProviderChange={() => {}}
      handleOllamaModeChange={() => {}}
      handleOllamaModelChange={() => {}}
    />
  );
}

describe('ChatComposer slash menu', () => {
  it('shows the menu when typing "/" and submits the command on click', () => {
    const onSubmit = vi.fn();
    render(<ComposerHarness onSubmit={onSubmit} />);
    const ta = screen.getByRole('textbox');
    fireEvent.change(ta, { target: { value: '/com' } });
    expect(screen.getByTestId('slash-menu')).toBeTruthy();
    expect(screen.getByText('/compact')).toBeTruthy();
    // mouseDown (not click) so the textarea doesn't blur first.
    fireEvent.mouseDown(screen.getByText('/compact'));
    expect(onSubmit).toHaveBeenCalledWith('/compact');
  });

  it('Enter selects the highlighted command instead of submitting raw text', () => {
    const onSubmit = vi.fn();
    render(<ComposerHarness onSubmit={onSubmit} />);
    const ta = screen.getByRole('textbox');
    fireEvent.change(ta, { target: { value: '/compact' } });
    fireEvent.keyDown(ta, { key: 'Enter' });
    expect(onSubmit).toHaveBeenCalledWith('/compact');
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it('does not show the menu for plain text', () => {
    render(<ComposerHarness onSubmit={vi.fn()} />);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'hi there' } });
    expect(screen.queryByTestId('slash-menu')).toBeNull();
  });
});

// ── ContextMeter (audit A1/D-A1) ──────────────────────────────────────────────
describe('ContextMeter', () => {
  it('renders nothing when there is no state', () => {
    const { container } = render(<ContextMeter state={null} />);
    expect(container.innerHTML).toBe('');
  });
  it('shows the percent and token usage', () => {
    render(
      <ContextMeter
        state={{
          cumulative_input: 124_000, budget: 200_000, phase: 'ok', percent: 62,
          last_input: 1000, last_output: 500, source: 'claude', estimate: false,
        }}
      />
    );
    const meter = screen.getByTestId('context-meter');
    expect(meter.getAttribute('aria-valuenow')).toBe('62');
    expect(meter.textContent).toContain('62%');
    expect(meter.textContent).toContain('124k');
    expect(meter.textContent).toContain('200k');
  });
  it('flags a critical level with a /compact hint', () => {
    render(
      <ContextMeter
        state={{
          cumulative_input: 196_000, budget: 200_000, phase: 'ok', percent: 98,
          last_input: 0, last_output: 0, source: 'claude', estimate: false,
        }}
      />
    );
    const meter = screen.getByTestId('context-meter');
    expect(meter.getAttribute('data-level')).toBe('crit');
    expect(meter.textContent).toContain('/compact');
  });
});

// ── QuestionCard (audit C4) ───────────────────────────────────────────────────
function questionsRequest(): PendingUserInput {
  return {
    id: 'req-1',
    kind: 'questions',
    message: '',
    questions: [
      { id: 'q1', header: 'Framework', question: 'Which framework?', options: [{ label: 'React' }, { label: 'Vue' }] },
    ],
    resolved: false,
  };
}

describe('QuestionCard', () => {
  it('renders codex questions and answers with the { id: {answers:[label]} } shape', () => {
    const onAnswer = vi.fn();
    render(<QuestionCard request={questionsRequest()} onAnswer={onAnswer} onDismiss={() => {}} />);
    expect(screen.getByText(/Which framework/)).toBeTruthy();
    // Pick an option, then send.
    fireEvent.click(screen.getByRole('button', { name: 'React' }));
    fireEvent.click(screen.getByRole('button', { name: 'Send answer to Codex' }));
    expect(onAnswer).toHaveBeenCalledWith({ q1: { answers: ['React'] } });
  });

  it('Send is disabled until an answer is chosen', () => {
    render(<QuestionCard request={questionsRequest()} onAnswer={vi.fn()} onDismiss={() => {}} />);
    const send = screen.getByRole('button', { name: 'Send answer to Codex' }) as HTMLButtonElement;
    expect(send.disabled).toBe(true);
  });

  it('renders an MCP elicitation as a non-answerable notice', () => {
    const req: PendingUserInput = {
      id: 'e1', kind: 'elicitation', message: 'Enter a token', questions: [], resolved: false,
    };
    render(<QuestionCard request={req} onAnswer={vi.fn()} onDismiss={() => {}} />);
    expect(screen.getByText(/An MCP server asked for input/)).toBeTruthy();
    // No send button for a declined elicitation.
    expect(screen.queryByRole('button', { name: 'Send answer to Codex' })).toBeNull();
  });
});

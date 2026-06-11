import { describe, it, expect } from 'vitest';
import { parseCodexItemCompleted } from '../codexEvents';

describe('parseCodexItemCompleted', () => {
  it('returns null for a non-codex JSON object', () => {
    expect(parseCodexItemCompleted({ type: 'status', item: undefined })).toBeNull();
  });

  it('returns null for an object with no type', () => {
    expect(parseCodexItemCompleted({})).toBeNull();
  });

  it('returns codex_agent_message for item.completed with agent_message', () => {
    const result = parseCodexItemCompleted({
      type: 'item.completed',
      item: { type: 'agent_message', text: '  Hello world  ' },
    });
    expect(result).toEqual({ type: 'codex_agent_message', payload: 'Hello world' });
  });

  it('returns codex_ignored for item.completed with agent_message and blank text', () => {
    const result = parseCodexItemCompleted({
      type: 'item.completed',
      item: { type: 'agent_message', text: '   ' },
    });
    expect(result).toEqual({ type: 'codex_ignored' });
  });

  it('returns codex_reasoning for item.completed with reasoning', () => {
    const result = parseCodexItemCompleted({
      type: 'item.completed',
      item: { type: 'reasoning', text: 'Thinking step' },
    });
    expect(result?.type).toBe('codex_reasoning');
    if (result?.type === 'codex_reasoning') {
      expect(result.payload).toBe('Thinking step');
    }
  });

  it('returns codex_ignored for item.completed with reasoning and blank text', () => {
    const result = parseCodexItemCompleted({
      type: 'item.completed',
      item: { type: 'reasoning', text: '' },
    });
    expect(result).toEqual({ type: 'codex_ignored' });
  });

  it('returns codex_shell_call for item.completed with local_shell_call', () => {
    const result = parseCodexItemCompleted({
      type: 'item.completed',
      item: { type: 'local_shell_call', action: { command: 'ls -la' } },
    });
    expect(result).toEqual({ type: 'codex_shell_call', payload: 'ls -la' });
  });

  it('returns codex_ignored for item.completed with unrecognised item type', () => {
    const result = parseCodexItemCompleted({
      type: 'item.completed',
      item: { type: 'unknown_future_type' },
    });
    expect(result).toEqual({ type: 'codex_ignored' });
  });

  it('returns codex_ignored for thread.started', () => {
    expect(parseCodexItemCompleted({ type: 'thread.started' })).toEqual({ type: 'codex_ignored' });
  });

  it('returns codex_ignored for turn.started', () => {
    expect(parseCodexItemCompleted({ type: 'turn.started' })).toEqual({ type: 'codex_ignored' });
  });

  it('returns codex_ignored for turn.completed', () => {
    expect(parseCodexItemCompleted({ type: 'turn.completed' })).toEqual({ type: 'codex_ignored' });
  });
});

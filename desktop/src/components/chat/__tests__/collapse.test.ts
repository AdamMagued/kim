import { describe, it, expect } from 'vitest';
import { collapseMessages, isIntermediateToolCall, synthesizeExchangeActivity } from '../utils';
import type { KimMessage } from '../../../types';

// ── collapseMessages_merges_retries ──────────────────────────────────────────

describe('collapseMessages_merges_retries', () => {
  it('collapses consecutive duplicate JSON assistant messages so collapsed length < raw length', () => {
    const msgs: KimMessage[] = [
      { role: 'user', content: 'do something' },
      { role: 'assistant', content: '{"type":"tool_call","tool":"bash","args":{}}' },
      { role: 'assistant', content: '{"type":"tool_call","tool":"bash","args":{}}' }, // retry
      { role: 'assistant', content: '{"type":"tool_call","tool":"bash","args":{}}' }, // retry
    ];

    const collapsed = collapseMessages(msgs);

    expect(collapsed.length).toBeLessThan(msgs.length);
    // three identical assistant messages merge into one with retries=2
    expect(collapsed.length).toBe(2);
    expect(collapsed[1].retries).toBe(2);
  });

  it('increments retries counter by 1 per duplicate dropped', () => {
    const dupe = '{"type":"tool_use","name":"read_file","input":{}}';
    const msgs: KimMessage[] = [
      { role: 'user', content: 'read it' },
      { role: 'assistant', content: dupe },
      { role: 'assistant', content: dupe }, // +1 retry
    ];

    const collapsed = collapseMessages(msgs);

    expect(collapsed).toHaveLength(2);
    expect(collapsed[1].retries).toBe(1);
  });

  it('does NOT collapse when content differs', () => {
    const msgs: KimMessage[] = [
      { role: 'user', content: 'go' },
      { role: 'assistant', content: '{"type":"tool_call","tool":"bash","args":{}}' },
      { role: 'assistant', content: '{"type":"tool_call","tool":"read_file","args":{}}' },
    ];

    const collapsed = collapseMessages(msgs);

    // different content — nothing merged
    expect(collapsed.length).toBe(msgs.length);
    expect(collapsed[1].retries).toBe(0);
    expect(collapsed[2].retries).toBe(0);
  });

  it('does NOT collapse non-JSON (plain text) assistant messages even if identical', () => {
    const msgs: KimMessage[] = [
      { role: 'user', content: 'hi' },
      { role: 'assistant', content: 'Hello there' },
      { role: 'assistant', content: 'Hello there' },
    ];

    const collapsed = collapseMessages(msgs);

    // plain text strings don't start with '{' — must not be merged
    expect(collapsed.length).toBe(msgs.length);
  });
});

// ── isIntermediateToolCall_detection ─────────────────────────────────────────

describe('isIntermediateToolCall_detection', () => {
  it('returns true for an assistant message with type=tool_call', () => {
    const msg: KimMessage = {
      role: 'assistant',
      content: '{"type":"tool_call","tool":"bash","args":{"command":"ls"}}',
    };

    expect(isIntermediateToolCall(msg)).toBe(true);
  });

  it('returns true for an assistant message with type=tool_use', () => {
    const msg: KimMessage = {
      role: 'assistant',
      content: '{"type":"tool_use","name":"read_file","input":{}}',
    };

    expect(isIntermediateToolCall(msg)).toBe(true);
  });

  it('returns false for a plain text final answer', () => {
    const msg: KimMessage = {
      role: 'assistant',
      content: 'Here is the answer to your question.',
    };

    expect(isIntermediateToolCall(msg)).toBe(false);
  });

  it('returns false for a user message even with matching JSON shape', () => {
    const msg: KimMessage = {
      role: 'user',
      content: '{"type":"tool_call","tool":"bash","args":{}}',
    };

    expect(isIntermediateToolCall(msg)).toBe(false);
  });

  it('returns false for assistant with block-array content (not a string)', () => {
    const msg: KimMessage = {
      role: 'assistant',
      content: [{ type: 'text', text: 'done' }],
    };

    expect(isIntermediateToolCall(msg)).toBe(false);
  });

  it('returns false for JSON that lacks type=tool_call/tool_use', () => {
    const msg: KimMessage = {
      role: 'assistant',
      content: '{"type":"answer","text":"hello"}',
    };

    expect(isIntermediateToolCall(msg)).toBe(false);
  });
});

// ── synthesizeExchangeActivity_maps_user_to_activity ─────────────────────────

describe('synthesizeExchangeActivity_maps_user_to_activity', () => {
  it('produces activity items for a user→tool→answer exchange', () => {
    const allMsgs: KimMessage[] = [
      // real user turn (userIdx=0)
      { role: 'user', content: 'read the config file' },
      // intermediate: assistant issues a tool call
      {
        role: 'assistant',
        content: [
          { type: 'tool_use', id: 't1', name: 'read_file', input: { path: '/project/config.ts' } },
        ],
      },
      // tool result (user role, but only tool_result blocks — not a real user message)
      {
        role: 'user',
        content: [{ type: 'tool_result', tool_use_id: 't1', content: 'export default {}' }],
      },
      // final text-only answer — excluded from activity slice
      { role: 'assistant', content: [{ type: 'text', text: 'I read the file, here is the summary.' }] },
    ];

    const items = synthesizeExchangeActivity(allMsgs, 0);

    expect(items.length).toBeGreaterThan(0);
    // the tool_use block must produce a 'tool' kind activity item
    const toolItem = items.find(i => i.kind === 'tool');
    expect(toolItem).toBeDefined();
    // label should mention the file basename
    expect(toolItem!.text).toContain('config.ts');
  });

  it('maps the correct exchange when multiple user turns exist', () => {
    const allMsgs: KimMessage[] = [
      // 0th real user turn
      { role: 'user', content: 'first question' },
      { role: 'assistant', content: [{ type: 'text', text: 'first answer' }] },
      // 1st real user turn
      { role: 'user', content: 'second question' },
      {
        role: 'assistant',
        content: [
          { type: 'tool_use', id: 't2', name: 'run_command', input: { command: 'ls -la' } },
        ],
      },
      { role: 'assistant', content: [{ type: 'text', text: 'second answer' }] },
    ];

    const items0 = synthesizeExchangeActivity(allMsgs, 0);
    const items1 = synthesizeExchangeActivity(allMsgs, 1);

    // first exchange has no intermediate tool calls — text-only answer is excluded
    expect(Array.isArray(items0)).toBe(true);
    // second exchange has a tool_use intermediate
    expect(items1.length).toBeGreaterThan(0);
    const toolItem = items1.find(i => i.kind === 'tool');
    expect(toolItem).toBeDefined();
    expect(toolItem!.text).toContain('ls -la');
  });

  it('returns empty array for an out-of-bounds userIdx', () => {
    const allMsgs: KimMessage[] = [
      { role: 'user', content: 'only one turn' },
      { role: 'assistant', content: [{ type: 'text', text: 'answer' }] },
    ];

    const items = synthesizeExchangeActivity(allMsgs, 99);

    expect(items).toEqual([]);
  });

  it('returns empty array for an empty message list', () => {
    expect(synthesizeExchangeActivity([], 0)).toEqual([]);
  });
});

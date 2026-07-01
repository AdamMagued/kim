/**
 * V-3a TypeScript side: golden-transcript seam test.
 *
 * These lines are the exact stdout the Python agent emits for a standard
 * FakeProvider run (see tests/fixtures/golden_transcript.json and
 * tests/test_v3_golden_transcript.py).
 *
 * Rules this test guards:
 * 1. Typed JSON events must be silenced (type:'none') if they ever reach
 *    kim-agent-output through the Codex compatibility stream — the real
 *    payloads come via typed kim:* Tauri events.
 * 2. Legacy [TOOL] lines produce an activity_item with kind:'tool'.
 * 3. The Codex JSONL branch (item.completed) still works and produces
 *    the appropriate codex_* types.
 * 4. Any new typed event the Python side adds must be explicitly silenced
 *    here — adding a new "type" key that falls through to activity feed
 *    would be a protocol bug caught by a new test.
 */
import { describe, it, expect } from 'vitest';
import { parseAgentLine } from '../parsers';

// ── Typed JSON events — must be silenced (handled via typed kim:* IPC) ────────

describe('Golden transcript: typed JSON events are silenced', () => {
  it('status event is silenced', () => {
    const line = '{"type":"status","message":"Kim is working on it\\u2026"}';
    expect(parseAgentLine(line, 1).type).toBe('none');
  });

  it('context event is silenced', () => {
    const line = '{"type":"context","cumulative_input":0,"budget":100000,"phase":"ok","percent":0,"last_input":0,"last_output":0,"source":"session","estimate":false}';
    expect(parseAgentLine(line, 1).type).toBe('none');
  });

  it('context event with estimate=true is silenced', () => {
    const line = '{"type":"context","cumulative_input":2458,"budget":100000,"phase":"ok","percent":2,"last_input":2458,"last_output":0,"source":"FakeProvider","estimate":true}';
    expect(parseAgentLine(line, 1).type).toBe('none');
  });

  it('stats event is silenced', () => {
    const line = '{"type":"stats","input":1200,"output":400,"total":1600}';
    expect(parseAgentLine(line, 1).type).toBe('none');
  });

  it('ui_screenshot_flash event is silenced (handled by kim:ui typed event)', () => {
    const line = '{"type":"ui_screenshot_flash"}';
    expect(parseAgentLine(line, 1).type).toBe('none');
  });

  it('ui_show event is silenced (handled by kim:ui typed event)', () => {
    const line = '{"type":"ui_show"}';
    expect(parseAgentLine(line, 1).type).toBe('none');
  });

  it('plan event is silenced', () => {
    const line = '{"type":"plan","steps":["Step 1","Step 2"]}';
    expect(parseAgentLine(line, 1).type).toBe('none');
  });

  it('step event is silenced', () => {
    const line = '{"type":"step","n":1,"data":{"index":1,"text":"Step 1"}}';
    expect(parseAgentLine(line, 1).type).toBe('none');
  });

  it('done event is silenced', () => {
    const line = '{"type":"done","n":1}';
    expect(parseAgentLine(line, 1).type).toBe('none');
  });

  it('usage event is silenced', () => {
    const line = '{"type":"usage","provider":"FakeProvider","input":100,"output":50}';
    expect(parseAgentLine(line, 1).type).toBe('none');
  });

  it('rate_limited event is silenced', () => {
    const line = '{"type":"rate_limited","delay":5.0,"attempt":1,"max_retries":3}';
    expect(parseAgentLine(line, 1).type).toBe('none');
  });

  it('run_failed event is silenced', () => {
    const line = '{"type":"run_failed","reason":"max_iterations","recoverable":true,"suggestion":"Try a simpler task."}';
    expect(parseAgentLine(line, 1).type).toBe('none');
  });

  it.each([
    '{"type":"tool","name":"read_file","args":{}}',
    '{"type":"answer","text":"done"}',
    '{"type":"diff","path":"main.ts","added":2,"removed":1}',
    '{"type":"activity","kind":"error","text":"failed"}',
  ])('schema-generated activity event is silenced on the raw stream: %s', line => {
    expect(parseAgentLine(line, 1).type).toBe('none');
  });
});

// ── Legacy [TOOL] lines — must produce activity_item ─────────────────────────

describe('Golden transcript: legacy [TOOL] lines produce activity items', () => {
  it('[TOOL] take_screenshot produces a tool activity item', () => {
    const line = '[TOOL] take_screenshot({})';
    const result = parseAgentLine(line, 1);
    expect(result.type).toBe('activity_item');
    if (result.type === 'activity_item') {
      expect(result.payload.kind).toBe('tool');
    }
  });

  it('[TOOL] run_command produces a tool activity item', () => {
    const line = '[TOOL] run_command({"cmd":"ls -la"})';
    const result = parseAgentLine(line, 1);
    expect(result.type).toBe('activity_item');
    if (result.type === 'activity_item') {
      expect(result.payload.kind).toBe('tool');
    }
  });

  it('[TOOL] read_file produces a tool activity item', () => {
    const line = '[TOOL] read_file({"path":"/tmp/test.txt"})';
    const result = parseAgentLine(line, 1);
    expect(result.type).toBe('activity_item');
    if (result.type === 'activity_item') {
      expect(result.payload.kind).toBe('tool');
    }
  });
});

// ── Plan/step markers from legacy [STATUS] embed ─────────────────────────────

describe('Golden transcript: plan markers embedded in legacy [STATUS] lines', () => {
  it('[STATUS] [PLAN]{...} produces activity_item with plan text', () => {
    const line = '[STATUS] [PLAN]{"steps":["Step 1","Step 2"]}';
    const result = parseAgentLine(line, 1);
    // Legacy plan markers arrive via kim-agent-output and are treated as status items.
    expect(result.type).toBe('activity_item');
    if (result.type === 'activity_item') {
      expect(result.payload.kind).toBe('status');
      expect(result.payload.text).toContain('[PLAN]');
    }
  });
});

// ── [SUCCESS] / [FAILED] legacy markers ──────────────────────────────────────

describe('Golden transcript: [SUCCESS] and [FAILED] lines', () => {
  it('[SUCCESS] produces a success activity item', () => {
    const line = '[SUCCESS] Task completed';
    const result = parseAgentLine(line, 1);
    expect(result.type).toBe('activity_item');
    if (result.type === 'activity_item') {
      expect(result.payload.kind).toBe('success');
    }
  });

  it('[FAILED] produces an error type (parseAgentLine unwraps error items)', () => {
    const line = '[FAILED] Something went wrong';
    const result = parseAgentLine(line, 1);
    // parseAgentLine unwraps kind:'error' ActivityItems to type:'error' directly
    expect(result.type).toBe('error');
  });
});

// ── Full golden fixture round-trip ────────────────────────────────────────────

describe('Golden transcript fixture: full round-trip', () => {
  // These are the exact lines from tests/fixtures/golden_transcript.json
  // for the default FakeProvider(take_screenshot → TASK_COMPLETE) scenario.
  const GOLDEN_LINES: Array<{ line: string; expectedType: string }> = [
    {
      line: '{"type":"status","message":"Kim is working on it\\u2026"}',
      expectedType: 'none',
    },
    {
      line: '{"type":"context","cumulative_input":0,"budget":100000,"phase":"ok","percent":0,"last_input":0,"last_output":0,"source":"session","estimate":false}',
      expectedType: 'none',
    },
    {
      line: '{"type":"context","cumulative_input":2458,"budget":100000,"phase":"ok","percent":2,"last_input":2458,"last_output":0,"source":"FakeProvider","estimate":true}',
      expectedType: 'none',
    },
    { line: '[TOOL] take_screenshot({})', expectedType: 'activity_item' },
    { line: '{"type":"ui_screenshot_flash"}', expectedType: 'none' },
    { line: '{"type":"ui_show"}', expectedType: 'none' },
    {
      line: '{"type":"context","cumulative_input":6457,"budget":100000,"phase":"ok","percent":6,"last_input":3999,"last_output":0,"source":"FakeProvider","estimate":true}',
      expectedType: 'none',
    },
  ];

  for (const { line, expectedType } of GOLDEN_LINES) {
    it(`line "${line.slice(0, 60)}…" → type="${expectedType}"`, () => {
      expect(parseAgentLine(line, 1).type).toBe(expectedType);
    });
  }
});

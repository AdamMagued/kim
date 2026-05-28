import { describe, it, expect } from 'vitest';
import { parseAgentLine, buildThinkingTrace } from '../parsers';
import { parsePlanFromActivity } from '../utils';
import type { ActivityItem } from '../types';

describe('Chat Parsers and Plan Extractors', () => {
  describe('parsePlanFromActivity', () => {
    it('should parse PLAN, STEP, and DONE json markers correctly', () => {
      const activity: ActivityItem[] = [
        {
          id: 1,
          kind: 'status',
          text: '[PLAN]{"steps":["Step One","Step Two","Step Three"]}',
          timestamp: 1000,
        },
        {
          id: 2,
          kind: 'status',
          text: '[STEP]{"index":1}',
          timestamp: 2000,
        },
        {
          id: 3,
          kind: 'status',
          text: '[DONE]{"index":1,"summary":"Done step one"}',
          timestamp: 3000,
        },
        {
          id: 4,
          kind: 'status',
          text: '[STEP]{"index":2}',
          timestamp: 4000,
        },
      ];

      const livePlan = parsePlanFromActivity(activity);
      expect(livePlan).not.toBeNull();
      expect(livePlan?.steps).toEqual(['Step One', 'Step Two', 'Step Three']);
      expect(livePlan?.activeStep).toBe(2);
      expect(livePlan?.doneSteps).toEqual([1]);
      expect(livePlan?.structured).toBe(true);
    });
  });

  describe('buildThinkingTrace', () => {
    it('should construct correct plan and thought traces', () => {
      const items: ActivityItem[] = [
        {
          id: 1,
          kind: 'status',
          text: '[PLAN]{"steps":["Step One","Step Two"]}',
          timestamp: 1000,
        },
        {
          id: 2,
          kind: 'tool',
          text: 'Reading file `test.txt`',
          timestamp: 2000,
        },
        {
          id: 3,
          kind: 'status',
          text: 'Running terminal command',
          timestamp: 3000,
        },
      ];

      const livePlan = parsePlanFromActivity(items);
      const trace = buildThinkingTrace(items, livePlan);

      expect(trace.length).toBe(3);
      expect(trace[0].kind).toBe('plan');
      expect(trace[1].kind).toBe('tool');
      expect((trace[1] as any).verb).toBe('Reading');
      expect((trace[1] as any).target).toBe('file test.txt');
      expect(trace[2].kind).toBe('thought');
      expect((trace[2] as any).text).toBe('Running terminal command');
    });
  });

  describe('parseAgentLine status and context variants', () => {
    it('should parse STATS correctly', () => {
      const line = '[STATS]   input_tokens=1500 output_tokens=350 total_tokens=1850';
      const parsed = parseAgentLine(line, 1);
      expect(parsed.type).toBe('stats');
      if (parsed.type === 'stats') {
        expect(parsed.payload.input).toBe(1500);
        expect(parsed.payload.output).toBe(350);
        expect(parsed.payload.total).toBe(1850);
      }
    });

    it('should parse CONTEXT correctly', () => {
      const line = '[CONTEXT] cumulative_input=24500 budget=200000 phase=ok percent=12 last_input=450 last_output=12 source=api estimate=0';
      const parsed = parseAgentLine(line, 1);
      expect(parsed.type).toBe('context');
      if (parsed.type === 'context') {
        expect(parsed.payload.cumulative_input).toBe(24500);
        expect(parsed.payload.budget).toBe(200000);
        expect(parsed.payload.phase).toBe('ok');
        expect(parsed.payload.percent).toBe(12);
        expect(parsed.payload.last_input).toBe(450);
        expect(parsed.payload.last_output).toBe(12);
        expect(parsed.payload.source).toBe('api');
        expect(parsed.payload.estimate).toBe(false);
      }
    });

    it('should parse USAGE correctly', () => {
      const line = '[USAGE] {"provider":"openai","model":"gpt-4o","input":150,"output":50}';
      const parsed = parseAgentLine(line, 1);
      expect(parsed.type).toBe('usage');
      if (parsed.type === 'usage') {
        expect(parsed.payload.provider).toBe('openai');
        expect(parsed.payload.model).toBe('gpt-4o');
        expect(parsed.payload.input).toBe(150);
        expect(parsed.payload.output).toBe(50);
      }
    });

    it('should parse NEED_HELP correctly', () => {
      const line = 'NEED_HELP: Reached conversational loop.';
      const parsed = parseAgentLine(line, 1);
      expect(parsed.type).toBe('need_help');
      if (parsed.type === 'need_help') {
        expect(parsed.payload).toBe('Reached conversational loop.');
      }
    });
  });

  describe('parseAgentLine Codex JSONL parser', () => {
    it('should parse agent message completed event', () => {
      const line = '{"type":"item.completed","item":{"type":"agent_message","text":"I am completed."}}';
      const parsed = parseAgentLine(line, 1);
      expect(parsed.type).toBe('codex_agent_message');
      if (parsed.type === 'codex_agent_message') {
        expect(parsed.payload).toBe('I am completed.');
      }
    });

    it('should parse reasoning completed event', () => {
      const line = '{"type":"item.completed","item":{"type":"reasoning","text":"I should check file."}}';
      const parsed = parseAgentLine(line, 1);
      expect(parsed.type).toBe('codex_reasoning');
      if (parsed.type === 'codex_reasoning') {
        expect(parsed.payload).toBe('I should check file.');
      }
    });

    it('should parse local shell command completed event', () => {
      const line = '{"type":"item.completed","item":{"type":"local_shell_call","action":{"command":"npm install"}}}';
      const parsed = parseAgentLine(line, 1);
      expect(parsed.type).toBe('codex_shell_call');
      if (parsed.type === 'codex_shell_call') {
        expect(parsed.payload).toBe('npm install');
      }
    });
  });
});

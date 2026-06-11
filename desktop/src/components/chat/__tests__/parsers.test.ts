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
          icon: '',
          text: '[PLAN]{"steps":["Step One","Step Two","Step Three"]}',
        },
        {
          id: 2,
          kind: 'status',
          icon: '',
          text: '[STEP]{"index":1}',
        },
        {
          id: 3,
          kind: 'status',
          icon: '',
          text: '[DONE]{"index":1,"summary":"Done step one"}',
        },
        {
          id: 4,
          kind: 'status',
          icon: '',
          text: '[STEP]{"index":2}',
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
          icon: '',
          text: '[PLAN]{"steps":["Step One","Step Two"]}',
        },
        {
          id: 2,
          kind: 'tool',
          icon: '',
          text: 'Reading file `test.txt`',
        },
        {
          id: 3,
          kind: 'status',
          icon: '',
          text: 'Running terminal command',
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
    it('should parse NEED_HELP correctly', () => {
      const line = 'NEED_HELP: Reached conversational loop.';
      const parsed = parseAgentLine(line, 1);
      expect(parsed.type).toBe('need_help');
      if (parsed.type === 'need_help') {
        expect(parsed.payload).toBe('Reached conversational loop.');
      }
    });
  });

  describe('parseAgentLine Kim lifecycle JSON lines', () => {
    it('should silently ignore run_done JSON lines', () => {
      const line = '{"type":"run_done","termination":"task_complete","success":true}';
      const parsed = parseAgentLine(line, 1);
      expect(parsed.type).toBe('none');
    });

    it('should silently ignore run_done failure JSON lines', () => {
      const line = '{"type":"run_done","termination":"max_iterations","success":false}';
      const parsed = parseAgentLine(line, 1);
      expect(parsed.type).toBe('none');
    });

    it('should silently ignore provider_error JSON lines', () => {
      const line = '{"type":"provider_error","code":"rate_limit","retryable":false}';
      const parsed = parseAgentLine(line, 1);
      expect(parsed.type).toBe('none');
    });

    it('should silently ignore provider_error retryable JSON lines', () => {
      const line = '{"type":"provider_error","code":"server_error","retryable":true}';
      const parsed = parseAgentLine(line, 1);
      expect(parsed.type).toBe('none');
    });

    it('should silently ignore hitl_approval_request JSON lines', () => {
      const line = '{"type":"hitl_approval_request","tool":"run_command","risk":"high","reason":"arbitrary_code_execution"}';
      const parsed = parseAgentLine(line, 1);
      expect(parsed.type).toBe('none');
    });

    it('should silently ignore hitl_approval_result approved JSON lines', () => {
      const line = '{"type":"hitl_approval_result","tool":"run_command","approved":true}';
      const parsed = parseAgentLine(line, 1);
      expect(parsed.type).toBe('none');
    });

    it('should silently ignore hitl_approval_result denied JSON lines', () => {
      const line = '{"type":"hitl_approval_result","tool":"delete_file","approved":false}';
      const parsed = parseAgentLine(line, 1);
      expect(parsed.type).toBe('none');
    });

    it('should silence hitl_approval_request for all high-risk tools', () => {
      const tools = ['run_command', 'delete_file', 'git_commit', 'run_python'];
      for (const tool of tools) {
        const line = `{"type":"hitl_approval_request","tool":"${tool}","risk":"high","reason":"arbitrary_code_execution"}`;
        const parsed = parseAgentLine(line, 1);
        expect(parsed.type).toBe('none');
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

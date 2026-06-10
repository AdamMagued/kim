import { describe, expect, it } from 'vitest';
import {
  extractInvokeError,
  formatNextRun,
  formatTimerLastResult,
  parseRunDueResponse,
  parseTaskList,
  parseTimerStatus,
} from '../SchedulePane';

describe('SchedulePane helpers', () => {
  describe('parseTaskList', () => {
    it('keeps valid scheduled task records', () => {
      const tasks = parseTaskList(JSON.stringify([
        { id: 'a', task: 'check disk', schedule_expr: '@hourly', enabled: true },
        { id: 1, task: 'bad' },
      ]));

      expect(tasks).toHaveLength(1);
      expect(tasks[0].id).toBe('a');
      expect(tasks[0].task).toBe('check disk');
    });

    it('returns an empty list for invalid JSON or non-arrays', () => {
      expect(parseTaskList('not json')).toEqual([]);
      expect(parseTaskList(JSON.stringify({ ok: true }))).toEqual([]);
    });
  });

  describe('parseRunDueResponse', () => {
    it('parses JSON objects returned by run-due', () => {
      expect(parseRunDueResponse('{"ok":true,"launched":false,"reason":"no tasks due"}')).toEqual({
        ok: true,
        launched: false,
        reason: 'no tasks due',
      });
    });

    it('returns an error object for invalid responses', () => {
      expect(parseRunDueResponse('[]').ok).toBe(false);
      expect(parseRunDueResponse('not json').error).toContain('parse');
    });
  });

  describe('extractInvokeError', () => {
    it('prefers JSON error strings from kimctl', () => {
      expect(extractInvokeError('{"ok":false,"error":"bad schedule"}')).toBe('bad schedule');
    });

    it('falls back to a string representation', () => {
      expect(extractInvokeError('plain failure')).toBe('plain failure');
      expect(extractInvokeError(new Error('boom'))).toBe('Error: boom');
    });
  });

  describe('run-due JSON error roundtrip', () => {
    it('parseRunDueResponse recovers the error field from a JSON rejection string', () => {
      // kimctl writes {"ok":false,"error":"..."} to stdout and exits 1;
      // run_kimctl returns Err(stdout_json) which Tauri forwards as the rejection string.
      const rejection = '{"ok":false,"error":"preflight failed: interpreter not found"}';
      const result = parseRunDueResponse(rejection);
      expect(result.ok).toBe(false);
      expect(result.error).toBe('preflight failed: interpreter not found');
    });

    it('extractInvokeError strips JSON to bare string, breaking re-parse', () => {
      // Documents WHY handleRunDue catch must not call extractInvokeError first.
      const rejection = '{"ok":false,"error":"preflight failed: interpreter not found"}';
      const bare = extractInvokeError(rejection);
      expect(bare).toBe('preflight failed: interpreter not found');
      const lost = parseRunDueResponse(bare);
      expect(lost.ok).toBe(false);
      // After unwrapping, the bare string is no longer a JSON object — parseRunDueResponse
      // returns a generic "Failed to parse" error and the original message is lost.
      expect(lost.error).not.toBe('preflight failed: interpreter not found');
    });

    it('parseRunDueResponse falls back gracefully on plain-text Rust rejections', () => {
      const result = parseRunDueResponse('Failed to start kimctl: No such file or directory');
      expect(result.ok).toBe(false);
      expect(typeof result.error).toBe('string');
    });
  });

  describe('formatNextRun', () => {
    it('handles empty and invalid timestamps', () => {
      expect(formatNextRun(null)).toBe('--');
      expect(formatNextRun('not a date')).toBe('--');
    });
  });

  describe('parseTimerStatus', () => {
    it('parses timer status objects', () => {
      expect(parseTimerStatus({
        running: true,
        interval_seconds: 120,
        tick_count: 3,
        last_result: '{"ok":true}',
      })).toEqual({
        running: true,
        interval_seconds: 120,
        tick_count: 3,
        last_result: '{"ok":true}',
        last_error: null,
      });
    });

    it('parses JSON strings and handles invalid values', () => {
      expect(parseTimerStatus('{"running":false,"interval_seconds":60,"tick_count":1}').tick_count).toBe(1);
      expect(parseTimerStatus('not json').last_error).toContain('parse');
      expect(parseTimerStatus([]).last_error).toContain('Invalid');
    });
  });

  describe('formatTimerLastResult', () => {
    it('uses the most specific useful timer result field', () => {
      expect(formatTimerLastResult('{"ok":true,"reason":"no tasks due"}')).toBe('no tasks due');
      expect(formatTimerLastResult('{"ok":true,"task":"review logs"}')).toBe('review logs');
      expect(formatTimerLastResult('{"ok":false,"error":"provider blocked"}')).toBe('provider blocked');
      expect(formatTimerLastResult('not json')).toContain('parse');
    });
  });
});

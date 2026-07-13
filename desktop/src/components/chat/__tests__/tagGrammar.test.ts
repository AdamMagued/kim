import { describe, it, expect } from 'vitest';
import { parseAgentLine } from '../parsers';
import { parsePlanFromActivity } from '../utils';
import type { ActivityItem } from '../types';

// F-H-6: golden fixture for the legacy `[TAG]` text protocol. The grammar is
// documented in docs/CONTRACTS.md "Seam 2"; this pins the representative shapes
// the frontend parser must keep decoding so emit (Python f-strings) and parse
// (utils.ts/parsers.ts) cannot silently diverge across the language boundary.
describe('legacy tag grammar conformance (F-H-6)', () => {
  // tool-line ::= "[TOOL]" SP [module ": "] tool_name "(" json-args ")"
  it('tool-line: plain and module-prefixed forms decode to a tool item', () => {
    const plain = parseAgentLine('[TOOL] read_file({"path":"a.txt"})', 1);
    expect(plain.type).toBe('activity_item');
    expect(plain.type === 'activity_item' && plain.payload.kind).toBe('tool');
    expect(plain.type === 'activity_item' && plain.payload.text).toContain('a.txt');

    const moduled = parseAgentLine('[TOOL] fs.io: write_file({"path":"b.txt"})', 2);
    expect(moduled.type).toBe('activity_item');
    expect(moduled.type === 'activity_item' && moduled.payload.kind).toBe('tool');
    expect(moduled.type === 'activity_item' && moduled.payload.text).toContain('b.txt');
  });

  // tool-line json-args may contain unbalanced/nested characters — the frontend
  // brace/quote state machine must survive parentheses inside string values.
  it('tool-line: balanced-paren extractor survives parens inside arg strings', () => {
    const nested = parseAgentLine('[TOOL] run_command({"command":"echo (hi) )("})', 3);
    expect(nested.type).toBe('activity_item');
    expect(nested.type === 'activity_item' && nested.payload.kind).toBe('tool');
  });

  // diff-line ::= "[DIFF] path=" basename " +" int " -" int
  it('diff-line decodes path + added/removed counts', () => {
    const diff = parseAgentLine('[DIFF] path=main.rs +10 -3', 4);
    expect(diff).toEqual({ type: 'diff', payload: { path: 'main.rs', added: 10, removed: 3 } });
  });

  // tag-line ::= "[SUCCESS]" / "TASK_COMPLETE:" / "NEED_HELP:"
  it('success + completion + need-help markers decode to their kinds', () => {
    const success = parseAgentLine('[SUCCESS] All checks passed', 5);
    expect(success.type === 'activity_item' && success.payload.kind).toBe('success');

    const complete = parseAgentLine('TASK_COMPLETE: shipped the fix', 6);
    expect(complete.type === 'activity_item' && complete.payload.kind).toBe('success');

    const help = parseAgentLine('NEED_HELP: which file should I edit?', 7);
    expect(help).toEqual({ type: 'need_help', payload: 'which file should I edit?' });
  });

  // plan-envelope-line ::= "[STATUS]" SP ("[PLAN]"|"[STEP]"|"[DONE]") json-object
  // (double-wrapped: a plan marker rides inside a [STATUS] line).
  it('plan-envelope: double-wrapped [PLAN]/[STEP]/[DONE] drive the structured plan', () => {
    const items: ActivityItem[] = [
      { id: 1, kind: 'status', icon: '›', text: '[STATUS] [PLAN]{"steps":["open","edit","save"]}' },
      { id: 2, kind: 'status', icon: '›', text: '[STATUS] [STEP]{"index":2,"name":"edit"}' },
      { id: 3, kind: 'status', icon: '›', text: '[STATUS] [DONE]{"index":1}' },
    ];
    const plan = parsePlanFromActivity(items);
    expect(plan).not.toBeNull();
    expect(plan!.structured).toBe(true);
    expect(plan!.steps).toEqual(['open', 'edit', 'save']);
    expect(plan!.activeStep).toBe(2);
    expect(plan!.doneSteps).toContain(1);
  });

  // free-text ::= anything not matching a tag → not misclassified as a marker.
  it('free-text containing tag-like words is not mis-parsed as a marker', () => {
    // A benign sentence that merely mentions a tag name must not decode as diff/tool.
    const line = parseAgentLine('Investigating the read_file helper in utils', 8);
    expect(line.type).not.toBe('diff');
    expect(line.type === 'activity_item' ? line.payload.kind : line.type).not.toBe('tool');
  });
});

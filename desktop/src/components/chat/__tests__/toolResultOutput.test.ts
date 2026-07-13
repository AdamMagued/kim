import { describe, it, expect } from 'vitest';
import { extractTouchedFiles } from '../utils';
import type { KimMessage, ToolResultBlock } from '../../../types';

// F-F-9: the runtime serializes some tool_result blocks with an `output`
// string instead of `content` (codex / app-server session JSONL). The frontend
// derives touched-file diffs from that field. Before this fix `output` was NOT
// on the ToolResultBlock type and was read via `as unknown as { output }`
// casts — tsc could not catch a backend rename. Modeling `output` makes this
// fixture compile-checked: the object literal below would fail `tsc --noEmit`
// if `output` were removed from ToolResultBlock again.
describe('ToolResultBlock.output (F-F-9)', () => {
  it('extractTouchedFiles reads a structuredPatch from the `output` field', () => {
    // Strongly typed — the `output` key is compile-gated by the ToolResultBlock type.
    const resultBlock: ToolResultBlock = {
      type: 'tool_result',
      tool_use_id: 'call_1',
      content: [],
      output: JSON.stringify({
        filePath: '/repo/src/app.ts',
        structuredPatch: [{ lines: ['+added one', '+added two', '-removed one'] }],
      }),
    };
    const messages: KimMessage[] = [{ role: 'tool', content: [resultBlock] }];

    const touched = extractTouchedFiles(messages);
    expect(touched).toHaveLength(1);
    expect(touched[0].path).toBe('/repo/src/app.ts');
    expect(touched[0].added).toBe(2);
    expect(touched[0].removed).toBe(1);
  });
});

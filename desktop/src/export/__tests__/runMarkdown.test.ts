import { describe, it, expect } from 'vitest';
import { buildRunMarkdown, type RunExport } from '../runMarkdown';

const sample: RunExport = {
  title: 'Fix the login bug',
  provider: 'claude',
  model: 'claude-opus-4-8',
  durationSec: 95,
  costUsd: 0.0123,
  timestamp: '2026-06-18T10:00:00Z',
  messages: [
    { role: 'user', text: 'Fix the login bug' },
    { role: 'assistant', text: 'Done — the token check used `<` instead of `<=`.' },
  ],
  activity: ['Read auth.ts', 'Edited auth.ts'],
  touchedFiles: [{ path: 'src/auth.ts', added: 3, removed: 1 }],
};

describe('buildRunMarkdown (K10)', () => {
  it('matches snapshot', () => {
    expect(buildRunMarkdown(sample)).toMatchInlineSnapshot(`
      "# Fix the login bug

      **Provider:** claude  ·  **Model:** claude-opus-4-8  ·  **Duration:** 1m 35s  ·  **Cost:** $0.0123  ·  **When:** 2026-06-18T10:00:00Z

      ## Conversation

      ### User

      Fix the login bug

      ### Kim

      Done — the token check used \`<\` instead of \`<=\`.

      ## Activity

      - Read auth.ts
      - Edited auth.ts

      ## Files touched

      - \`src/auth.ts\` (+3/-1)
      "
    `);
  });

  it('handles a minimal run (no meta, no activity)', () => {
    const md = buildRunMarkdown({ messages: [{ role: 'user', text: 'hi' }] });
    expect(md).toContain('# Kim run');
    expect(md).toContain('### User');
    expect(md).not.toContain('## Activity');
    expect(md).not.toContain('## Files touched');
  });

  it('renders empty message text as placeholder', () => {
    const md = buildRunMarkdown({ messages: [{ role: 'assistant', text: '   ' }] });
    expect(md).toContain('_(empty)_');
  });
});

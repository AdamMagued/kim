import { describe, it, expect } from 'vitest';

// No @types/node in this project; a local ambient require keeps tsc happy.
declare const require: (id: string) => {
  readFileSync(p: string, enc: string): string;
  readdirSync(p: string): string[];
};
const fs = require('node:fs');

// vitest cwd = desktop/
const tokensCss = fs.readFileSync('src/styles/tokens.css', 'utf8');
const styleFiles = fs
  .readdirSync('src/styles')
  .filter((f: string) => f.endsWith('.css') && f !== 'tokens.css');

// F-F-13: stacking is governed by a single named --z-* token scale, not ad-hoc
// literals scattered across the sheets.
describe('z-index token scale (F-F-13)', () => {
  const expectedTokens = [
    '--z-base', '--z-raised', '--z-above', '--z-sticky', '--z-nav', '--z-dropdown',
    '--z-popover', '--z-header', '--z-overlay', '--z-drawer', '--z-modal',
    '--z-modal-top', '--z-onboarding', '--z-toast', '--z-flash',
  ];

  it('tokens.css defines the full ordered scale', () => {
    for (const t of expectedTokens) {
      expect(tokensCss, `missing ${t}`).toContain(`${t}:`);
    }
  });

  it('no sheet uses a bare numeric z-index literal (all go through a token)', () => {
    for (const f of styleFiles) {
      const css = fs.readFileSync(`src/styles/${f}`, 'utf8');
      const bare = css.match(/z-index:\s*-?\d/g);
      expect(bare, `${f} still has a raw z-index literal: ${bare?.join(', ')}`).toBeNull();
    }
  });

  it('every z-index declaration references a defined --z-* token', () => {
    for (const f of styleFiles) {
      const css = fs.readFileSync(`src/styles/${f}`, 'utf8');
      for (const m of css.matchAll(/z-index:\s*var\((--z-[\w-]+)\)/g)) {
        expect(expectedTokens, `${f} uses undefined ${m[1]}`).toContain(m[1]);
      }
    }
  });
});

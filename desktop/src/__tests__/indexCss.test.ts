import { describe, it, expect } from 'vitest';

// This project has no @types/node, and vitest's CSS handling returns an empty
// string for a bundler `?raw` import of a .css file. Read the file directly via
// the Node runtime instead; a local ambient `require` keeps `tsc --noEmit`
// happy without pulling @types/node into the app's type environment.
declare const require: (id: string) => { readFileSync(p: string, enc: string): string };
// vitest runs with cwd = desktop/, so src/index.css resolves off the project root.
const indexCss = require('node:fs').readFileSync('src/index.css', 'utf8');

describe('index.css (F-F-6 / CSS import-order invariant)', () => {
  it('makes no external/CDN @import (no cross-origin font fetch on launch)', () => {
    // A local desktop app must not fetch remote CSS/fonts. Guard against any
    // network-origin @import (http(s):// or protocol-relative //host) being
    // reintroduced — the Google-Fonts Inter import was the regression.
    const externalImport = /@import\s+(?:url\(\s*)?['"]?(?:https?:)?\/\//i;
    expect(indexCss).not.toMatch(externalImport);
    expect(indexCss).not.toContain('fonts.googleapis.com');
    expect(indexCss).not.toContain('fonts.gstatic.com');
  });

  it('preserves the load-bearing style import order (invariant)', () => {
    // The split from the former 6,790-line index.css relies on this exact
    // cascade order. Assert the sequence is unchanged: tailwind first, then
    // tokens → base layers → component sheets → overrides (revamp last).
    const imports = [...indexCss.matchAll(/@import\s+(?:url\()?['"]([^'"]+)['"]/g)].map(m => m[1]);
    expect(imports).toEqual([
      'tailwindcss',
      './styles/tokens.css',
      './styles/animations.css',
      './styles/shell.css',
      './styles/sidebar.css',
      './styles/chat-base.css',
      './styles/chat-welcome.css',
      './styles/chat-activity.css',
      './styles/chat-composer.css',
      './styles/chat-providers.css',
      './styles/chat-session.css',
      './styles/chat-messages.css',
      './styles/tool-cards.css',
      './styles/theme-toggle.css',
      './styles/settings.css',
      './styles/onboarding.css',
      './styles/greeting.css',
      './styles/loaders.css',
      './styles/settings-shader.css',
      './styles/typing-animations.css',
      './styles/revamp.css',
    ]);
  });
});

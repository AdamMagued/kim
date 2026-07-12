// ESLint flat config for the Kim desktop frontend (F-F-1).
//
// Until this landed there was NO linter anywhere in the repo — no config, no
// dependency, no CI step — even though desktop/src/CLAUDE.md told contributors
// "ESLint warns on new `any`" (F-L-12: the doc invented an enforcement
// mechanism that did not exist). ~20k LOC of TS/TSX had never been linted, so
// the entire react-hooks bug class (missing deps, stale closures) the frontend
// hunt chases was mechanically undetectable.
//
// This wires up the standard Vite + React + TS flat config:
//   - typescript-eslint `recommended` (incl. no-explicit-any — promoted to
//     ERROR here to actually enforce the documented no-new-`any` rule)
//   - react-hooks (rules-of-hooks = error, exhaustive-deps = warn) — exactly
//     the missing-dep / stale-closure class the frontend hunt targets
//   - react-refresh (fast-refresh boundary hygiene, warn)
//
// `npm run lint` must exit 0 (zero ERRORS). exhaustive-deps and react-refresh
// findings are intentionally WARN, not error, and form the documented baseline:
// the large existing effects (useChatStream) carry deliberate, commented dep
// omissions that are correct by construction, and several modules deliberately
// export non-component helpers; promoting either to error would force either
// churn or a spray of eslint-disable comments. Warnings surface them without
// failing the gate. A NEW `any` is the one thing that hard-fails.
//
// HANDOFF -> K': wire `npm --prefix desktop run lint` into .github/workflows
// (the frontend lint job the master plan's G3 exit criterion — "eslint
// errors=0" — assumes already exists).

import js from '@eslint/js';
import globals from 'globals';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  // Not source we own or lint: build output, deps, the Rust shell (D'), and the
  // generated IPC event types (edit events.schema.json + regen, never by hand).
  { ignores: ['dist', 'coverage', 'node_modules', 'src-tauri', 'src/types/events.gen.ts'] },
  {
    files: ['src/**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      ...tseslint.configs.recommended,
      reactHooks.configs['recommended-latest'],
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2022,
      globals: { ...globals.browser },
    },
    rules: {
      // Enforce the desktop/src/CLAUDE.md "No new `any`" rule for real.
      '@typescript-eslint/no-explicit-any': 'error',
      // A leading underscore is this codebase's convention for a deliberately
      // unused binding (e.g. `label: _a => 'Typing text'` in the activity-label
      // table, or `catch (_err)`). Respect it instead of forcing noise.
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_', caughtErrorsIgnorePattern: '^_' },
      ],
      // Empty catch blocks are the codebase's fire-and-forget idiom for
      // localStorage / JSON.parse guards that must never throw. Allow them; a
      // genuinely-empty if/for/while block still errors.
      'no-empty': ['error', { allowEmptyCatch: true }],
      // Documented baseline (warn, not error) — pre-existing patterns that are
      // out of scope to refactor in the lint-wiring pass:
      //  - only-export-components: several modules deliberately co-locate a
      //    component with its constants/helpers; splitting them is a separate,
      //    behaviour-neutral refactor. Fast-refresh DX only, no runtime effect.
      //  - no-useless-escape: two redundant-but-harmless `\[` escapes live
      //    inside load-bearing JSON-fragment parser regexes; left verbatim to
      //    guarantee zero parse-grammar change.
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      'no-useless-escape': 'warn',
    },
  },
  {
    // Test files run under Vitest (jsdom + node) — allow their globals and the
    // occasional structural `any` in mock plumbing without failing the gate.
    files: ['src/**/*.{test,spec}.{ts,tsx}', 'src/**/__tests__/**/*.{ts,tsx}'],
    languageOptions: {
      globals: { ...globals.node },
    },
    rules: {
      '@typescript-eslint/no-explicit-any': 'warn',
    },
  },
);

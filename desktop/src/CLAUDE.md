# desktop/src/CLAUDE.md

## What lives here
The React 19 + TypeScript frontend (Tauri webview).

| File/Dir | Role |
|---|---|
| `App.tsx` | Root; owns `useSessions`, `useAccount`, `useTheme` hooks |
| `components/ChatView.tsx` | Main chat UI (~3300 lines — god file, splitting tracked in V-4) |
| `components/chat/` | Sub-components: `ChatComposer`, `StreamRenderer`, `parsers.ts`, `types.ts` |
| `components/kim-ui/` | Shared UI: `RevampSidebar`, `RevampSettings`, `ThinkingWithPlan`, etc. |
| `components/settings/` | Settings panes (one file per pane) |
| `hooks/` | Custom React hooks |
| `styles/` | `design-tokens.css` and global token definitions |
| `index.css` | **Load-bearing import order** — do not reorder `@import` lines |
| `types/` | Shared TypeScript types including `events.gen.ts` (generated — do not hand-edit) |

## Local invariants
- **CSS import order** in `index.css` is cascade-dependent. Changing order breaks themes. The order is: design-tokens → base → components → overrides.
- **`events.gen.ts` is generated**: edit `events.schema.json` + run `npm run gen:events`, never hand-edit.
- **No new `any`**: ESLint enforces this. `npm run lint` (flat config at `desktop/eslint.config.js`) makes `@typescript-eslint/no-explicit-any` a hard **error** in non-test source (a warning in tests, for mock plumbing). Use proper types. The lint also runs `react-hooks` (rules-of-hooks = error) and carries a small documented warn-baseline (exhaustive-deps, fast-refresh boundaries) — see the config header.
- **IPC events**: listen on `kim:*` typed events (not the legacy `[STATUS]` text protocol). Both are currently emitted (dual-emit debt); prefer the typed path for new code.
- **Code tab provider constraint**: the Code tab must never construct an OpenAI auth flow or reference gpt-5.5.

## How to add a settings pane
See `HOW_TO.md` → "Add a settings pane" (3 files to touch).

## How to add an agent event (frontend side)
See `HOW_TO.md` → "Add an agent event" (after V-1 lands: edit schema → regen → render).

## How to test this layer
```bash
cd desktop && npm run test           # Vitest unit tests
cd desktop && npx tsc --noEmit      # Type check
```

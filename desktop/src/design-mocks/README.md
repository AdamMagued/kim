# Kim design mock React components

Standalone React 19 + TypeScript presentational components converted from the uploaded HTML mocks. Import `tokens.css` and `styles.css` once in your app entry before rendering any component.

## Run the real Kim app (npm must find `package.json`)

From the repo root, use the **`desktop`** folder — there is **no** `package.json` directly under `kim-pro/`:

```bash
cd kim-pro/desktop
npm run tauri dev
```

## Dev default: you should see the mock shell

In **development**, the entry (`main.tsx`) renders **`FullWindowShell`** (the `dm-*` mock) **by default**, so the converter output is visible.

## Run normal Kim instead

Create **`kim-pro/desktop/.env.development.local`**:

```bash
VITE_KIM_REAL_UI=true
```

Restart `npm run tauri dev`. Or open **`http://localhost:1420/?kim=1`** (adds query without editing env).

Production builds always ship the real **`App`** (mock shell is dev-only).

## Why importing CSS alone didn’t change Kim

Kim’s UI uses **`kim-*`** classes; mocks use **`dm-*`**. Importing `tokens.css` / `styles.css` without rendering **`dm-*`** markup leaves Kim looking unchanged — that’s expected until components are merged.

```ts
import "./design-mocks/tokens.css";
import "./design-mocks/styles.css";
```

## Files

- `AppLaunchEmpty.tsx` — corresponds to `App launch _ empty state.html`
- `NewCodeSession.tsx` — corresponds to `New Code session.html`
- `ChatPlanCollapsible.tsx` — corresponds to `Chat _ collapsible plan in flight.html`, with the V7 thinking stream merged into the plan UI
- `ChatStreamHybrid.tsx` — corresponds to `V7 _ Hybrid _stream _ inline plan_.html`
- `FullWindowShell.tsx` — corresponds to `Kim _ full window.html`; use mainly for the shell, color, and user bubble treatment
- `SettingsAppearance.tsx` — corresponds to `Settings _ Appearance.html`
- `SettingsAi.tsx` — corresponds to `Settings _ Ai.html`
- `SettingsVoice.tsx` — corresponds to `Settings _ Voice.html`
- `SettingsPaths.tsx` — corresponds to `Settings _ Paths.html`
- `SettingsData.tsx` — corresponds to `Settings _ Data.html`
- `SettingsAccount.tsx` — corresponds to `Settings _ Account.html`
- `SettingsMcp.tsx` — corresponds to `Settings _ Mcp.html`
- `SettingsFeedback.tsx` — corresponds to `Settings _ Feedback.html`
- `SettingsAbout.tsx` — corresponds to `Settings _ About.html`
- `tokens.css` — CSS custom properties only
- `styles.css` — shared prefixed visual styles
- `index.ts` — optional barrel exports

## Notes

- No base64 fonts or binary assets are included.
- Classes use the `dm-` prefix.
- Components are presentational only. No routing, auth, API calls, or app-specific dependencies.
- Props expose the text/list data that differed between screenshots, with defaults matching the mocks.

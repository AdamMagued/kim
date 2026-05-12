# Kim design mock React components

Standalone React 19 + TypeScript presentational components converted from the uploaded HTML mocks. Import `tokens.css` and `styles.css` once in your app entry before rendering any component.

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

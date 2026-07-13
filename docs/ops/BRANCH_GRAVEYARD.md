# Branch graveyard — delete/keep recommendations (Wave 0)

Generated 2026-07-11 against `origin/main` @ `6f69adb` after `git fetch --prune`.
**No branches have been deleted** — this is a recommendation table for user sign-off (plan §Wave-0.3).

## Method
- `merged` = the branch tip is an ancestor of `origin/main` (nothing unique).
- `rebased-equal` = not an ancestor, but `git cherry` shows **every** commit is patch-equivalent
  to a commit already in main (the work landed via rebase/cherry-pick; e.g. the whole
  `fix/audit-*` wave and `fix/secret-sandbox-bypass` — verified individually).
- `unmerged(+N)` = N commits whose patches are NOT in main. All of these fall in two stale buckets:
  - **2026-05-06 … 2026-05-11:** ~60 auto-generated bot branches (code-health/jules/voice-tests/
    fix-unused-import swarm) plus early feature branches — 1-4 tiny commits each on a pre-restructure
    base. The codebase has since been through the AI restructure, god-file splits, and four audit
    campaigns; these patches no longer apply to files that mostly don't exist in that shape anymore.
  - **2026-06-19:** roadmap-phase branches (`fix/backend-plumbing`, `fix/trust-features`, …) whose
    feature content merged; the dangling +1/+2 commits are `ci: rustfmt`/`pyright` formatting nits
    on the old tree.
  - The two `+26` rows are `ai-architecture-restructure` (local+remote) — an abandoned first attempt;
    `ai-architecture-restructure-fixed` is what actually landed (merged).

## Keep (8)
`main`, `origin/main`, `integration/audit-fixes`, `origin/integration/audit-fixes` — active line.
`dev`, `origin/dev` — secondary line (last used 2026-07-08; fold into main flow when convenient).
`feat/roadmap-to-10`, `origin/feat/roadmap-to-10` — merged, but it is the ROADMAP_TO_10 working
branch and the cobweb-hunt reference point (`cf319b7`); delete after the roadmap job closes.

## Delete (184 refs = ~92 branches × local+remote)
Everything below marked DELETE. Suggested cleanup after sign-off:
`git branch -d <local…>` / `git push origin --delete <remote…>` (or the forge's stale-branch UI).

| Branch | Last commit | vs origin/main | Recommendation | Why |
|---|---|---|---|---|
| `feat/roadmap-to-10` | 2026-07-06 | merged | **KEEP** | keep for now (see notes) |
| `main` | 2026-07-06 | merged | **KEEP** | active line |
| `origin/feat/roadmap-to-10` | 2026-07-07 | merged | **KEEP** | keep for now (see notes) |
| `dev` | 2026-07-08 | merged | **KEEP** | keep for now (see notes) |
| `origin/dev` | 2026-07-08 | merged | **KEEP** | keep for now (see notes) |
| `integration/audit-fixes` | 2026-07-11 | merged | **KEEP** | active line |
| `origin/integration/audit-fixes` | 2026-07-11 | merged | **KEEP** | active line |
| `origin/main` | 2026-07-11 | merged | **KEEP** | active line |
| `copilot/fix-google-account-management-issues` | 2026-05-06 | merged | **DELETE** | fully merged into origin/main |
| `feature/auto-signin-browser-llms` | 2026-05-06 | unmerged(+1) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/copilot/fix-google-account-management-issues` | 2026-05-06 | rebased-equal | **DELETE** | all commits patch-equivalent in main (rebased) |
| `origin/feature/auto-signin-browser-llms` | 2026-05-06 | unmerged(+1) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `backup/current-changes` | 2026-05-08 | merged | **DELETE** | fully merged into origin/main |
| `copilot/fix-message-bubble-rendering` | 2026-05-08 | rebased-equal | **DELETE** | all commits patch-equivalent in main (rebased) |
| `feat/google-account-auto-management` | 2026-05-08 | merged | **DELETE** | fully merged into origin/main |
| `feat/structured-ui-observation` | 2026-05-08 | merged | **DELETE** | fully merged into origin/main |
| `origin/backup/current-changes` | 2026-05-08 | rebased-equal | **DELETE** | all commits patch-equivalent in main (rebased) |
| `origin/copilot/fix-message-bubble-rendering` | 2026-05-08 | rebased-equal | **DELETE** | all commits patch-equivalent in main (rebased) |
| `origin/feat/google-account-auto-management` | 2026-05-08 | rebased-equal | **DELETE** | all commits patch-equivalent in main (rebased) |
| `origin/feat/structured-ui-observation` | 2026-05-08 | rebased-equal | **DELETE** | all commits patch-equivalent in main (rebased) |
| `add-shell-validation-tests-13805362173993612366` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `add-voice-provider-tests-10995176007081852664` | 2026-05-11 | unmerged(+3) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `code-health-agent-config-12667480930388999630` | 2026-05-11 | unmerged(+3) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `code-health-ui-unused-import-15511231762356134659` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `code-health/mcp-agent-context-7813515382934265982` | 2026-05-11 | unmerged(+3) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `code-health/remove-unused-annotations-import-4857753180509290641` | 2026-05-11 | unmerged(+1) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `fix-bridge-payload-collector-fallback-10956322009130523731` | 2026-05-11 | unmerged(+3) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `fix-ci-errors-5585101849547000812` | 2026-05-11 | unmerged(+1) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `fix-command-injection-9487283749927652789` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `fix-insecure-deserialization-maya1-4962077343059917735` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `fix-unused-annotations-import-11153612719454816483` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `fix-unused-import-annotations-web-14566045881817964452` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `fix-unused-import-server-14810939692847314160` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `fix-unused-import-tempfile-18170295162563307493` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `fix-unused-tempfile-import-3055562467991149586` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `fix-voice-trust-remote-code-11049077988255828864` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `fix/context-loader-complexity-2263890105479300603` | 2026-05-11 | unmerged(+1) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `fix/insecure-deserialization-voice-3618883225884889181` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `fix/mcp-server-unused-import-2941723988872264835` | 2026-05-11 | unmerged(+1) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `fix/mcp-shell-unused-import-11511611716024324545` | 2026-05-11 | unmerged(+3) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `fix/observe-ui-and-cancel` | 2026-05-11 | merged | **DELETE** | fully merged into origin/main |
| `fix/shell-command-injection-11336654437385716255` | 2026-05-11 | unmerged(+1) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `fix/tray-ui-remove-unused-import-7732858649439208633` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `fix/unused-imports-search-py-7572639272740665074` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `jules-15192333453776767884-77b55827` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `jules-7435730859451402790-2f256aeb` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `jules-7772397845275025536-260a4b8c` | 2026-05-11 | unmerged(+1) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `mcp-logger-async-performance-2990268782794505300` | 2026-05-11 | unmerged(+3) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/add-shell-validation-tests-13805362173993612366` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/add-voice-provider-tests-10995176007081852664` | 2026-05-11 | unmerged(+3) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/code-health-agent-config-12667480930388999630` | 2026-05-11 | unmerged(+3) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/code-health-ui-unused-import-15511231762356134659` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/code-health/mcp-agent-context-7813515382934265982` | 2026-05-11 | unmerged(+3) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/code-health/remove-unused-annotations-import-4857753180509290641` | 2026-05-11 | unmerged(+1) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/fix-bridge-payload-collector-fallback-10956322009130523731` | 2026-05-11 | unmerged(+3) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/fix-ci-errors-5585101849547000812` | 2026-05-11 | unmerged(+1) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/fix-command-injection-9487283749927652789` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/fix-insecure-deserialization-maya1-4962077343059917735` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/fix-unused-annotations-import-11153612719454816483` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/fix-unused-import-annotations-web-14566045881817964452` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/fix-unused-import-server-14810939692847314160` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/fix-unused-import-tempfile-18170295162563307493` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/fix-unused-tempfile-import-3055562467991149586` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/fix-voice-trust-remote-code-11049077988255828864` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/fix/context-loader-complexity-2263890105479300603` | 2026-05-11 | unmerged(+1) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/fix/insecure-deserialization-voice-3618883225884889181` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/fix/mcp-server-unused-import-2941723988872264835` | 2026-05-11 | unmerged(+1) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/fix/mcp-shell-unused-import-11511611716024324545` | 2026-05-11 | unmerged(+3) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/fix/observe-ui-and-cancel` | 2026-05-11 | rebased-equal | **DELETE** | all commits patch-equivalent in main (rebased) |
| `origin/fix/shell-command-injection-11336654437385716255` | 2026-05-11 | unmerged(+1) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/fix/tray-ui-remove-unused-import-7732858649439208633` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/fix/unused-imports-search-py-7572639272740665074` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/jules-15192333453776767884-77b55827` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/jules-7435730859451402790-2f256aeb` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/jules-7772397845275025536-260a4b8c` | 2026-05-11 | unmerged(+1) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/mcp-logger-async-performance-2990268782794505300` | 2026-05-11 | unmerged(+3) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/perf-session-store-cache-16941632223666314072` | 2026-05-11 | unmerged(+3) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/perf/async-file-read-6997450105755457566` | 2026-05-11 | unmerged(+3) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/perf/async-logger-16691899593792255881` | 2026-05-11 | unmerged(+4) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/refactor-discover-instruction-files-1681875843799169463` | 2026-05-11 | unmerged(+3) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/refactor-ui-observe-389543495526731340` | 2026-05-11 | unmerged(+3) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/refactor/claw-relay-complexity-5357875946927300782` | 2026-05-11 | unmerged(+3) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/review-architecture-1357881849632957490` | 2026-05-11 | rebased-equal | **DELETE** | all commits patch-equivalent in main (rebased) |
| `origin/test-improvement-voice-providers-2340809393654306911` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/test-mcp-server-config-path-validation-17246552865581184874` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/test-mcp-server-config-path-validation-4940287285207477593` | 2026-05-11 | unmerged(+1) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/voice-error-handling-tests-17488843891539140361` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `origin/voice-tests-7295266130377923658` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `perf-session-store-cache-16941632223666314072` | 2026-05-11 | unmerged(+3) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `perf/async-file-read-6997450105755457566` | 2026-05-11 | unmerged(+3) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `perf/async-logger-16691899593792255881` | 2026-05-11 | unmerged(+4) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `refactor-discover-instruction-files-1681875843799169463` | 2026-05-11 | unmerged(+3) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `refactor-ui-observe-389543495526731340` | 2026-05-11 | unmerged(+3) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `refactor/claw-relay-complexity-5357875946927300782` | 2026-05-11 | unmerged(+3) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `review-architecture-1357881849632957490` | 2026-05-11 | rebased-equal | **DELETE** | all commits patch-equivalent in main (rebased) |
| `test-improvement-voice-providers-2340809393654306911` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `test-mcp-server-config-path-validation-17246552865581184874` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `test-mcp-server-config-path-validation-4940287285207477593` | 2026-05-11 | unmerged(+1) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `voice-error-handling-tests-17488843891539140361` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `voice-tests-7295266130377923658` | 2026-05-11 | unmerged(+2) | **DELETE** | stale pre-restructure work (May); superseded by later campaigns |
| `feature/design-mocks-integration` | 2026-05-12 | merged | **DELETE** | fully merged into origin/main |
| `feature/ui-v7-hybrid` | 2026-05-12 | merged | **DELETE** | fully merged into origin/main |
| `feature/browser-reliability-applied` | 2026-05-15 | merged | **DELETE** | fully merged into origin/main |
| `codex-implementation` | 2026-05-19 | merged | **DELETE** | fully merged into origin/main |
| `ai-architecture-restructure` | 2026-05-24 | unmerged(+26) | **DELETE** | dangling CI-nit commits on a 3-week-old base; content superseded |
| `ai-architecture-restructure-fixed` | 2026-05-24 | merged | **DELETE** | fully merged into origin/main |
| `manual-test-ai-restructure` | 2026-05-24 | merged | **DELETE** | fully merged into origin/main |
| `origin/ai-architecture-restructure` | 2026-05-24 | unmerged(+26) | **DELETE** | dangling CI-nit commits on a 3-week-old base; content superseded |
| `origin/ai-architecture-restructure-fixed` | 2026-05-24 | rebased-equal | **DELETE** | all commits patch-equivalent in main (rebased) |
| `kim-improvement` | 2026-06-10 | merged | **DELETE** | fully merged into origin/main |
| `origin/kim-improvement` | 2026-06-10 | rebased-equal | **DELETE** | all commits patch-equivalent in main (rebased) |
| `origin/production-roadmap` | 2026-06-11 | rebased-equal | **DELETE** | all commits patch-equivalent in main (rebased) |
| `production-roadmap` | 2026-06-11 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/backend-plumbing` | 2026-06-19 | unmerged(+1) | **DELETE** | dangling CI-nit commits on a 3-week-old base; content superseded |
| `origin/fix/cli-agentic-chat` | 2026-06-19 | unmerged(+1) | **DELETE** | dangling CI-nit commits on a 3-week-old base; content superseded |
| `origin/fix/cli-p0-persistence` | 2026-06-19 | unmerged(+1) | **DELETE** | dangling CI-nit commits on a 3-week-old base; content superseded |
| `origin/fix/cli-polish` | 2026-06-19 | unmerged(+1) | **DELETE** | dangling CI-nit commits on a 3-week-old base; content superseded |
| `origin/fix/cli-ux` | 2026-06-19 | unmerged(+1) | **DELETE** | dangling CI-nit commits on a 3-week-old base; content superseded |
| `origin/fix/frontend-p0` | 2026-06-19 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/frontend-p2` | 2026-06-19 | unmerged(+1) | **DELETE** | dangling CI-nit commits on a 3-week-old base; content superseded |
| `origin/fix/installer` | 2026-06-19 | unmerged(+1) | **DELETE** | dangling CI-nit commits on a 3-week-old base; content superseded |
| `origin/fix/sandbox-hardening` | 2026-06-19 | unmerged(+1) | **DELETE** | dangling CI-nit commits on a 3-week-old base; content superseded |
| `origin/fix/session-ux` | 2026-06-19 | unmerged(+1) | **DELETE** | dangling CI-nit commits on a 3-week-old base; content superseded |
| `origin/fix/speed-access` | 2026-06-19 | unmerged(+2) | **DELETE** | dangling CI-nit commits on a 3-week-old base; content superseded |
| `origin/fix/trust-features` | 2026-06-19 | unmerged(+2) | **DELETE** | dangling CI-nit commits on a 3-week-old base; content superseded |
| `pr-2` | 2026-06-20 | merged | **DELETE** | fully merged into origin/main |
| `fix/browser-provider-bugs` | 2026-06-22 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/browser-provider-bugs` | 2026-06-22 | merged | **DELETE** | fully merged into origin/main |
| `ui-wip` | 2026-06-23 | merged | **DELETE** | fully merged into origin/main |
| `fix/browser-2nd-turn-hang` | 2026-06-28 | merged | **DELETE** | fully merged into origin/main |
| `audit-fixes` | 2026-06-29 | merged | **DELETE** | fully merged into origin/main |
| `origin/audit-fixes` | 2026-06-29 | merged | **DELETE** | fully merged into origin/main |
| `fix/app-codex-wrapper` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `fix/browser-web` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `fix/bugsweep-cli` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `fix/bugsweep-fe` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `fix/bugsweep-mcp` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `fix/bugsweep-orch` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `fix/bugsweep-tauri` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `fix/cli-browser-bridge-freshchat` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `fix/cobweb-plumbing` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `fix/desktop-frontend-ux` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `fix/desktop-rust-webview` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `fix/orch-core` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `fix/os-tools-xplat` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `fix/py-godfile-safety` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `fix/rust-godfile` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `fix/secret-sandbox-bypass` | 2026-07-06 | rebased-equal | **DELETE** | all commits patch-equivalent in main (rebased) |
| `fix/tests-scripts-ci` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `integration/bugsweep-all` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `integration/waveA` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `integration/waveB` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `origin/feat/browser-stateful-threads` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/app-codex-wrapper` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/browser-web` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/bugsweep-cli` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/bugsweep-fe` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/bugsweep-mcp` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/bugsweep-orch` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/bugsweep-tauri` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/cli-browser-bridge-freshchat` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/cobweb-plumbing` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/desktop-frontend-ux` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/desktop-rust-webview` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/orch-core` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/os-tools-xplat` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/py-godfile-safety` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/rust-godfile` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/secret-sandbox-bypass` | 2026-07-06 | rebased-equal | **DELETE** | all commits patch-equivalent in main (rebased) |
| `origin/fix/tests-scripts-ci` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `origin/integration/bugsweep-all` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `worktree-agent-a10fb213c968e467d` | 2026-07-06 | merged | **DELETE** | fully merged into origin/main |
| `feat/browser-stateful-threads` | 2026-07-07 | merged | **DELETE** | fully merged into origin/main |
| `fix/codex-followups` | 2026-07-07 | merged | **DELETE** | fully merged into origin/main |
| `fix/run-identity` | 2026-07-07 | merged | **DELETE** | fully merged into origin/main |
| `integration/final` | 2026-07-07 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/codex-followups` | 2026-07-07 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/run-identity` | 2026-07-07 | merged | **DELETE** | fully merged into origin/main |
| `fix/audit-build-ci` | 2026-07-10 | merged | **DELETE** | fully merged into origin/main |
| `fix/audit-cli-crate` | 2026-07-10 | merged | **DELETE** | fully merged into origin/main |
| `fix/audit-frontend` | 2026-07-10 | merged | **DELETE** | fully merged into origin/main |
| `fix/audit-mcp-server` | 2026-07-10 | merged | **DELETE** | fully merged into origin/main |
| `fix/audit-tauri-desktop` | 2026-07-10 | merged | **DELETE** | fully merged into origin/main |
| `fix/audit-web-codexengine` | 2026-07-10 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/audit-build-ci` | 2026-07-10 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/audit-cli-crate` | 2026-07-10 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/audit-frontend` | 2026-07-10 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/audit-mcp-server` | 2026-07-10 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/audit-tauri-desktop` | 2026-07-10 | merged | **DELETE** | fully merged into origin/main |
| `origin/fix/audit-web-codexengine` | 2026-07-10 | merged | **DELETE** | fully merged into origin/main |
| `fix/audit-orchestrator` | 2026-07-11 | rebased-equal | **DELETE** | all commits patch-equivalent in main (rebased) |
| `origin/fix/audit-orchestrator` | 2026-07-11 | rebased-equal | **DELETE** | all commits patch-equivalent in main (rebased) |
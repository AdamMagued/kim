# TEAM E — Wave 1 findings (Rust CLI `cli/` + `kimctl/`)

Baseline: `integration/audit-fixes`. Read-only hunt. Format per OPERATION_GOOGLE_LEVEL §3.
Status: IN PROGRESS — preliminary batch committed early for resilience; findings are
appended (and re-severity-sorted at final pass) as the hunt proceeds.

## F-E-1: `kim doctor` always exits 0, even when every check fails
- **File:** cli/src/main.rs:289-297, cli/src/commands.rs:385-458
- **Severity:** Medium
- **Class:** bug | contract
- **Evidence:** `doctor()` builds human-readable status lines (`python: NOT FOUND`,
  `Ollama server: unreachable`, `Kim desktop bridge: not running`, model "not in the
  known list", etc.) and always returns `CommandOutcome::Message(...)`. `main()` prints
  it and falls through to `Ok(())` → exit 0. Concrete trigger: `kim doctor` on a machine
  with no python/codex/bridge → prints failures, exits 0. Install scripts and CI
  (`kim doctor && ...`) cannot gate on it. The unexpected-outcome arm
  (`other => eprintln!(...)`) also exits 0.
- **Fix sketch:** have `doctor()` return a pass/fail bit (or scan for known failure
  markers); `std::process::exit(1)` when any required check fails. Keep 0 when only
  optional checks (cargo, provider-specific) fail, or add `--strict`.
- **Cross-territory?** no

## F-E-2: unknown flags/subcommands silently start a fresh REPL session
- **File:** cli/src/main.rs:85-132 (`parse_cli_args`)
- **Severity:** Medium
- **Class:** bug
- **Evidence:** anything that isn't `chat|code|doctor|--help|--version|--resume` falls
  into `CliCommand::Repl { resume_id: None }`. Concrete triggers: `kim resume latest`
  (missing dashes), `kim --continue`, `kim --resum latest` (typo), `kim login` — all
  silently open a brand-new interactive session instead of erroring. This is the same
  bug class as the fixed #6 (bare trailing `--resume`), one level up: the parser has no
  "unknown argument" rejection at all. A typo'd `--resume` loses the user's intent to
  resume and scatters a new session file. Also: `kim chat --resume abc` treats
  `--resume abc` as prompt text and sends it to the model.
- **Fix sketch:** reject any unconsumed `--flag` (and any non-subcommand bare word)
  with a UsageError (exit 2), mirroring the existing `--resume`-without-value handling.
- **Cross-territory?** no

## F-E-3: every session save rewrites all message timestamps to "now"
- **File:** cli/src/sessions.rs:125-143 (`save_session_messages_in`)
- **Severity:** Low
- **Class:** bug
- **Evidence:** the whole transcript is re-serialized on every save with a single
  `timestamp_ms = now` stamped onto every record. A 2-hour conversation saved after its
  last turn shows every message created at the final save instant; original per-message
  times are unrecoverable. Any future feature (or the desktop app reading these files)
  that trusts `timestamp_ms` gets fiction.
- **Fix sketch:** carry an optional `timestamp_ms` on `UiMessage`, set at push time,
  preserved on load and re-save; only stamp `now` for messages that lack one.
- **Cross-territory?** no

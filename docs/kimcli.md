# kimcli

`kimcli` is Kim's rebranded, version-pinned build of the `codex` CLI
(0.144.3), used as the backend engine for Kim's Code tab / `kim tui`. It is
built from a fork at [`AdamMagued/codex`](https://github.com/AdamMagued/codex),
branch `kim-brand-0.144.3` — 7 branding-only commits on top of upstream
`codex-cli` 0.144.3 (name/binary/version-string changes; no behavioral
changes to the agent loop or protocol). Apache-2.0 licensed, same as upstream.

Version string: `kimcli 0.144.3 (rebranded codex-cli 0.144.3)`.

## Why a fork instead of using `codex` directly

Kim ships a consistent "Kim" brand end-to-end (desktop app, `kim` CLI,
`kim tui`). Shelling out to a binary that prints `codex` in its own `--help`,
version string, and update-nagging would leak the wrong product name into a
user-facing surface. The fork changes only branding/naming; it tracks
upstream codex-cli's actual agent behavior and wire protocol exactly at
0.144.3.

## How routing works

Kim's Codex bridge (`codex_engine/engine.py` `_CodexProxy`, `orchestrator/codex_bridge_service.py`) picks a wire mode per provider:

- **`browser-contract`** (default) — for `browser:*` providers (and bare
  `"browser"`). Codex's model traffic is translated to/from Kim's
  browser-JSON contract on both `/v1/responses` and (legacy) `/v1/chat/completions`.
  This is kimcli's primary path: `BrowserProvider.complete()` speaks the
  `/v1/responses` JSON contract rather than native tool-calling.
- **`responses-passthrough`** — for API providers (Claude, Gemini, DeepSeek,
  Ollama-behind-proxy). codex-cli 0.144.3 removed the chat-completions wire
  API entirely (`WireApi` in `codex-rs/model-provider-info/src/lib.rs` only
  has the `Responses` variant now), so **codex 0.144.3 is Responses-wire-only**
  — these providers are served natively on `/v1/responses` with no
  chat-completions fallback. See `codex_engine/responses_passthrough.py`.
- **`ollama direct`** — Ollama can also be driven directly (no browser
  proxy hop) when configured that way; see `orchestrator/providers/ollama.py`.

`codex_engine/standalone_proxy.py` is the standalone kimcli entry point (used
by `kim tui` and the standalone `kimcli` binary path): it auto-selects
`browser-contract` vs `responses-passthrough` per the rules above ("auto"
mode).

## Install

```bash
./scripts/install_kimcli.sh
```

Mirrors `scripts/install-kim.sh`'s conventions: platform/arch detection
(`aarch64-apple-darwin`, `x86_64-apple-darwin`, `x86_64-unknown-linux-musl`,
`x86_64-pc-windows-msvc`), hard-fail `.sha256` checksum verification (escape
hatch: `KIM_SKIP_CHECKSUM=1`, not recommended), extraction to
`~/.kim/bin/kimcli`, quarantine-strip on macOS, `chmod +x`.

Env overrides:

| Var | Default | Notes |
|---|---|---|
| `KIMCLI_RELEASE_REPO` | `AdamMagued/codex` | Fork to install from |
| `KIMCLI_VERSION` | `kimcli-v0.144.3` | **Pinned tag — never `latest`.** kimcli tracks an exact upstream version deliberately; a rolling install would silently drift Kim's Code tab onto an unvalidated protocol version. |

After install, either add `~/.kim/bin` to `PATH`, or point Kim explicitly at
the binary:

```bash
export CODEX_BIN="$HOME/.kim/bin/kimcli"
```

Binary resolution order (both the Python orchestrator and the Rust desktop
shell implement the same chain): `CODEX_BIN` env → `~/.kim/bin/kimcli` →
`kimcli` on `PATH` → `codex` on `PATH` → bare `"codex"` last resort. See
`codex_engine/binary_resolver.py` and `desktop/src-tauri/src/binary_resolver.rs`.

## How to launch

- `kim tui` — Kim CLI's launcher for the full Codex-style TUI, routed
  through Kim's providers.
- `kimcli` — the standalone binary directly (once installed and on `PATH`,
  or via `$CODEX_BIN`).

## Version-bump playbook

When upstream `codex-cli` ships a new pinned version (`rust-vNEW`):

1. Rebase the `kim-brand-0.144.3`-style branding branch onto `rust-vNEW` in
   the `AdamMagued/codex` fork.
2. Re-grep for branding strings (`codex-cli`, `codex `, version literals,
   update-check URLs) to make sure the rebase didn't reintroduce upstream
   naming in a new file.
3. Verify the updater-kill patch still applies (kimcli must never
   self-update or nag about updates — Kim owns the version pin).
4. Tag the fork `kimcli-vNEW`; the fork's release workflow publishes
   `kimcli-<target>.tar.gz`/`.zip` + `.sha256` archives per target.
5. Regenerate the app-server schema snapshot:
   `kimcli app-server generate-json-schema --out codex_engine/appserver_schema`,
   diff, bump `codex_engine/appserver_schema/VERSION`.
6. Bump the pins: `.github/workflows/nightly-contract.yml`'s
   `real-kimcli-appserver` job (release tag + version-string assertion) and
   `scripts/install_kimcli.sh`'s `KIMCLI_VERSION` default.
7. Re-run the full feature matrix (`python -m pytest tests/ -q`, desktop
   Rust/TS suites, a live `kim tui` smoke) before merging.

## Licensing / attribution

`kim-pro` itself is MIT licensed. `kimcli` is a separately-licensed
Apache-2.0 binary that kim-pro *downloads* (via `scripts/install_kimcli.sh`
or the release CI job) rather than vendors — no Apache-2.0 source lives in
this repository. Each release archive includes upstream's `NOTICE` file per
Apache-2.0 §4(d).

## Known caveats

- **`kimcli app-server daemon` managed-install path**: this subcommand
  retains upstream codex's managed-install behavior for its
  remote-control/IDE-extension feature, which can download a *separate*
  upstream `codex` binary under its own management directory. This does
  **not** overwrite or affect the `kimcli` binary Kim installed — it's a
  distinct, upstream-owned artifact. Kim does not currently invoke
  `app-server daemon`; tracked as a policy question for phase 2 (see the
  follow-up issue).
- **TUI `FREE_GO_TOOLTIP`** still shows upstream's "included in your plan"
  wording in some flows — this string wasn't caught by the branding grep
  pass. Pending an owner decision on rewording vs. suppressing it.
- **Unsigned binaries**: kimcli release binaries are not code-signed:
  macOS needs the quarantine-strip step in the installer (already handled);
  Windows may show a SmartScreen prompt on first run.
- **Windows release leg**: the fork's release workflow builds
  `x86_64-pc-windows-msvc` on a best-effort, continue-on-error basis — don't
  treat a red Windows leg alone as blocking.
- **Approvals**: Kim's MCP tool-approval tiers (risk-gated tool calls) have
  no broker wired into the app-server transport under kimcli today — approval
  requests default-deny without one. Phase 2 tracks wiring these through as
  MCP elicitations (`mcpServer/elicitation/request`); see the follow-up issue
  for the design pointer.

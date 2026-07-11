# Team C — Wave 1 findings (mcp_server/ safety gates, round 3)

Branch: `integration/audit-fixes`. Read-only hunt of `mcp_server/` (server, policy,
config, tool_registry, tools/, sites/). Format mirrors §3 / inherited.md: most severe first.

---

## F-C-1: `git -c` config-injection turns the allowlisted `git` binary into arbitrary RCE + absolute-path secret read (defeats S2 allowlist AND S3/validate_path sandbox)
- **Files:** mcp_server/policy.py:497-511 (`_git_escalations` / git risk arm), mcp_server/policy.py:341-356; mcp_server/tools/shell.py:64-81,458-498 (`_check_blocked` never inspects `git -c`); dispatch via mcp_server/tools/shell.py:501 (`run_command`, `git` allowlisted in policy `_ALLOWED_MUTATING`)
- **Severity:** CRITICAL
- **Class:** security / sandbox-escape
- **Evidence (confirmed end-to-end, not theoretical):**
  Git executes `!`-prefixed aliases and several config keys (`core.fsmonitor`,
  `core.pager`, `core.editor`, `core.sshCommand`, `gpg.program`, …) as **arbitrary
  shell commands**. All of these can be injected on the command line with `git -c
  KEY=VALUE`, so an allowlisted `git` invocation becomes an arbitrary-command
  executor — completely defeating the S2 positive shell allowlist whose entire
  purpose is to bound *which binaries* can run.
  - Empirical (git 2.53.0, run in a fresh non-repo temp dir == the default
    `run_command` sandbox cwd, with `HOME` pointed at the sandbox):
    - `git -c 'alias.pwn=!echo PWNED_NOREPO_$(id -un)' pwn` → prints
      `PWNED_NOREPO_adammaged`. **Runs OUTSIDE any git repository** (the sandbox temp
      dir is not a repo) — so "not in a repo" is no protection.
    - `git -c "alias.exfil=!cat /ABS/secret" exfil` → prints the secret's contents
      even with `HOME` sandboxed to PROJECT_ROOT. Absolute paths ignore `HOME`, so the
      sandbox env buys nothing here.
  - Against the ACTUAL policy module (`mcp_server.policy.enforce` +
    `mcp_server.tools.shell._check_blocked`):
    - `run_command {cmd: "git -c 'alias.exfil=!cat /Users/adammaged/.ssh/id_rsa' exfil"}`
      → `_check_blocked` returns `None` (NOT blocked); `policy.enforce` returns
      **action=allow** (risk=high, but the risk-threshold arm is inert by default —
      see policy.py:83-115). Net: **dispatches and reads the SSH private key.**
    - `git -c 'alias.pwn=!id' pwn` → **allow**.
    - `git -c core.fsmonitor=/tmp/evil status` → **allow** (a second injection family:
      any config key git runs as a program).
  - Why the existing gates miss it:
    - `_git_escalations` (policy.py:341-356) only flags `push --force`,
      `reset --hard`, `clean -f`. It never inspects `-c`/`--config-env` or alias/pager/
      fsmonitor config, so no escalation fires.
    - `_scan_path_tokens` (policy.py:359-411) sees the alias value as ONE opaque token
      (`alias.exfil=!cat /abs/secret`); it does not start with `/`, `~`, or contain a
      `..` component, so `is_pathy` is False and the absolute secret path buried inside
      it is never handed to `validate_path`. The S3 secret-file sandbox is bypassed.
    - shell.py `_check_blocked` only denies by first-token basename (`git` is not in
      `_DENY_COMMANDS`) and by metachars. A payload with no `$()`/`` ` ``/`;`/`|`
      (e.g. `!cat /abs/secret`, `!/bin/sh /tmp/x`) sails through. (Only the
      `$(...)`-containing variant is caught, and only incidentally by the metachar
      regex — trivially avoided.)
- **Blast radius:** full arbitrary code execution as the user from a single
  `run_command`, and read of ANY absolute-path file including every secret the
  validate_path deny-list is meant to protect (`~/.ssh/id_rsa`, `.env`, cloud creds) —
  by absolute path, bypassing the filename/dir globs entirely. Works in the shipped
  default config (sandbox_mode on, hitl threshold unset). This is the headline
  allowlist-escape: `git` was allowlisted as "common local-dev binary" but is a
  general-purpose command runner.
- **Note on attribution:** the recovered lead pinned this on `mcp_server/tools/git.py`.
  The git.py MCP tool handlers are NOT the vector — they use fixed subcommands
  (`status`/`diff`/`add`/`commit`/`log`/`checkout`) via `create_subprocess_exec` and
  never accept `-c`/alias tokens. The real, reproducible vector is `run_command`
  allowlisting the `git` binary. (git.py is clean here; see trust-model section.)
- **Fix sketch:** treat `git` like the interpreters — add an argv escalation (or hard
  deny) in policy `_analyze_command_words`/`_git_escalations` for any `-c`/`--config`/
  `--config-env`/`-C`-with-config token, and specifically for config keys git executes
  as programs (`alias.*` values starting with `!`, `core.fsmonitor`, `core.pager`,
  `core.editor`, `core.sshCommand`, `sequence.editor`, `diff.external`,
  `gpg.program`, `credential.helper`, `uploadpack.packObjectsHook`, …). Simplest
  robust option: escalate/deny ANY `git -c`/`--config*` from `run_command` and route
  legitimate needs through the structured git.py tools. Also make `_scan_path_tokens`
  aware that `-c KEY=VALUE` values can embed paths.
- **Cross-territory?** Team A (agent-side gate mirrors this vocabulary) + Team H
  (S2/S3 invariant docs — the allowlist "trust model" must state that allowlisting a
  program that can exec other programs is itself an escape).

## F-C-2: Allowlisted `awk` / `tar` (and class) execute arbitrary commands via their own exec features — same allowlist escape as F-C-1
- **Files:** mcp_server/policy.py:219-229 (`_ALLOWED_MUTATING` includes `awk`, `tar`, `sed`, `make`, `zip`, `unzip`), policy.py:493-513 (only `python/node/perl/ruby/sh…` get `_INLINE_EXEC_FLAGS` escalation); mcp_server/tools/shell.py:64-95 (`_DENY_COMMANDS`/`_DENY_PATTERNS` don't cover these)
- **Severity:** CRITICAL (same blast radius as F-C-1; separate root binaries)
- **Class:** security / allowlist-escape
- **Evidence (against the real policy module):**
  - `run_command {cmd: "awk 'BEGIN{system(\"id\")}'"}` → `_check_blocked`=None,
    `policy.enforce`=**allow**. `awk`'s `system()` / `"cmd"|getline` run arbitrary
    shell; `awk` is allowlisted and gets no inline-exec escalation. Also reads any
    file via `getline < "/abs/secret"` inside the program string (never a path token,
    so `_scan_path_tokens` can't see it).
  - `run_command {cmd: "tar -cf /dev/null --checkpoint=1 --checkpoint-action=exec=id ."}`
    → **allow**. GNU/bsd tar runs `--checkpoint-action=exec=<cmd>` as a shell command.
    The glued option value (`exec=id`) is not path-shaped, so no gate fires.
  - Same family, not all exhaustively fired but same mechanism: `make` (runs Makefile
    recipes), `zip -T --unzip-command`, `sed` GNU `e`/`s///e` (the `e` form is only
    incidentally denied when it *also* carries an absolute path arg), `find -fprintf`.
  - Contrast — the gate that DOES work: `perl -e 'system("id")'` → **approve**
    (`inline_interpreter_exec`). The interpreter escalation list is correct as far as
    it goes; the defect is that it enumerates interpreters by name and misses every
    other allowlisted binary that can spawn a shell.
- **Root cause:** the S2 allowlist assumes an allowlisted binary only does its
  nominal job. Several allowlisted "local-dev" binaries are general-purpose command
  runners. A positive allowlist of *program names* cannot by itself bound behaviour
  when the allowed programs can exec other programs.
- **Fix sketch:** add argv escalation rules for the known exec-capable allowlisted
  binaries (`awk` program strings containing `system`/`|getline`/`|& `; `tar
  --checkpoint-action`/`--to-command`/`--use-compress-program`; `sed` `-e`/`-f`
  scripts with `e`/`W`/`s///e`; `find -fprintf`; `zip/unzip -T/-TT/--*-command`), OR
  invert to a much smaller allowlist and route power tools through approve. Document
  the trust boundary either way.
- **Cross-territory?** Team A (mirror in agent gate) + Team H (allowlist trust-model doc).

## F-C-3: `gh auth token` / `gh auth status --show-token` exfiltrates the stored GitHub token via an allowlisted binary — bypasses the `~/.config/gh` sandbox
- **File:** mcp_server/policy.py:219-229 (`gh` in `_ALLOWED_MUTATING`); config.py:207 (`~/.config/gh` is in `_SENSITIVE_PATHS`, but only guards *path arguments*)
- **Severity:** HIGH
- **Class:** security / secret-disclosure
- **Evidence:** `run_command {cmd: "gh auth token"}` → `_check_blocked`=None,
  `policy.enforce`=**allow**. `gh auth token` prints the user's GitHub OAuth/PAT to
  stdout, and `gh auth status --show-token` does the same. The token lives in
  `~/.config/gh/hosts.yml` — which `validate_path` correctly denies as a path — but
  the `gh` binary reads it internally, so no path argument is ever inspected and the
  sandbox is bypassed. The agent (or a prompt-injected task) can read the token and
  then use it (or exfil it via a second allowed channel).
- **Fix sketch:** deny/escalate `gh auth token` and `gh auth status --show-token`
  (and `gh auth login`) as argv rules; more generally, credential-printing
  subcommands of allowlisted tools (`aws configure get`, `docker …`, `npm token`,
  `heroku auth:token`, …) are the same class as F-C-2/F-C-3 — a binary that can read
  its own secret store defeats the filename-based sandbox.
- **Cross-territory?** Team H (sandbox trust-model doc).

<!-- more findings appended below -->

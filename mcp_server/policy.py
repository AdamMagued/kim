"""
Kim MCP Server — tool-call policy engine (K1/S1–S3 of docs/ROADMAP_TO_10.md).

``enforce(name, args) -> PolicyDecision`` is called as the FIRST thing inside
``server.py:call_tool``, before dispatch, so that EVERY caller of the MCP
server (agent, CLI, codex bridge, future connectors) is gated by construction.

The decision ladder (worst outcome wins):

  deny     — the call never dispatches. Sources: sensitive/out-of-sandbox
             paths in ANY path-typed argument (S3, closes the
             ``cp ~/.ssh/id_rsa /tmp`` gap), the shell deny-set hit on the
             *resolved real binary* (defeats symlink-renames), and hard argv
             rules (``find -delete`` …).
  approve  — the call blocks on a human decision, default-deny on timeout.
             Sources: risk >= the configured HITL threshold (same vocabulary
             the agent-side gate used), and escalation argv rules: inline
             interpreter exec (``python -c`` / ``node -e`` / ``bash -c``),
             non-allowlisted or untrusted-location binaries (defeats
             copy-renames), destructive git flags, inline env assignments.
  allow    — dispatch. Shell commands whose resolved binary is in the
             read-only safe set are downgraded to low risk so a "high"
             threshold does not prompt for ``ls -la``.

The positive shell allowlist is the primary control (S2); the shell.py
denylist stays as defense-in-depth inside the handler.

Config knobs (config.yaml):
  hitl_risk_threshold: high|medium|low   — same key the orchestrator reads
  shell.allowlist_extra: [..]            — extra binaries treated as allowlisted
  shell.safe_extra: [..]                 — extra binaries treated as read-only
Env: KIM_HITL_RISK_THRESHOLD (config wins, matching agent_config semantics).
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

from mcp_server.config import PROJECT_ROOT, get_config, validate_path
from mcp_server.tools.shell import _DENY_COMMANDS

logger = logging.getLogger(__name__)

# Risk classification is shared with the orchestrator on purpose: one
# vocabulary for gate decisions on both sides of the MCP boundary.
from orchestrator.tool_risk import classify_tool_risk  # noqa: E402

_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class PolicyDecision:
    """Outcome of policy evaluation for one tool call."""

    action: str                 # "allow" | "deny" | "approve"
    risk: str = "low"           # effective risk after argv refinement
    reason: str = ""            # stable reason code
    message: str = ""           # model-facing text (deny) / context (approve)
    preview: str = ""           # human-readable approval-card preview
    signature: str = ""         # acceptForSession cache key
    escalations: tuple = ()     # argv rules that fired


def _deny(risk: str, reason: str, message: str) -> PolicyDecision:
    return PolicyDecision(action="deny", risk=risk, reason=reason, message=message)


# ---------------------------------------------------------------------------
# Threshold resolution — mirrors orchestrator.agent_config._resolve_hitl_threshold
# (config key wins over env; anything not high/medium/low disables the gate).
# Read lazily so tests can monkeypatch the environment.
# ---------------------------------------------------------------------------

def hitl_threshold() -> str | None:
    # NOTE (G3): with neither config.yaml `hitl_risk_threshold` nor
    # KIM_HITL_RISK_THRESHOLD set, this returns None and the risk-threshold
    # arm of the gate is INERT — only argv escalation rules (and hard denies)
    # gate calls. Set the key to "high" to require approval for high-risk
    # tools (write_file, delete_file, run_python, mouse/keyboard, …).
    raw = get_config().get("hitl_risk_threshold") or os.environ.get(
        "KIM_HITL_RISK_THRESHOLD"
    )
    if raw is None:
        return None
    normalized = str(raw).strip().lower()
    return normalized if normalized in ("high", "medium", "low") else None


# ---------------------------------------------------------------------------
# S3 — path-typed tool arguments, validated at the chokepoint.
# Handlers validate most of these too; the table here makes the sandbox hold
# even if a handler forgets, and extends coverage to shell command arguments.
# ---------------------------------------------------------------------------

_PATH_ARGS: dict[str, tuple[str, ...]] = {
    "read_file": ("path",),
    "write_file": ("path",),
    "delete_file": ("path",),
    "list_dir": ("path",),
    "run_python": ("file", "cwd"),
    "run_node": ("file", "cwd"),
    "lint_file": ("path", "cwd"),
    "run_command": ("cwd",),
    "git_status": ("cwd",),
    "git_diff": ("path", "cwd"),
    "git_add": ("paths", "cwd"),
    "git_commit": ("cwd",),
    "git_log": ("cwd",),
    "git_checkout": ("cwd",),
    "search_in_files": ("path",),
    "find_files": ("path",),
    "write_memory": ("cwd",),
    "read_memory": ("cwd",),
}

# Absolute paths that shell arguments may always reference.
_SAFE_DEV_PATHS = frozenset({
    "/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin",
    "/dev/tty", "/dev/urandom", "/dev/zero",
})


def _check_one_path(value: str) -> str | None:
    """validate_path with ~ expansion. Returns an error string or None."""
    text = str(value).strip()
    if not text:
        return None
    try:
        validate_path(str(Path(text).expanduser()))
    except PermissionError as e:
        return str(e)
    return None


def _validate_path_args(name: str, args: dict) -> str | None:
    """Check every declared path-typed argument of the tool. None == OK."""
    for key in _PATH_ARGS.get(name, ()):
        raw = args.get(key)
        if raw is None:
            continue
        values = raw if isinstance(raw, (list, tuple)) else [raw]
        for value in values:
            err = _check_one_path(str(value))
            if err:
                return f"argument {key!r}: {err}"
    return None


# ---------------------------------------------------------------------------
# S2 — shell command allowlist + argv-level rules
# ---------------------------------------------------------------------------

# Read-only binaries: allowed AND risk-downgraded to low (no threshold prompt).
_SAFE_READONLY = frozenset({
    "ls", "pwd", "echo", "printf", "cat", "head", "tail", "wc", "grep",
    "egrep", "fgrep", "rg", "which", "whereis", "file", "stat", "du", "df",
    "date", "whoami", "id", "uname", "hostname", "basename", "dirname",
    "realpath", "readlink", "env", "printenv", "sort", "uniq", "cut", "tr",
    "comm", "diff", "cmp", "md5", "md5sum", "shasum", "sha1sum", "sha256sum",
    "tree", "jq", "column", "nl", "od", "xxd", "strings", "less", "more",
    "sleep", "true", "false", "test", "type", "find", "seq",
})

# git subcommands that are read-only queries → low risk.
_SAFE_GIT_SUBCOMMANDS = frozenset({
    "status", "diff", "log", "show", "ls-files", "rev-parse", "blame",
    "shortlog", "describe",
})

# Mutating but common local-dev binaries: allowed, still subject to the
# HITL risk threshold exactly like today's agent-side gate.
_ALLOWED_MUTATING = frozenset({
    "mkdir", "touch", "cp", "mv", "ln", "tee", "sed", "awk", "tar", "zip",
    "unzip", "gzip", "gunzip", "patch", "chmod", "git", "gh", "python",
    "python2", "python3", "node", "nodejs", "npm", "npx", "yarn", "pnpm",
    "pip", "pip2", "pip3", "uv", "cargo", "rustc", "rustup", "go", "gofmt",
    "make", "cmake", "ninja", "pytest", "tox", "ruff", "flake8", "black",
    "isort", "mypy", "pyright", "tsc", "eslint", "prettier", "vitest",
    "jest", "sqlite3", "open", "xdg-open", "xargs", "sudo", "doas", "env",
    "nohup", "nice", "time", "command", "sh", "bash", "zsh", "fish", "dash",
    "ksh", "perl", "ruby", "swift",
})

# POSIX shell builtins that have no on-disk binary on every platform. Harmless
# in a one-shot non-interactive /bin/sh (no chaining reaches a second command).
_SHELL_BUILTINS = frozenset({
    "cd", "export", "set", "unset", "ulimit", "umask", "exit", "read",
    ":", "[", "wait", "shift", "hash",
})

# System locations an *explicit-path* invocation may point into and still be
# treated as the genuine binary (e.g. /usr/bin/git). Anything else invoked by
# path (./tool, /tmp/tool, ~/tool) is an unverifiable copy → escalate.
_TRUSTED_BIN_PREFIXES = (
    "/usr/bin/", "/bin/", "/usr/sbin/", "/sbin/",
    "/usr/local/bin/", "/opt/homebrew/bin/", "/opt/local/bin/",
)

# Interpreter flags that turn an allowlisted binary into arbitrary inline code.
_INLINE_EXEC_FLAGS: dict[str, frozenset] = {
    "python": frozenset({"-c"}),
    "python2": frozenset({"-c"}),
    "python3": frozenset({"-c"}),
    "node": frozenset({"-e", "--eval", "-p", "--print"}),
    "nodejs": frozenset({"-e", "--eval", "-p", "--print"}),
    "perl": frozenset({"-e", "-E"}),
    "ruby": frozenset({"-e"}),
    "sh": frozenset({"-c"}),
    "bash": frozenset({"-c"}),
    "zsh": frozenset({"-c"}),
    "fish": frozenset({"-c"}),
    "dash": frozenset({"-c"}),
    "ksh": frozenset({"-c"}),
}

# Wrapper commands whose real command is the first non-option argument.
_WRAPPERS = frozenset({
    "env", "nohup", "nice", "time", "command", "xargs", "busybox",
})


def _shell_lists() -> tuple[frozenset, frozenset]:
    """(safe_readonly, allowed_mutating) with config.yaml extensions."""
    shell_cfg = get_config().get("shell", {}) or {}
    safe = _SAFE_READONLY | frozenset(
        str(x).lower() for x in shell_cfg.get("safe_extra", []) or []
    )
    mutating = _ALLOWED_MUTATING | frozenset(
        str(x).lower() for x in shell_cfg.get("allowlist_extra", []) or []
    )
    return safe, mutating


def _normalize_binary_name(base: str) -> str:
    """python3.13 → python, pip3 → pip, node18 → node (allowlist lookups)."""
    for stem in ("python", "pip", "node", "ruby", "perl"):
        if re.fullmatch(rf"{stem}[\d.]*", base):
            return stem
    return base


def _resolve_binary(token: str) -> tuple[str, str, bool]:
    """Resolve argv[0] to (invoked_basename, real_basename, trusted).

    - ``invoked_basename`` is what the user named (allowlist lookups use it,
      normalized) — npm stays npm even though its realpath is npm-cli.js.
    - ``real_basename`` is the realpath target's name: a symlink named ``ls``
      pointing at ``rm`` yields real_basename ``rm`` (deny-set lookups).
    - ``trusted``: plain names resolved through PATH are trusted; explicit
      paths (./tool, /tmp/tool, ~/tool) are trusted only when the real file
      lives in a system bin directory — a copied binary in the project dir is
      never trusted, whatever its name.
    """
    text = token.strip()
    if not text:
        return "", "", False
    is_explicit = ("/" in text) or ("\\" in text) or text.startswith("~")
    if is_explicit:
        invoked = Path(text).name.lower()
        try:
            real = os.path.realpath(str(Path(text).expanduser()))
        except (OSError, ValueError):
            return invoked, "", False
        real_base = Path(real).name.lower()
        trusted = real.startswith(_TRUSTED_BIN_PREFIXES)
        return invoked, real_base, trusted
    located = shutil.which(text)
    if located is None:
        # Not on PATH: either a builtin (checked by caller) or a command that
        # will fail at exec time — unverifiable, so not trusted.
        return text.lower(), text.lower(), False
    real = os.path.realpath(located)
    return text.lower(), Path(real).name.lower(), True


def _looks_like_assignment(token: str) -> bool:
    """True for VAR=value shell-assignment-shaped tokens."""
    eq = token.find("=")
    if eq <= 0 or token.startswith(("/", "\\", "-", ".", "~")):
        return False
    return token[:eq].replace("_", "a").isalnum()


def _split_assignments(tokens: list[str]) -> tuple[list[str], list[str]]:
    """Split leading VAR=value assignment tokens from the command tokens."""
    assignments: list[str] = []
    rest = list(tokens)
    while rest and _looks_like_assignment(rest[0]):
        assignments.append(rest[0])
        rest = rest[1:]
    return assignments, rest


def _git_escalations(tokens: list[str]) -> list[str]:
    """Destructive git argv patterns that need a human (roadmap ARG_RULES)."""
    words = [t for t in tokens[1:] if t != "--"]
    sub = next((w for w in words if not w.startswith("-")), "")
    flags = set(words)
    out: list[str] = []
    if sub == "push" and ({"--force", "-f"} & flags):
        out.append("git_force_push")
    if sub == "reset" and "--hard" in flags:
        out.append("git_reset_hard")
    if sub == "clean" and (
        "--force" in flags  # long form must escalate like -f/-fd (G1)
        or any(f.startswith("-") and "f" in f and not f.startswith("--") for f in flags)
    ):
        out.append("git_clean_force")
    return out


def _scan_path_tokens(tokens: list[str], cwd: str | None) -> str | None:
    """Deny shell arguments that reference sensitive or out-of-sandbox paths.

    Only tokens that are unambiguously path-shaped are checked: absolute,
    ``~``-prefixed, or containing a ``..`` component. Relative project paths,
    git refs (``origin/main``), URLs and sed exprs resolve inside the project
    root and pass untouched.
    """
    base_dir = Path(cwd) if cwd else PROJECT_ROOT
    for tok in tokens[1:]:
        if tok.startswith("-"):
            # Glued option values (`--file=/etc/shadow`, `-o=/abs/x`) still
            # carry a path — check the substring after the first '=' (G4).
            eq = tok.find("=")
            if eq == -1:
                continue
            candidate = tok[eq + 1:]
            if not candidate:
                continue
        else:
            candidate = tok
        # >>file / 2>/abs redirect prefixes are handled by shell.py; strip a
        # leading redirect so the target still gets path-checked here.
        candidate = candidate.lstrip("<>&123456789")
        if not candidate:
            continue
        expanded = Path(candidate).expanduser()
        is_pathy = (
            expanded.is_absolute()
            or candidate.startswith("~")
            or ".." in expanded.parts
        )
        if not is_pathy:
            continue
        if str(expanded) in _SAFE_DEV_PATHS:
            continue
        target = expanded if expanded.is_absolute() else base_dir / expanded
        try:
            validate_path(str(target))
        except PermissionError as e:
            return f"shell argument {tok!r}: {e}"
    return None


@dataclass(frozen=True)
class _ShellAnalysis:
    deny_reason: str = ""
    deny_message: str = ""
    escalations: tuple = ()
    effective_risk: str = "high"
    binary: str = ""


def _analyze_command_words(tokens: list[str], cwd: str | None, depth: int = 0) -> _ShellAnalysis:
    """Analyse one already-tokenized command (no chaining operators)."""
    if depth > 4:
        return _ShellAnalysis(escalations=("nesting_too_deep",))
    if not tokens:
        return _ShellAnalysis(effective_risk="low")

    escalations: list[str] = []
    assignments, rest = _split_assignments(tokens)
    if assignments:
        # Inline PATH=… (or any assignment) can redirect a plain name to an
        # attacker-controlled binary; a human gets to look at it first.
        escalations.append("inline_env_assignment")
    if not rest:
        return _ShellAnalysis(escalations=tuple(escalations), effective_risk="low")

    path_err = _scan_path_tokens(rest, cwd)
    if path_err:
        return _ShellAnalysis(
            deny_reason="sensitive_path_argument",
            deny_message=f"POLICY_DENIED: {path_err}",
        )

    invoked, real_base, trusted = _resolve_binary(rest[0])
    base = _normalize_binary_name(invoked)
    safe, mutating = _shell_lists()

    if real_base in _DENY_COMMANDS or invoked in _DENY_COMMANDS:
        return _ShellAnalysis(
            deny_reason="denylisted_binary",
            deny_message=(
                f"POLICY_DENIED: {rest[0]!r} resolves to blocked command "
                f"{(real_base if real_base in _DENY_COMMANDS else invoked)!r}"
            ),
        )

    if base == "find":
        for t in rest:
            if t in {"-delete", "-exec", "-execdir"}:
                return _ShellAnalysis(
                    deny_reason="find_destructive_flag",
                    deny_message=f"POLICY_DENIED: 'find {t}' is a blocked pattern",
                )

    if base in _SHELL_BUILTINS:
        return _ShellAnalysis(escalations=tuple(escalations), effective_risk="low")

    if base in _WRAPPERS or base in {"sudo", "doas"}:
        if base in {"sudo", "doas"}:
            escalations.append("privilege_escalation")
        # `env PATH=/tmp ls` sets the child env through the wrapper — flag any
        # assignment-shaped token the wrapper consumes, same as a leading one.
        wrapped = _wrapped_command(rest)
        skipped = rest[1:len(rest) - len(wrapped)] if wrapped else rest[1:]
        if any(_looks_like_assignment(t) for t in skipped):
            escalations.append("inline_env_assignment")
        if wrapped:
            inner = _analyze_command_words(wrapped, cwd, depth + 1)
            if inner.deny_reason:
                return inner
            escalations.extend(inner.escalations)
            return _ShellAnalysis(
                escalations=tuple(escalations),
                effective_risk=inner.effective_risk,
                binary=base,
            )
        return _ShellAnalysis(
            escalations=tuple(escalations), effective_risk="high", binary=base
        )

    inline_flags = _INLINE_EXEC_FLAGS.get(base)
    if inline_flags and any(t in inline_flags for t in rest[1:]):
        escalations.append("inline_interpreter_exec")

    if base == "git":
        escalations.extend(_git_escalations(rest))

    if base not in safe and base not in mutating:
        escalations.append("binary_not_allowlisted")
    elif not trusted:
        # Right name, wrong place: an allowlisted basename whose real file is
        # not the PATH-resolved system binary (e.g. `cp /bin/rm ./ls; ./ls`).
        escalations.append("untrusted_binary_location")

    if base in safe and not escalations:
        risk = "low"
    elif base == "git" and not escalations:
        words = [t for t in rest[1:] if not t.startswith("-")]
        risk = "low" if (words and words[0] in _SAFE_GIT_SUBCOMMANDS) else "high"
    else:
        risk = "high"
    return _ShellAnalysis(
        escalations=tuple(escalations), effective_risk=risk, binary=base
    )


def _wrapped_command(tokens: list[str]) -> list[str]:
    """Return the command tokens a wrapper (env/nohup/xargs/sudo…) executes."""
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t == "-I" and i + 1 < len(tokens):  # xargs -I PLACEHOLDER cmd …
            i += 2
            continue
        if t == "--":
            i += 1
            continue
        if t.startswith("-"):
            i += 1
            continue
        if "=" in t and not t.startswith(("/", "\\", "~", ".")):
            i += 1
            continue
        return tokens[i:]
    return []


_CHAIN_OPERATOR_TOKENS = frozenset({"&&", "||", ";", "|", "&"})


def _analyze_shell(cmd: str, cwd: str | None) -> _ShellAnalysis:
    """Analyse a full run_command string, segment by segment."""
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return _ShellAnalysis(
            deny_reason="malformed_quoting",
            deny_message="POLICY_DENIED: command has malformed shell quoting",
        )

    # Segment on chaining operator tokens so EVERY command in `a && b` is
    # vetted, not just the first (G6). shlex.split keeps whitespace-separated
    # `&&`/`||`/`;`/`|`/`&` as plain tokens.
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _CHAIN_OPERATOR_TOKENS:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(tok)
    segments.append(current)  # possibly empty: preserves empty-command handling

    escalations: list[str] = []
    worst_risk = "low"
    binary = ""
    for seg in segments:
        analysis = _analyze_command_words(seg, cwd)
        if analysis.deny_reason:
            return analysis
        escalations.extend(analysis.escalations)
        if _RISK_ORDER.get(analysis.effective_risk, 2) > _RISK_ORDER.get(worst_risk, 0):
            worst_risk = analysis.effective_risk
        if not binary:
            binary = analysis.binary

        # Recurse into `sh -c '…'` payloads so nested commands see the same rules.
        if seg:
            base = Path(seg[0]).name.lower()
            if base in {"sh", "bash", "zsh", "fish", "dash", "ksh"}:
                try:
                    c_index = seg.index("-c")
                except ValueError:
                    c_index = -1
                if 0 <= c_index < len(seg) - 1:
                    inner = _analyze_shell(seg[c_index + 1], cwd)
                    if inner.deny_reason:
                        return inner
                    escalations.extend(inner.escalations)
    return _ShellAnalysis(
        escalations=tuple(dict.fromkeys(escalations)),
        effective_risk=worst_risk,
        binary=binary,
    )


# ---------------------------------------------------------------------------
# Approval previews (moved here from agent.py — the server owns the gate now)
# ---------------------------------------------------------------------------

def build_approval_preview(name: str, args: dict) -> str:
    """Human-readable preview for the approval card."""
    args = args or {}
    try:
        if name in ("run_command", "shell", "execute_command"):
            return str(args.get("command") or args.get("cmd") or "").strip()
        if name == "run_powershell":
            return str(args.get("script", "")).strip()[:400]
        if name in ("write_file", "create_file", "edit_file"):
            path = str(args.get("path") or args.get("file_path") or "")
            new = str(args.get("content", ""))
            old = ""
            try:
                p = Path(path)
                if p.is_file():
                    old = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                old = ""
            import difflib

            diff = list(difflib.unified_diff(
                old.splitlines(), new.splitlines(),
                fromfile=f"{path} (current)", tofile=f"{path} (new)", lineterm="",
            ))
            if len(diff) > 40:
                diff = diff[:40] + ["… (diff truncated)"]
            return "\n".join(diff) if diff else f"(no textual change to {path})"
        if name.startswith("web_") or name in ("navigate", "click_element", "open_url"):
            url = str(args.get("url") or args.get("href") or "")
            label = str(
                args.get("label") or args.get("selector") or args.get("element_id") or ""
            )
            parts = [url]
            if label:
                parts.append(f"→ {label}")
            return " ".join(x for x in parts if x).strip()
        if name in ("delete_file", "run_python", "run_node", "lint_file"):
            return str(args.get("path") or args.get("file") or args.get("code", ""))[:400]
    except Exception as preview_err:
        logger.debug("build_approval_preview failed: %s", preview_err)
    return ""


# ---------------------------------------------------------------------------
# The chokepoint
# ---------------------------------------------------------------------------

def enforce(name: str, args: dict | None) -> PolicyDecision:
    """Evaluate one tool call. Never raises — unexpected errors deny."""
    args = args or {}
    try:
        return _enforce(name, args)
    except Exception as e:  # fail closed: a broken policy must not fail open
        logger.error("policy.enforce(%s) crashed: %s", name, e, exc_info=True)
        return _deny("high", "policy_error", f"POLICY_DENIED: policy error ({e})")


def _enforce(name: str, args: dict) -> PolicyDecision:
    risk_info = classify_tool_risk(name, args)
    risk = str(risk_info.get("level", "high"))
    reason = str(risk_info.get("reason", ""))

    # S3 — every declared path-typed argument goes through validate_path.
    path_err = _validate_path_args(name, args)
    if path_err:
        return _deny(risk, "sensitive_path_argument", f"POLICY_DENIED: {path_err}")

    escalations: tuple = ()
    signature = f"{name}"
    # G5: "accept for session" on a bare tool name would auto-approve EVERY
    # later call of that tool. Scope the cache key to the reviewed target
    # (path arguments) so approving write_file(a.txt) doesn't pre-approve
    # write_file(~/.zshrc).
    path_keys = _PATH_ARGS.get(name, ())
    if path_keys:
        discriminator = ",".join(
            f"{k}={args.get(k)}" for k in path_keys if args.get(k) is not None
        )
        if discriminator:
            signature = f"{name}:{discriminator}"
    if name == "run_command":
        analysis = _analyze_shell(str(args.get("cmd", "")), args.get("cwd"))
        if analysis.deny_reason:
            return _deny(risk, analysis.deny_reason, analysis.deny_message)
        escalations = analysis.escalations
        risk = analysis.effective_risk
        signature = f"run_command:bin={analysis.binary}"
        if escalations:
            # Escalated shell calls are only session-cacheable per exact command.
            signature = f"run_command:cmd={str(args.get('cmd', '')).strip()}"
            reason = escalations[0]
    elif name == "run_powershell":
        # G2: PowerShell scripts get no argv-level analysis here (the shell
        # allowlist/denylist grammar is POSIX-only), so a human always reviews
        # them instead of dispatching a whole unvetted script.
        escalations = ("powershell_script_unanalyzed",)
        reason = escalations[0]
        signature = f"run_powershell:script={str(args.get('script', '')).strip()}"

    threshold = hitl_threshold()
    over_threshold = bool(
        threshold
        and _RISK_ORDER.get(risk, 2) >= _RISK_ORDER.get(threshold, 99)
    )
    if escalations or over_threshold:
        return PolicyDecision(
            action="approve",
            risk=risk,
            reason=reason or (escalations[0] if escalations else "risk_threshold"),
            preview=build_approval_preview(name, args),
            signature=signature,
            escalations=escalations,
        )
    return PolicyDecision(action="allow", risk=risk, reason=reason, signature=signature)

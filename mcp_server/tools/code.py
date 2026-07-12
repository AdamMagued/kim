"""
Kim MCP Server — Code Execution Tools (Phase 6)

Provides code execution and linting tools:
  - run_python:  Execute a .py file or inline snippet
  - run_node:    Execute a .js file or inline snippet
  - lint_file:   Run ruff or flake8 on a Python file

Uses os_utils for cross-platform safety and availability checks.

Security layers (defence-in-depth, outermost first):
  1. HITL gate — run_python / run_node are HIGH risk in tool_risk.py; any
     hitl_risk_threshold >= high requires human approval before execution.
  2. Minimal environment — all subprocesses receive a restricted allowlist env
     that strips provider API keys (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.).
  3. OS sandbox — network is disabled via sandbox-exec (macOS) or bwrap (Linux)
     when the binary is available; logs a WARNING and continues without it
     (fail-open) so tests pass on boxes without these tools.
  4. Code blocklists — Python and Node pattern lists catch common dangerous
     calls; kept as defence-in-depth only, NOT the sole control.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
import os

from mcp_server.config import PROJECT_ROOT, CODE_TIMEOUT, SHELL_TIMEOUT, validate_path
from mcp_server.os_utils import IS_MACOS, IS_LINUX, IS_WINDOWS
from mcp_server.tools.shell import _kill_process_tree
from mcp_server.tools._errors import tool_error

logger = logging.getLogger(__name__)

# F-C-5: a model-supplied `timeout` must be clamped exactly like shell.py's
# MAX_SHELL_TIMEOUT_S. Without a ceiling, run_python(timeout=999999) pins the
# single-threaded MCP server for ~11.5 days server-side — the same client/server
# desync the shell clamp closes, reopened for code exec. Kept in sync with
# shell.MAX_SHELL_TIMEOUT_S (600s).
MAX_CODE_TIMEOUT_S = 600


def _clamp_code_timeout(raw: object, default: int) -> int:
    """Coerce a model-supplied timeout to a positive int within the hard cap."""
    try:
        val = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if val < 1:
        return 1
    if val > MAX_CODE_TIMEOUT_S:
        return MAX_CODE_TIMEOUT_S
    return val

# ── Minimal sandbox environment ───────────────────────────────────────────────

# /usr/local/bin is included so system-wide installs (e.g. node on macOS/Linux)
# stay reachable; /snap/bin on Linux for snap-installed interpreters (1.3).
_SANDBOX_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
if IS_LINUX and os.path.isdir("/snap/bin"):
    _SANDBOX_PATH += ":/snap/bin"


def _minimal_env(extra: dict | None = None) -> dict[str, str]:
    """Build a minimal allowlist env that strips provider API keys.

    All code-execution subprocesses use this instead of inheriting os.environ
    so that OPENAI_API_KEY, ANTHROPIC_API_KEY, and similar secrets are never
    visible to executed code (finding 1).

    On Windows the env must carry SystemRoot/ComSpec/Path (1.1): CPython
    cannot even initialise without SystemRoot, and a POSIX-style PATH makes
    every child binary unfindable. Mirrors shell.py's _sandbox_env.
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
        comspec = os.environ.get("ComSpec")
        if comspec:
            env["ComSpec"] = comspec
        pathext = os.environ.get("PATHEXT")
        if pathext:
            env["PATHEXT"] = pathext
        if extra:
            env.update(extra)
        return env
    env = {
        "PATH": _SANDBOX_PATH,
        "HOME": str(PROJECT_ROOT),
        "TMPDIR": tempfile.gettempdir(),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    lc_all = os.environ.get("LC_ALL", "")
    if lc_all:
        env["LC_ALL"] = lc_all
    if extra:
        env.update(extra)
    return env


# ── OS-level sandbox wrapping ─────────────────────────────────────────────────

def _sandbox_wrap_cmd(cmd: list[str]) -> list[str]:
    """Wrap cmd with an OS sandbox that disables network access (findings 2 & 3).

    macOS: sandbox-exec with a minimal seatbelt profile that allows everything
    except outbound/inbound network connections.

    Linux: bubblewrap (bwrap) with --unshare-net.

    Both paths are fail-open: if no sandbox binary is found the original cmd
    is returned unchanged and a loud WARNING is emitted so the CI log captures
    the degraded state.  The remaining layers (HITL gate + minimal env +
    blocklists) still apply.
    """
    if IS_MACOS and shutil.which("sandbox-exec"):
        # Network is denied; filesystem and process operations remain allowed
        # so the interpreter can read its own libraries.
        profile = "(version 1)(allow default)(deny network*)"
        return ["sandbox-exec", "-p", profile] + cmd

    if IS_LINUX and shutil.which("bwrap"):
        return [
            "bwrap",
            "--ro-bind", "/", "/",
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--unshare-net",
            "--die-with-parent",
        ] + cmd

    logger.warning(
        "code sandbox: no OS-level sandbox binary found (sandbox-exec / bwrap); "
        "running without network isolation — minimal-env + HITL + blocklist remain active"
    )
    return cmd


# ── Python code blocklist ─────────────────────────────────────────────────────

# WARNING: This blocklist is defence-in-depth only.  It is NOT the primary
# control — bypassable via importlib.import_module('os'), pathlib, urllib, etc.
# The OS sandbox (network disabled) and minimal env are the real controls.
# Patterns scan the *full* code string to prevent padding-based bypasses.
_CODE_BLOCKLIST = [
    re.compile(r"\bos\.system\b"),
    re.compile(r"\bsubprocess\b"),
    re.compile(r"__import__\s*\("),
    re.compile(r"\beval\s*\("),
    re.compile(r"\bexec\s*\("),
    # Catch `import os` / `import os as o` style alias bypasses
    re.compile(r"\bimport\s+os\b"),
    re.compile(r"\bfrom\s+os\b"),
]


def _check_code_blocked(code: str) -> str | None:
    """Scan *all* inline Python code for dangerous patterns (no truncation)."""
    for pat in _CODE_BLOCKLIST:
        if pat.search(code):
            return f"BLOCKED: Inline code contains blocked pattern '{pat.pattern}'"
    return None


# ── Node.js code blocklist ────────────────────────────────────────────────────

# Node has unblocked access to child_process, fs, net etc. via require/import
# unless explicitly denied (finding 3).  These patterns catch the straightforward
# forms; the OS sandbox (no network) and minimal env handle the rest.
_NODE_DANGEROUS_MODULES = (
    "child_process", "fs", "net", "http", "https",
    "dgram", "vm", "worker_threads", "os", "v8",
)
_NODE_MODULE_RE = "(?:" + "|".join(re.escape(m) for m in _NODE_DANGEROUS_MODULES) + ")"

_NODE_BLOCKLIST = [
    # require('child_process'), require("fs"), etc.
    re.compile(r"require\s*\(\s*['\"]" + _NODE_MODULE_RE + r"['\"]\s*\)"),
    # import x from 'child_process' / import('fs')
    re.compile(r"(?:import\b[^;'\"]*\bfrom\s*['\"]|import\s*\()" + _NODE_MODULE_RE + r"['\"]"),
    # process.binding() — low-level internal bindings bypass the module system
    re.compile(r"\bprocess\.binding\s*\("),
]


def _check_node_blocked(code: str) -> str | None:
    """Scan Node.js code for dangerous require/import patterns (no truncation)."""
    for pat in _NODE_BLOCKLIST:
        if pat.search(code):
            return f"BLOCKED: Node.js code contains blocked pattern '{pat.pattern}'"
    return None


# ── Interpreter resolution ────────────────────────────────────────────────────

def _find_python() -> str:
    """Return the absolute path of the best available Python executable.

    Resolved against the *real* PATH before it is restricted so the venv
    interpreter is found; the returned absolute path works even under the
    minimal env (finding 1 regression guard).
    """
    for name in ("python3", "python"):
        path = shutil.which(name)
        if path:
            return path
    return "python3"  # fallback: let it error naturally


def _find_node() -> str:
    """Return the absolute path of the best available Node.js executable."""
    for name in ("node", "nodejs"):
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError("OS_LIMITATION: node not installed")


# ── Core subprocess runner ────────────────────────────────────────────────────

async def _run_exec(
    cmd: list[str],
    cwd: str | None = None,
    timeout: int | None = None,
    extra_env: dict | None = None,
) -> str:
    """Run a command via create_subprocess_exec and return formatted output.

    Always uses a minimal allowlist environment (finding 1): caller's extra_env
    is merged ON TOP of the allowlist, never on top of os.environ.
    """
    resolved_cwd = cwd or str(PROJECT_ROOT)
    resolved_timeout = timeout or CODE_TIMEOUT

    logger.info(f"code exec: {' '.join(cmd)} (cwd={resolved_cwd})")

    # Always build the restricted env; extra_env overrides individual keys only.
    env = _minimal_env(extra_env)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=resolved_cwd,
            env=env,
            # F-C-6: own process group (POSIX) so a timeout kill reaps the whole
            # tree. An approved run_python *file* can Popen freely (the inline
            # blocklist is exempt for files, M4); without this those
            # grandchildren survive proc.kill() and accumulate orphaned.
            start_new_session=not IS_WINDOWS,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=resolved_timeout
            )
        except asyncio.TimeoutError:
            # F-C-6: kill the whole process group, not just the interpreter.
            _kill_process_tree(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                logger.warning("code exec process did not exit after kill")
            return f"TIMEOUT: command exceeded {resolved_timeout}s"
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        exit_code = proc.returncode

        parts = [f"exit_code: {exit_code}"]
        if out.strip():
            parts.append(f"stdout:\n{out}")
        if err.strip():
            parts.append(f"stderr:\n{err}")
        if not out.strip() and not err.strip():
            parts.append("(no output)")
        return "\n".join(parts)
    except FileNotFoundError:
        return tool_error(f"'{cmd[0]}' is not installed or not found on PATH.")
    except Exception as e:
        logger.error(f"code exec failed: {e}", exc_info=True)
        return tool_error(e)


# ── Tool handlers ─────────────────────────────────────────────────────────────

async def handle_run_python(args: dict) -> str:
    """
    Execute Python code.

    Risk tier: HIGH (== HITL-gated when hitl_risk_threshold >= high).
    Security layers: HITL gate → minimal env → OS sandbox → Python blocklist.

    Accepts either:
      - 'file': path to a .py file (relative to PROJECT_ROOT or absolute)
      - 'code': inline Python code snippet to execute

    If both are provided, 'file' takes priority.
    """
    file_path = args.get("file", "")
    code = args.get("code", "")
    cwd = args.get("cwd", str(PROJECT_ROOT))
    # F-C-5: clamp the model-supplied timeout to [1, MAX_CODE_TIMEOUT_S].
    timeout = _clamp_code_timeout(args.get("timeout"), CODE_TIMEOUT)

    # Validate cwd
    try:
        validate_path(cwd)
    except PermissionError as e:
        return f"PERMISSION_ERROR: cwd {e}"

    python = _find_python()

    if file_path:
        # Execute a .py file
        try:
            resolved = validate_path(file_path)
        except PermissionError as e:
            return f"PERMISSION_ERROR: {e}"

        if not resolved.exists():
            return tool_error(f"File not found: {resolved}")
        if not str(resolved).endswith(".py"):
            return tool_error(f"Expected a .py file, got: {resolved.name}")

        # NOTE: the inline-snippet blocklist is NOT applied to files (M4).
        # Nearly every real .py file contains `import os` or the word
        # "subprocess" (even in comments), so scanning whole files blocked the
        # tool's primary use case. Files are gated by validate_path + the HITL
        # gate + minimal env + the OS sandbox instead.
        cmd = _sandbox_wrap_cmd([python, str(resolved)])
        return await _run_exec(cmd, cwd=cwd, timeout=timeout)

    elif code:
        # Check for dangerous patterns in inline code (defence-in-depth)
        block_msg = _check_code_blocked(code)
        if block_msg:
            return block_msg

        # Execute in isolated mode: -I strips user site-packages, no PYTHONSTARTUP
        cmd = _sandbox_wrap_cmd([python, "-I", "-c", code])
        return await _run_exec(
            cmd,
            cwd=cwd,
            timeout=timeout,
            extra_env={"PYTHONNOUSERSITE": "1"},
        )

    else:
        return tool_error("Provide either 'file' (path to .py file) or 'code' (inline Python snippet).")


async def handle_run_node(args: dict) -> str:
    """
    Execute JavaScript/Node.js code.

    Risk tier: HIGH (== HITL-gated when hitl_risk_threshold >= high).
    Security layers: HITL gate → minimal env → OS sandbox → Node blocklist.

    Accepts either:
      - 'file': path to a .js file (relative to PROJECT_ROOT or absolute)
      - 'code': inline JavaScript code snippet to execute

    If both are provided, 'file' takes priority.
    """
    file_path = args.get("file", "")
    code = args.get("code", "")
    cwd = args.get("cwd", str(PROJECT_ROOT))
    # F-C-5: clamp the model-supplied timeout to [1, MAX_CODE_TIMEOUT_S].
    timeout = _clamp_code_timeout(args.get("timeout"), CODE_TIMEOUT)

    # Validate cwd
    try:
        validate_path(cwd)
    except PermissionError as e:
        return f"PERMISSION_ERROR: cwd {e}"

    try:
        node = _find_node()
    except RuntimeError as e:
        return tool_error(e)

    if file_path:
        # Execute a .js file
        try:
            resolved = validate_path(file_path)
        except PermissionError as e:
            return f"PERMISSION_ERROR: {e}"

        if not resolved.exists():
            return tool_error(f"File not found: {resolved}")
        if not str(resolved).endswith((".js", ".mjs", ".cjs")):
            return tool_error(f"Expected a .js file, got: {resolved.name}")

        # NOTE: the inline-snippet blocklist is NOT applied to files (M4) —
        # any real Node script `require`s fs/http/etc. Files are gated by
        # validate_path + the HITL gate + minimal env + the OS sandbox.
        cmd = _sandbox_wrap_cmd([node, str(resolved)])
        return await _run_exec(cmd, cwd=cwd, timeout=timeout)

    elif code:
        # Check for dangerous patterns in inline Node.js code (defence-in-depth)
        block_msg = _check_node_blocked(code)
        if block_msg:
            return block_msg

        # Apply the Python inline blocklist too as belt-and-suspenders for
        # cross-runtime patterns (eval/exec tokens look the same in JS).
        block_msg = _check_code_blocked(code)
        if block_msg:
            return block_msg

        # Execute inline snippet with restricted flags
        cmd = _sandbox_wrap_cmd([node, "--disable-proto=delete", "-e", code])
        return await _run_exec(cmd, cwd=cwd, timeout=timeout)

    else:
        return tool_error("Provide either 'file' (path to .js file) or 'code' (inline JavaScript snippet).")


async def handle_lint_file(args: dict) -> str:
    """
    Lint a Python file using ruff (preferred) or flake8 (fallback).

    Args:
      - 'path': Path to the Python file to lint.
      - 'fix': If True, attempt auto-fix (ruff only). Default False.
    """
    file_path = args.get("path", "")
    fix = args.get("fix", False)
    cwd = args.get("cwd", str(PROJECT_ROOT))
    # F-C-5: clamp the model-supplied timeout to [1, MAX_CODE_TIMEOUT_S].
    timeout = _clamp_code_timeout(args.get("timeout"), SHELL_TIMEOUT)

    if not file_path:
        return tool_error("'path' parameter is required (path to Python file to lint).")

    try:
        resolved = validate_path(file_path)
    except PermissionError as e:
        return f"PERMISSION_ERROR: {e}"

    if not resolved.exists():
        return tool_error(f"File not found: {resolved}")

    # Prefer ruff, fall back to flake8. The linter must be invoked by its
    # ABSOLUTE path: availability is checked against the parent PATH, but the
    # subprocess runs with the restricted sandbox PATH — a venv/homebrew ruff
    # would resolve for the check yet FileNotFoundError at exec time (H3).
    ruff_path = shutil.which("ruff")
    flake8_path = shutil.which("flake8")
    if ruff_path:
        linter = "ruff"
        if fix:
            cmd = [ruff_path, "check", "--fix", str(resolved)]
        else:
            cmd = [ruff_path, "check", str(resolved)]
        logger.info(f"lint_file: using ruff for {resolved}")
    elif flake8_path:
        linter = "flake8"
        cmd = [flake8_path, str(resolved)]
        if fix:
            logger.info("lint_file: --fix is not supported by flake8, running check only")
        logger.info(f"lint_file: using flake8 for {resolved}")
    else:
        return (
            "ERROR: No Python linter found. Install ruff ('pip install ruff') "
            "or flake8 ('pip install flake8') and try again."
        )

    result = await _run_exec(cmd, cwd=cwd, timeout=timeout)
    return f"[{linter}] {result}"

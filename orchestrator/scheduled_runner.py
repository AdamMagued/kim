"""
Scheduled task executor foundation.

Discovers due tasks from CronStore, enforces a provider allowlist, and
launches each through orchestrator.agent (same subprocess path as send_task).

Provider policy (allowlist):
  Allowed: empty/None (-> ollama), "ollama", "ollama-cloud",
           "browser", "browser:<site>"
  Refused: all other values (openai, gpt*, claude, gemini, deepseek, etc.)

"browser" requires the Kim app/bridge to be running for full functionality.
"ollama" or empty are the standalone-safe defaults for scheduled tasks.

Interpreter resolution mirrors Tauri's find_python_interpreter preference:
  venv/bin/python, .venv/bin/python (or Windows Scripts equivalents), then
  sys.executable (the running interpreter) as a safe absolute-path fallback.

Preflight: before spawning and before record_run, a cheap subprocess.run
verifies that mcp and orchestrator.agent are importable via the resolved
interpreter.  Failure returns a RunDueResult with error set; record_run is
never called.

record_run is called atomically within a cross-process runner lock (after
passing preflight) to prevent duplicate task launches when a timer tick races
a manual run_due_scheduled_task call.  The lock is released before Popen so
it is never held during the slow subprocess spawn.

Active agent PIDs are tracked in a registry file so subsequent invocations can
reap orphaned/runaway processes that exceed _AGENT_MAX_WALL_SECONDS.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from orchestrator.cron_store import CronStore

_ALLOWED_RE = re.compile(
    r"^(ollama(-cloud)?|browser(:[a-z0-9_.-]+)?)$",
    re.IGNORECASE,
)

_PREFLIGHT_TIMEOUT = 10  # seconds
_RUNNER_LOCK_TIMEOUT = 30  # seconds to wait for the cross-process runner lock
_AGENT_MAX_WALL_SECONDS = 3600  # 1 hour wall-clock limit per scheduled agent


def is_allowed_provider(provider: Optional[str]) -> bool:
    """Return True if provider is permitted for scheduled execution."""
    if not provider or not provider.strip():
        return True  # empty -> defaults to ollama
    return bool(_ALLOWED_RE.match(provider.strip()))


def find_interpreter(kim_root: Path) -> str:
    """
    Locate the Python interpreter for kim_root, mirroring Tauri's
    find_python_interpreter preference order.

    Returns an absolute path string (from venv/.venv) when a venv is found,
    or sys.executable (the interpreter running this code) as a safe
    absolute-path fallback.  Bare PATH lookups are intentionally avoided to
    eliminate the PATH-hijack risk.
    """
    candidates = [
        kim_root / "venv" / "bin" / "python",
        kim_root / ".venv" / "bin" / "python",
        kim_root / "venv" / "Scripts" / "python.exe",
        kim_root / ".venv" / "Scripts" / "python.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)

    # Fall back to the interpreter that is already running this process.
    # sys.executable is always an absolute, resolved path, making it immune to
    # PATH-based hijacking.
    return sys.executable


# ---------------------------------------------------------------------------
# Cross-process runner lock
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _runner_exclusive_lock(kim_root: Path, timeout: float = _RUNNER_LOCK_TIMEOUT):
    """Advisory file lock that serialises the due-check + preflight + claim
    cycle across all processes (timer tick and manual command) so that only
    one process can spawn an agent for a given task slot at a time.

    Mirrors the _exclusive_lock pattern from cron_store.py.
    """
    lock_path = kim_root / "logs" / "scheduled_runs" / ".runner.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"scheduled_runner: could not acquire runner lock "
                        f"{lock_path} within {timeout}s"
                    )
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                lock_path.unlink()
            except OSError:
                pass
    else:
        import fcntl  # noqa: PLC0415 — POSIX only
        with open(lock_path, "a") as fh:  # 'a' creates without truncating
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# PID registry for orphan reaping
# ---------------------------------------------------------------------------

def _pid_registry_path(kim_root: Path) -> Path:
    return kim_root / "logs" / "scheduled_runs" / ".running_pids.json"


def _reap_stale_agents(
    kim_root: Path,
    timeout_seconds: float = _AGENT_MAX_WALL_SECONDS,
) -> None:
    """Read the PID registry and kill any scheduled-agent processes that have
    exceeded *timeout_seconds* of wall-clock runtime.  Finished or vanished
    processes are pruned from the registry regardless of their runtime.
    """
    reg_path = _pid_registry_path(kim_root)
    if not reg_path.exists():
        return
    try:
        entries: list = json.loads(reg_path.read_text())
    except (OSError, ValueError):
        return

    now_ts = time.time()
    surviving = []
    for entry in entries:
        pid = entry.get("pid")
        if pid is None:
            continue
        started = entry.get("started_at", now_ts)
        elapsed = now_ts - started
        try:
            os.kill(pid, 0)  # signal 0: existence probe; raises OSError if gone
        except OSError:
            continue  # already exited — drop from registry
        if elapsed > timeout_seconds:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
            # drop from registry after kill (do not append to surviving)
        else:
            surviving.append(entry)

    try:
        reg_path.write_text(json.dumps(surviving))
    except OSError:
        pass


def _register_agent_pid(kim_root: Path, task_id: str, pid: int) -> None:
    """Append a running-agent PID entry to the registry so future invocations
    can reap it if it exceeds the wall-clock timeout.
    """
    reg_path = _pid_registry_path(kim_root)
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing: list = (
            json.loads(reg_path.read_text()) if reg_path.exists() else []
        )
    except (OSError, ValueError):
        existing = []
    existing.append({"task_id": task_id, "pid": int(pid), "started_at": time.time()})
    try:
        reg_path.write_text(json.dumps(existing))
    except (OSError, TypeError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def _preflight(python: str, kim_root: Path, env: dict) -> Optional[str]:
    """
    Verify that mcp and orchestrator.agent are importable via *python*.

    Returns None on success, or an error string describing the failure.
    """
    try:
        r = subprocess.run(
            [python, "-c", "import mcp; import orchestrator.agent"],
            cwd=str(kim_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=_PREFLIGHT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"preflight timed out after {_PREFLIGHT_TIMEOUT}s"
    except OSError as exc:
        return f"preflight could not run interpreter: {exc}"

    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "non-zero exit").strip()
        return f"preflight failed (interpreter lacks required deps): {msg[:200]}"
    return None


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class RunDueResult:
    task_id: str
    task_text: str
    launched: bool = False
    recorded: bool = False
    skipped: bool = False
    skip_reason: str = ""
    error: str = ""
    log_file: str = ""

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "task": self.task_text,
            "launched": self.launched,
            "recorded": self.recorded,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "error": self.error,
            "log_file": self.log_file,
        }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_next_due_task(
    store_file: Optional[Path] = None,
    dry_run: bool = False,
    kim_root: Optional[Path] = None,
    as_of: Optional[datetime] = None,
    session_dir: Optional[Path] = None,
    _interpreter_override: Optional[str] = None,
) -> Optional[RunDueResult]:
    """
    Find the first due task, check provider, run preflight, spawn
    orchestrator.agent, and record the run.

    Returns None when no tasks are due.
    Returns a RunDueResult describing the outcome otherwise.

    The due-check, preflight, and record_run are performed while holding a
    cross-process advisory lock so that a timer tick and a manual
    run_due_scheduled_task call cannot both see the same task as due and
    launch duplicate agents.  The lock is released before Popen so it is
    never held during the slow subprocess spawn.

    _interpreter_override: inject a specific interpreter path (tests only).
    """
    if kim_root is None:
        kim_root = Path(__file__).parent.parent

    store = CronStore(store_file=store_file)

    # Reap orphaned/runaway agents from previous invocations before checking
    # for new work.
    _reap_stale_agents(kim_root)

    # ------------------------------------------------------------------
    # Atomic section: hold the cross-process runner lock across the
    # due-check → provider-filter → preflight sequence.
    # record_run is intentionally called AFTER a successful Popen so that
    # a spawn failure does not advance next_run_at.
    # Variables set inside the with-block (task, result, python, env,
    # provider) remain accessible after it exits.
    # ------------------------------------------------------------------
    try:
        with _runner_exclusive_lock(kim_root):
            due = store.due_tasks(as_of=as_of)
            if not due:
                return None

            # Iterate due tasks (most-overdue first) to find the first with an
            # allowed provider.  A forbidden-provider task never gets
            # record_run called, so its next_run_at never advances — if we
            # always stopped at due[0] we would permanently block every task
            # behind a single misconfigured entry.
            forbidden_result: Optional[RunDueResult] = None
            task = None
            for candidate in due:
                if is_allowed_provider(candidate.provider):
                    task = candidate
                    break
                if forbidden_result is None:
                    forbidden_result = RunDueResult(
                        task_id=candidate.id,
                        task_text=candidate.task,
                        skipped=True,
                        skip_reason=(
                            f"provider {candidate.provider!r} is not in the scheduled-executor allowlist "
                            "(allowed: ollama, ollama-cloud, browser, browser:<site>, or empty); "
                            "update the task provider or remove the constraint"
                        ),
                    )

            if task is None:
                # All due tasks have forbidden providers; return the first as a sample.
                return forbidden_result

            result = RunDueResult(task_id=task.id, task_text=task.task)

            if dry_run:
                result.skipped = True
                result.skip_reason = "dry_run"
                return result

            python = _interpreter_override or find_interpreter(kim_root)
            provider = (task.provider or "ollama").strip()
            env = _build_subprocess_env(kim_root)

            # Preflight runs inside the lock so that a failed check does not
            # advance next_run_at (record_run is not called on preflight failure).
            preflight_err = _preflight(python, kim_root, env)
            if preflight_err:
                result.error = preflight_err
                return result

    except TimeoutError as exc:
        # Runner lock could not be acquired — treat as a transient error so the
        # task remains due and will be retried on the next timer tick.
        return RunDueResult(
            task_id="unknown",
            task_text="",
            error=f"runner lock timeout: {exc}",
        )

    # ------------------------------------------------------------------
    # Spawn outside the lock so we do not hold it during the slow Popen.
    # task, result, python, env, provider are all set above.
    # ------------------------------------------------------------------
    args = [python, "-m", "orchestrator.agent", "--task", task.task, "--provider", provider]
    if session_dir is not None:
        args += ["--session-dir", str(session_dir)]

    log_path = _make_run_log_path(kim_root, task.id)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = log_path.open("w")
    except OSError as exc:
        result.error = f"could not open run log {log_path}: {exc}"
        return result

    try:
        proc = subprocess.Popen(
            args,
            cwd=str(kim_root),
            env=env,
            stdout=log_fh,
            stderr=log_fh,
        )
    except OSError as exc:
        log_fh.close()
        try:
            log_path.unlink()
        except OSError:
            pass
        result.error = f"spawn failed: {exc}"
        return result

    log_fh.close()
    result.launched = True
    result.log_file = str(log_path)

    # Register the PID so future invocations can enforce the wall-clock timeout.
    _register_agent_pid(kim_root, task.id, int(proc.pid))

    # Advance next_run_at now that the agent is successfully running.
    try:
        recorded = store.record_run(task.id)
    except TimeoutError as exc:
        result.error = f"record_run failed: {exc}"
        return result
    result.recorded = recorded is not None

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_subprocess_env(kim_root: Path) -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(kim_root)
    env["PROJECT_ROOT"] = str(kim_root)
    return env


def _make_run_log_path(kim_root: Path, task_id: str) -> Path:
    """Return a per-run log path under logs/scheduled_runs/.

    Filename: <task_id>_<UTC-ISO-timestamp>.log with colons replaced by
    hyphens so it is safe on all filesystems.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_id = re.sub(r"[^\w-]", "_", task_id)
    return kim_root / "logs" / "scheduled_runs" / f"{safe_id}_{ts}.log"

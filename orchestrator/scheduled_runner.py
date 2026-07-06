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

record_run and Popen are both called while still holding the cross-process
runner lock so that the task slot is claimed atomically before the lock is
released.  Popen is non-blocking (fork+exec returns immediately; the child
runs asynchronously), so the lock is held for only a brief moment.

Active agent PIDs are tracked in a registry file so subsequent invocations can
reap orphaned/runaway processes that exceed _AGENT_MAX_WALL_SECONDS.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
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
# Platform-aware process existence check (Finding 1)
# ---------------------------------------------------------------------------

def _pid_exists(pid: int) -> bool:
    """Return True if *pid* refers to a running process.

    On POSIX, uses signal 0 (no signal is actually sent — raises OSError if
    the process is gone).

    On Windows, os.kill(pid, 0) calls TerminateProcess in CPython for any
    signal other than CTRL_C / CTRL_BREAK, which would kill every scheduled
    agent on each tick.  Instead we try psutil.pid_exists first (lightweight),
    then fall back to a raw OpenProcess(SYNCHRONIZE) call via ctypes.  Neither
    path sends any signal or kills the target process.
    """
    if sys.platform == "win32":
        # Prefer psutil when available — it handles zombie / access-denied
        # cases correctly.
        try:
            import psutil  # type: ignore[import]
            return psutil.pid_exists(pid)
        except ImportError:
            pass
        # Fallback: OpenProcess with SYNCHRONIZE (0x100000) succeeds when the
        # process exists even if we lack full access rights.
        import ctypes
        SYNCHRONIZE = 0x100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)  # type: ignore[attr-defined]
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _kill_process_tree(pid: int) -> None:
    """Kill a runaway scheduled agent AND its descendants (finding 5.1).

    A bare os.kill(pid, 9) reaps only the agent, orphaning the MCP server,
    controlled browser, and any shell-tool subprocesses it spawned — the reaper
    that exists to stop runaways would leave untracked runaways behind. Agents
    are spawned as session leaders (start_new_session=True), so on POSIX the
    whole process group can be killed at once. The group kill is used ONLY when
    the target is confirmed to be its own group leader (pgid == pid); otherwise
    a group kill could reach the runner/app's own group, so fall back to a
    single-process kill.
    """
    if sys.platform == "win32":
        # taskkill /T terminates the whole tree; fall back to a plain kill.
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=10, check=False,
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
        return

    try:
        pgid = os.getpgid(pid)
    except OSError:
        return  # already gone
    if pgid == pid:
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except OSError:
            pass
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)


# ---------------------------------------------------------------------------
# PID registry for orphan reaping
# ---------------------------------------------------------------------------

def _boot_ref() -> int:
    """A cheap, portable per-boot identity.

    ``time.time() - time.monotonic()`` is the wall-clock instant the
    system-wide monotonic clock was zeroed — stable across processes within a
    boot session and shifted by the downtime across a reboot. Used to detect
    PID reuse across reboots (M6): a registry entry whose boot ref does not
    match the current one predates this boot, so its PID may now belong to an
    innocent process and must never be SIGKILLed.
    """
    return round(time.time() - time.monotonic())


# Allow small NTP/monotonic drift when comparing boot refs (seconds).
_BOOT_REF_TOLERANCE = 5


def _pid_registry_path(kim_root: Path) -> Path:
    return kim_root / "logs" / "scheduled_runs" / ".running_pids.json"


@contextlib.contextmanager
def _pid_registry_lock(kim_root: Path, timeout: float = _RUNNER_LOCK_TIMEOUT):
    """Advisory file lock that serialises reads and writes to .running_pids.json
    (Finding 2).  Uses the same spin-on-Windows / flock-on-POSIX pattern as
    _runner_exclusive_lock but targets a separate lock file so it never blocks
    the main runner lock.
    """
    lock_path = kim_root / "logs" / "scheduled_runs" / ".pids.lock"
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
                        f"scheduled_runner: could not acquire PID registry lock "
                        f"{lock_path} within {timeout}s"
                    )
                time.sleep(0.05)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                lock_path.unlink()
    else:
        import fcntl  # noqa: PLC0415 — POSIX only
        with open(lock_path, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def _write_pid_registry_atomic(reg_path: Path, data: list) -> None:
    """Write *data* to *reg_path* atomically via tmp file + os.replace (Finding 2)."""
    text = json.dumps(data).encode()
    tmp_fd, tmp_name = tempfile.mkstemp(dir=reg_path.parent, prefix=".pids_tmp")
    try:
        os.write(tmp_fd, text)
        os.fsync(tmp_fd)  # durable before the atomic rename (finding 4.3)
        os.close(tmp_fd)
        os.replace(tmp_name, str(reg_path))
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _reap_stale_agents(
    kim_root: Path,
    timeout_seconds: float = _AGENT_MAX_WALL_SECONDS,
) -> None:
    """Read the PID registry and kill any scheduled-agent processes that have
    exceeded *timeout_seconds* of wall-clock runtime.  Finished or vanished
    processes are pruned from the registry regardless of their runtime.

    The entire read-modify-write is performed under _pid_registry_lock (Finding 2)
    and the write is atomic via tmp + os.replace.  The existence probe uses
    _pid_exists so it does not accidentally kill processes on Windows (Finding 1).
    """
    reg_path = _pid_registry_path(kim_root)
    if not reg_path.exists():
        return

    try:
        with _pid_registry_lock(kim_root):
            try:
                entries: list = json.loads(reg_path.read_text())
            except (OSError, ValueError):
                return

            now_ts = time.time()
            current_boot = _boot_ref()
            surviving = []
            for entry in entries:
                pid = entry.get("pid")
                if pid is None:
                    continue
                # M6: an entry from a previous boot cannot be trusted — the OS
                # may have reused its PID for an unrelated process. Drop it
                # without killing. Entries predating this fix (no boot_ref)
                # are also treated as unverifiable and never killed.
                entry_boot = entry.get("boot_ref")
                if entry_boot is None or abs(int(entry_boot) - current_boot) > _BOOT_REF_TOLERANCE:
                    continue
                started = entry.get("started_at", now_ts)
                elapsed = now_ts - started
                if not _pid_exists(pid):
                    continue  # already exited — drop from registry
                if elapsed > timeout_seconds:
                    # Kill the whole process group so orphaned children (MCP
                    # server, browser, shell subprocesses) go with it (5.1).
                    _kill_process_tree(pid)
                    # drop from registry after kill (do not append to surviving)
                else:
                    surviving.append(entry)

            try:
                _write_pid_registry_atomic(reg_path, surviving)
            except OSError:
                pass
    except TimeoutError:
        pass  # lock contention — skip reap this tick; not fatal


def _register_agent_pid(kim_root: Path, task_id: str, pid: int) -> None:
    """Append a running-agent PID entry to the registry so future invocations
    can reap it if it exceeds the wall-clock timeout.

    The entire read-modify-write is performed under _pid_registry_lock (Finding 2)
    and the write is atomic via tmp + os.replace.
    """
    reg_path = _pid_registry_path(kim_root)
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _pid_registry_lock(kim_root):
            try:
                existing: list = (
                    json.loads(reg_path.read_text()) if reg_path.exists() else []
                )
            except (OSError, ValueError):
                existing = []
            existing.append({
                "task_id": task_id,
                "pid": int(pid),
                "started_at": time.time(),
                "boot_ref": _boot_ref(),  # M6: guards against reboot PID reuse
            })
            try:
                _write_pid_registry_atomic(reg_path, existing)
            except (OSError, TypeError, ValueError):
                pass
    except TimeoutError:
        pass  # lock contention — PID unregistered; reap on next tick may miss it


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

    The due-check, preflight, Popen, and record_run are all performed while
    holding a cross-process advisory lock so that a timer tick and a manual
    run_due_scheduled_task call cannot both see the same task as due and
    launch duplicate agents.  Popen is non-blocking (fork+exec returns
    immediately), so the lock is held for only a brief moment during spawn.

    _interpreter_override: inject a specific interpreter path (tests only).
    """
    if kim_root is None:
        kim_root = Path(__file__).parent.parent

    store = CronStore(store_file=store_file)

    # Reap orphaned/runaway agents from previous invocations before checking
    # for new work.
    _reap_stale_agents(kim_root)

    # ------------------------------------------------------------------
    # Atomic section: hold the cross-process runner lock across the entire
    # due-check → provider-filter → preflight → Popen → record_run sequence.
    # Keeping Popen and record_run inside the lock means the task slot is
    # claimed (next_run_at advanced) before the lock is released, so a
    # concurrent process (e.g. a timer tick racing a manual run) that
    # acquires the lock next will see the task as no longer due and will not
    # spawn a duplicate agent.  Popen itself is non-blocking — it fork+execs
    # and returns immediately — so the lock is held for only a brief moment.
    # record_run is only called after a successful Popen, preserving the
    # invariant that a spawn failure does not advance next_run_at.
    # ------------------------------------------------------------------
    proc = None  # set inside the lock on successful spawn
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

            # Build args and open the log file before spawning.
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

            # Spawn the agent.  Popen is non-blocking (fork+exec returns
            # immediately; the child runs asynchronously), so the lock is
            # held for only a brief moment here.
            # Spawn as a session/process-group leader so the reaper can later
            # kill the agent AND its descendants as a group (finding 5.1). On
            # POSIX this makes pgid == pid; on Windows a new process group.
            _group_kwargs: dict = {}
            if sys.platform == "win32":
                _group_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                _group_kwargs["start_new_session"] = True
            try:
                proc = subprocess.Popen(
                    args,
                    cwd=str(kim_root),
                    env=env,
                    stdout=log_fh,
                    stderr=log_fh,
                    **_group_kwargs,
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

            # Advance next_run_at now that the agent is successfully running.
            # This runs inside the lock so a concurrent process cannot observe
            # the task as still-due and spawn a duplicate.
            # Anchor to as_of (the due-check time) rather than wall-clock now,
            # so catch-up/replay runs advance next_run_at from the scheduled
            # instant instead of drifting to the moment the runner happened to fire.
            try:
                recorded = store.record_run(task.id, ran_at=as_of)
            except TimeoutError as exc:
                # M7: the agent is already spawned (launched=True). Register its
                # PID before this early return so the reaper can still enforce
                # the wall-clock timeout — otherwise the child runs unreaped and
                # (next_run_at not advanced) the same task is due again next
                # tick → a duplicate agent.
                if proc is not None:
                    _register_agent_pid(kim_root, task.id, int(proc.pid))
                result.error = f"record_run failed: {exc}"
                return result
            result.recorded = recorded is not None

    except TimeoutError as exc:
        # Runner lock could not be acquired — treat as a transient error so the
        # task remains due and will be retried on the next timer tick.
        return RunDueResult(
            task_id="unknown",
            task_text="",
            error=f"runner lock timeout: {exc}",
        )

    # Register the PID outside the lock — bookkeeping only; does not affect
    # the due-check race.  proc is guaranteed set here (all early returns above
    # happen before the Popen or via return inside the with-block).
    if proc is not None:
        _register_agent_pid(kim_root, task.id, int(proc.pid))

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

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
  python3/python on PATH.

Preflight: before spawning and before record_run, a cheap subprocess.run
verifies that mcp and orchestrator.agent are importable via the resolved
interpreter.  Failure returns a RunDueResult with error set; record_run is
never called.

record_run is called on successful Popen (after passing preflight) to advance
next_run_at and prevent re-firing on the next check even if the task later fails.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from orchestrator.cron_store import CronStore

_ALLOWED_RE = re.compile(
    r"^(ollama(-cloud)?|browser(:[a-z0-9_.-]+)?)$",
    re.IGNORECASE,
)

_PREFLIGHT_TIMEOUT = 10  # seconds


def is_allowed_provider(provider: Optional[str]) -> bool:
    """Return True if provider is permitted for scheduled execution."""
    if not provider or not provider.strip():
        return True  # empty -> defaults to ollama
    return bool(_ALLOWED_RE.match(provider.strip()))


def find_interpreter(kim_root: Path) -> str:
    """
    Locate the Python interpreter for kim_root, mirroring Tauri's
    find_python_interpreter preference order.

    Returns an absolute path string (from venv/.venv) or a bare command name
    (python3/python) when no venv is found.
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

    for cmd in ("python3", "python"):
        found = shutil.which(cmd)
        if found:
            return found

    return sys.executable


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

    _interpreter_override: inject a specific interpreter path (tests only).
    """
    store = CronStore(store_file=store_file)
    due = store.due_tasks(as_of=as_of)
    if not due:
        return None

    # Iterate due tasks (most-overdue first) to find the first with an allowed
    # provider.  A forbidden-provider task never gets record_run called, so its
    # next_run_at never advances -- if we always stopped at due[0] we would
    # permanently block every task behind a single misconfigured entry.
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

    if kim_root is None:
        kim_root = Path(__file__).parent.parent

    python = _interpreter_override or find_interpreter(kim_root)
    provider = (task.provider or "ollama").strip()
    env = _build_subprocess_env(kim_root)

    preflight_err = _preflight(python, kim_root, env)
    if preflight_err:
        result.error = preflight_err
        return result

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
        subprocess.Popen(
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

    # Record on successful spawn (after passing preflight) so next_run_at
    # advances and the task does not re-fire immediately on the next check.
    try:
        recorded = store.record_run(task.id)
    except TimeoutError as exc:
        result.error = f"record_run failed: {exc}"
        return result
    result.recorded = recorded is not None

    return result


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
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    safe_id = re.sub(r"[^\w-]", "_", task_id)
    return kim_root / "logs" / "scheduled_runs" / f"{safe_id}_{ts}.log"

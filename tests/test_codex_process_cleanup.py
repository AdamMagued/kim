"""Behavioral process-lifecycle tests for the live codex spawn path (K4).

Pins, by actually spawning and observing a REAL fake codex binary:

1. exit-code propagation  — _run_async returns the codex exit code
2. timeout kill           — when the whole-run budget elapses, the codex
                            subprocess is killed (no orphan) and rc == 1
3. proxy teardown         — the local proxy is stopped on every exit path
4. _cleanup_sync          — the atexit/SIGTERM handler kills the active
                            process for the abnormal-interpreter-exit path
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from codex_bridge_harness import run_bridge


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - pid exists, other owner
        return True
    return True


def _wait_for_death(pid: int, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _pid_is_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_is_alive(pid)


class TestCodexProcessLifecycle(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="kim-cleanup-")
        self.tmp = Path(self._tmp.name)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_clean_exit_returns_zero_and_stops_proxy(self):
        result = await run_bridge(self.tmp, exit_code=0)
        self.assertEqual(result.rc, 0)
        result.proxy.stop.assert_awaited()

    async def test_nonzero_exit_code_is_propagated(self):
        result = await run_bridge(self.tmp, exit_code=7)
        self.assertEqual(result.rc, 7)
        result.proxy.stop.assert_awaited()

    async def test_timeout_kills_the_codex_subprocess(self):
        """A codex run exceeding the task budget must not leave an orphan."""
        result = await run_bridge(
            self.tmp,
            sleep_s=60,
            # A tight 1s budget races interpreter startup under a loaded
            # machine; 8s still expires long before sleep_s=60, so the
            # timeout-kill semantics are unchanged.
            config_yaml="codex_bridge:\n  transport: exec\n  task_timeout_s: 8\n",
        )
        self.assertEqual(result.rc, 1)
        pid_file = result.bin_dir / "pid.txt"
        self.assertTrue(pid_file.exists(), "fake codex never started")
        pid = int(pid_file.read_text().strip())
        self.assertTrue(
            _wait_for_death(pid),
            f"codex subprocess (pid {pid}) survived the timeout kill — orphan",
        )
        result.proxy.stop.assert_awaited()

    async def test_cleanup_sync_kills_active_process(self):
        """The atexit/SIGTERM handler must kill whatever process is active."""
        from orchestrator import codex_bridge_service as svc

        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        try:
            svc._active_process = proc  # type: ignore[assignment]
            svc._cleanup_sync()
            # proc is our direct child: reap it and confirm cleanup did not
            # let it run to a natural successful exit. POSIX reports a negative
            # signal code; Windows taskkill commonly reports a positive code.
            returncode = proc.wait(timeout=5)
            self.assertNotEqual(
                returncode,
                0,
                "_cleanup_sync did not kill the active codex process "
                f"(returncode={returncode})",
            )
        finally:
            svc._active_process = None
            if proc.poll() is None:  # safety net
                proc.kill()
            proc.wait(timeout=5)

    async def test_no_orphan_after_normal_run(self):
        """After a normal run, the spawned codex process is fully reaped."""
        result = await run_bridge(self.tmp, exit_code=0)
        pid = int((result.bin_dir / "pid.txt").read_text().strip())
        self.assertTrue(_wait_for_death(pid))

    async def test_timeout_kills_grandchild_shell_subprocess_too(self):
        """A timeout must reap the WHOLE process tree, not just codex-exec.

        The fake codex binary spawns its own grandchild (standing in for a
        shell/tool subprocess codex itself launches) and never waits on it.
        Before finding 3 was fixed, process.kill() only signalled the direct
        codex-exec PID, orphaning this grandchild forever.
        """
        result = await run_bridge(
            self.tmp,
            sleep_s=60,
            spawn_child=True,
            # A larger budget than the other timeout tests: this fake binary
            # does extra work (spawning + recording a grandchild pid) before
            # it can be killed, and a tight budget races interpreter startup
            # under a loaded machine.
            config_yaml="codex_bridge:\n  transport: exec\n  task_timeout_s: 8\n",
        )
        self.assertEqual(result.rc, 1)
        child_pid_file = result.bin_dir / "child_pid.txt"
        self.assertTrue(child_pid_file.exists(), "fake codex never spawned its grandchild")
        child_pid = int(child_pid_file.read_text().strip())
        self.assertTrue(
            _wait_for_death(child_pid),
            f"grandchild subprocess (pid {child_pid}) survived the timeout kill — orphan",
        )


if __name__ == "__main__":
    unittest.main()

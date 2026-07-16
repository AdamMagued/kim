"""Tests for codex_engine/standalone_proxy.py — the kimcli standalone runtime.

Spawns the module as a real subprocess (KIM_FAKE=1 so no network/API keys
are needed) and asserts the stdout handshake contract, bearer auth, the
parent-pid watchdog, and clean SIGTERM shutdown.

See tests/test_codex_proxy_modes.py for the in-process _CodexProxy
mode-generalization tests (chat-passthrough translation, the two TUI fixes)
that this runtime builds on.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MODULE_CMD = [sys.executable, "-u", "-m", "codex_engine.standalone_proxy"]


def _spawn(extra_args: list, env_extra: dict | None = None) -> subprocess.Popen:
    env = os.environ.copy()
    env.pop("KIM_FAKE", None)
    pythonpath = str(_REPO_ROOT)
    if env.get("PYTHONPATH"):
        pythonpath += os.pathsep + env["PYTHONPATH"]
    env["PYTHONPATH"] = pythonpath
    if env_extra:
        env.update(env_extra)
    return subprocess.Popen(
        _MODULE_CMD + extra_args,
        cwd=str(_REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def _readline_with_timeout(stream, timeout: float = 20.0):
    """readline() that can't hang the test suite if the line never comes."""
    q: "queue.Queue" = queue.Queue()
    threading.Thread(target=lambda: q.put(stream.readline()), daemon=True).start()
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        return None


def _terminate(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=timeout)


class StandaloneProxyHandshakeTests(unittest.TestCase):
    def test_ready_handshake_is_a_single_parseable_line(self):
        proc = _spawn(["--provider", "fake"], env_extra={"KIM_FAKE": "1"})
        try:
            line = _readline_with_timeout(proc.stdout)
            self.assertIsNotNone(line, "no handshake line printed within timeout")
            payload = json.loads(line)
            self.assertEqual(payload["event"], "ready")
            self.assertIsInstance(payload["port"], int)
            self.assertGreater(payload["port"], 0)
            self.assertIsInstance(payload["token"], str)
            self.assertTrue(payload["token"])
        finally:
            _terminate(proc)

    def test_fatal_handshake_on_bad_provider_name(self):
        proc = _spawn(["--provider", "totally-bogus-provider-name"])
        try:
            line = _readline_with_timeout(proc.stdout)
            self.assertIsNotNone(line)
            payload = json.loads(line)
            self.assertEqual(payload["event"], "fatal")
            self.assertIn("bogus-provider-name", payload["message"])
            proc.wait(timeout=10)
            self.assertNotEqual(proc.returncode, 0)
        finally:
            _terminate(proc)

    def test_bearer_auth_enforced(self):
        proc = _spawn(["--provider", "fake"], env_extra={"KIM_FAKE": "1"})
        try:
            line = _readline_with_timeout(proc.stdout)
            self.assertIsNotNone(line)
            payload = json.loads(line)
            port, token = payload["port"], payload["token"]
            url = f"http://127.0.0.1:{port}/v1/chat/completions"
            body = json.dumps({
                "model": "x", "messages": [{"role": "user", "content": "hi"}],
            }).encode()

            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"}, method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(ctx.exception.code, 401)

            req_ok = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                method="POST",
            )
            with urllib.request.urlopen(req_ok, timeout=10) as resp:
                self.assertEqual(resp.status, 200)
                reply = json.loads(resp.read())
            self.assertEqual(reply["object"], "chat.completion")
        finally:
            _terminate(proc)

    @unittest.skipIf(sys.platform == "win32", "graceful SIGTERM handling differs on Windows")
    def test_sigterm_triggers_clean_exit(self):
        proc = _spawn(["--provider", "fake"], env_extra={"KIM_FAKE": "1"})
        try:
            line = _readline_with_timeout(proc.stdout)
            self.assertIsNotNone(line)
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)
            self.assertEqual(proc.returncode, 0)
        finally:
            _terminate(proc)


class StandaloneProxyWatchdogTests(unittest.TestCase):
    def test_watchdog_exits_when_parent_pid_dies(self):
        dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        proc = None
        try:
            proc = _spawn(
                ["--provider", "fake", "--parent-pid", str(dummy.pid)],
                env_extra={"KIM_FAKE": "1", "KIM_STANDALONE_PROXY_WATCHDOG_INTERVAL_S": "0.2"},
            )
            line = _readline_with_timeout(proc.stdout)
            self.assertIsNotNone(line)

            dummy.terminate()
            dummy.wait(timeout=10)

            proc.wait(timeout=15)  # watchdog polls every 0.2s in this test
            self.assertEqual(proc.returncode, 0)
        finally:
            if proc is not None:
                _terminate(proc)
            if dummy.poll() is None:
                dummy.terminate()
                dummy.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()

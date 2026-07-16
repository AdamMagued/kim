"""Tests for codex_engine/standalone_proxy.py — the kimcli standalone runtime.

Spawns the module as a real subprocess (KIM_FAKE=1 so no network/API keys
are needed) and asserts the stdout handshake contract, bearer auth, the
parent-pid watchdog, and clean SIGTERM shutdown.

See tests/test_codex_proxy_modes.py for the in-process _CodexProxy
mode-generalization tests (chat-passthrough translation, the two TUI fixes)
and tests/test_responses_passthrough.py for the responses-passthrough golden
translation tests that this runtime builds on.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from codex_engine.standalone_proxy import _resolve_auto_mode

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


def _collect_lines(stream) -> list:
    """Background-thread line collector — readline() until EOF, appending to
    a list a concurrently-reading test can poll (safe under the GIL: only
    ever appended to here, only ever iterated/read elsewhere)."""
    lines: list = []

    def _reader() -> None:
        for line in iter(stream.readline, ""):
            lines.append(line)

    threading.Thread(target=_reader, daemon=True).start()
    return lines


def _wait_for_substring(lines: list, substring: str, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(substring in line for line in lines):
            return True
        time.sleep(0.05)
    return False


class AutoModeResolutionTests(unittest.TestCase):
    """"auto" --mode resolution (module docstring "Mode"): browser:* (and
    bare "browser") providers are kimcli's primary path and must resolve to
    browser-contract; everything else (API providers — codex 0.144.3 removed
    the chat-completions wire API, so they can only be served on
    /v1/responses) resolves to responses-passthrough. chat-passthrough is
    never an auto-resolution target — it stays explicit-only."""

    def test_browser_colon_provider_resolves_to_browser_contract(self):
        for name in ("browser:claude", "browser:chatgpt", "browser:gemini", "browser:grok"):
            with self.subTest(name=name):
                self.assertEqual(_resolve_auto_mode(name), "browser-contract")

    def test_bare_browser_resolves_to_browser_contract(self):
        self.assertEqual(_resolve_auto_mode("browser"), "browser-contract")

    def test_case_insensitive(self):
        self.assertEqual(_resolve_auto_mode("Browser:Claude"), "browser-contract")

    def test_non_browser_providers_resolve_to_responses_passthrough(self):
        for name in ("claude", "openai", "gemini", "deepseek", "ollama", "fake"):
            with self.subTest(name=name):
                self.assertEqual(_resolve_auto_mode(name), "responses-passthrough")


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


class StdoutRedirectAfterHandshakeTests(unittest.TestCase):
    """After the one ready line, stdout's fd is dup2'd onto stderr's — proves
    the redirect against a REAL child process (an in-process test can't: it
    would repoint the test runner's own fd 1). browser-contract mode is used
    deliberately (--mode override, bypassing "auto") because it is the one
    mode whose narration/compaction prints would otherwise reach stdout."""

    def test_post_handshake_narration_lands_on_stderr_not_stdout(self):
        proc = _spawn(
            ["--provider", "fake", "--mode", "browser-contract"],
            env_extra={"KIM_FAKE": "1"},
        )
        stderr_lines = _collect_lines(proc.stderr)
        try:
            ready_line = _readline_with_timeout(proc.stdout)
            self.assertIsNotNone(ready_line, "no handshake line printed within timeout")
            payload = json.loads(ready_line)
            self.assertEqual(payload["event"], "ready")
            port, token = payload["port"], payload["token"]

            # A big enough /v1/responses input to cross the auto-compaction
            # threshold: _CodexProxy._apply_compaction unconditionally
            # prints two bare "[STATUS] Compacting context..."/"...compacted"
            # lines in browser-contract mode — real production narration,
            # not a contrived print, and independent of what the fake
            # provider's scripted replies actually contain.
            big_items = [{"role": "user", "content": "x" * 20000} for _ in range(25)]
            body = json.dumps({"model": "kim-proxy-model", "input": big_items, "stream": False}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/responses",
                data=body,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                method="POST",
            )
            # The compaction narration prints unconditionally BEFORE the
            # actual LLM call — orchestrator's FakeProvider (built for the
            # agent loop, not this browser-contract kwarg surface) 502s on
            # the follow-up complete(clear_chat=...) call, which is fine:
            # this test is about the print()->stderr redirect, not a
            # successful model round-trip.
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    resp.read()
            except urllib.error.HTTPError:
                pass

            # The narration reached stderr...
            self.assertTrue(
                _wait_for_substring(stderr_lines, "Compacting context"),
                f"expected narration on stderr; got: {stderr_lines!r}",
            )
            # ...and stdout's original pipe saw nothing further — dup2 closed
            # its write end in the child, so the parent's read end hits EOF
            # (readline() returns "") rather than ever seeing a second line.
            extra = _readline_with_timeout(proc.stdout, timeout=5.0)
            self.assertIn(extra, (None, ""), f"unexpected extra stdout content: {extra!r}")
        finally:
            _terminate(proc)


class StandaloneProxyWatchdogTests(unittest.TestCase):
    def test_watchdog_exits_when_parent_pid_dies(self):
        # A short-lived dummy that exits ON ITS OWN, rather than one we
        # terminate(): the dummy's OWN Popen object here would otherwise
        # keep it queryable-by-pid on Windows via a lingering open handle
        # for the rest of this test regardless of the child's actual run
        # state (see _pid_alive's Windows docstring) — a natural exit plus
        # a plain wait() sidesteps that entirely and does not depend on
        # this test's own signal delivery to the dummy being reliable.
        dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"])
        proc = None
        try:
            proc = _spawn(
                ["--provider", "fake", "--parent-pid", str(dummy.pid)],
                env_extra={"KIM_FAKE": "1", "KIM_STANDALONE_PROXY_WATCHDOG_INTERVAL_S": "0.2"},
            )
            # Generous timeout: this test spawns two subprocesses, and
            # process-spawn-heavy work is measurably slower on some CI runners.
            line = _readline_with_timeout(proc.stdout, timeout=30.0)
            self.assertIsNotNone(line)

            dummy.wait(timeout=10)  # exits on its own after ~2s

            proc.wait(timeout=20)  # watchdog polls every 0.2s in this test
            self.assertEqual(proc.returncode, 0)
        finally:
            if proc is not None:
                _terminate(proc)
            if dummy.poll() is None:
                dummy.terminate()
                dummy.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()

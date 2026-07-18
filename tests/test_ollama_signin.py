"""Unit tests for orchestrator/providers/ollama_signin.py's poll loop.

`asyncio.create_subprocess_exec` is mocked throughout — these tests never
launch a real `ollama` process or touch the network/browser. Timing uses
tiny poll_interval_s/timeout_s so the whole suite stays fast.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from orchestrator.providers.ollama_signin import (
    OllamaSigninFailed,
    OllamaSigninTimeout,
    OllamaSigninUnavailable,
    trigger_signin_and_wait,
)


class _FakeStdout:
    def __init__(self, data: bytes):
        self._data = data

    async def read(self):
        return self._data


class _FakeProc:
    """A fake `ollama signin` child: exits with `returncode` after `delay`
    seconds (absolute, not per-call — repeated `wait()` calls during the
    poll loop must not each re-sleep the full delay)."""

    def __init__(self, delay: float, returncode: int, stdout_data: bytes = b""):
        self._end_time = asyncio.get_event_loop().time() + delay
        self.returncode = None
        self._returncode = returncode
        self.stdout = _FakeStdout(stdout_data)
        self.killed = False

    async def wait(self):
        remaining = self._end_time - asyncio.get_event_loop().time()
        if remaining > 0:
            await asyncio.sleep(remaining)
        self.returncode = self._returncode
        return self.returncode

    def kill(self):
        self.killed = True
        self._returncode = -9
        # A killed process's wait() must resolve immediately afterward — real
        # subprocess semantics, and needed so the post-kill `await proc.wait()`
        # reap in trigger_signin_and_wait() doesn't re-sleep the original delay.
        self._end_time = asyncio.get_event_loop().time()


async def _mock_subprocess_exec(proc: "_FakeProc"):
    """`create_subprocess_exec`-shaped async factory returning `proc`.

    Built and awaited from INSIDE the running loop (a fresh coroutine per
    call, as `asyncio.create_subprocess_exec` itself is) so `_FakeProc`'s
    `asyncio.get_event_loop().time()` call has a real running loop —
    building it outside `asyncio.run()` raised "no current event loop".
    """
    return proc


class OllamaSigninTests(unittest.TestCase):
    def test_already_signed_in_returns_immediately(self):
        async def _drive():
            proc = _FakeProc(delay=0.0, returncode=0, stdout_data=b"You are already signed in as user 'x'\n")
            with patch(
                "orchestrator.providers.ollama_signin.asyncio.create_subprocess_exec",
                lambda *a, **k: _mock_subprocess_exec(proc),
            ):
                await trigger_signin_and_wait(poll_interval_s=0.01, timeout_s=1.0)
            self.assertFalse(proc.killed)

        asyncio.run(_drive())

    def test_succeeds_after_a_few_poll_iterations(self):
        # Exits after ~3 poll intervals — exercises the actual poll loop
        # (not just the immediate-exit fast path).
        async def _drive():
            proc = _FakeProc(delay=0.03, returncode=0)
            with patch(
                "orchestrator.providers.ollama_signin.asyncio.create_subprocess_exec",
                lambda *a, **k: _mock_subprocess_exec(proc),
            ):
                await trigger_signin_and_wait(poll_interval_s=0.01, timeout_s=1.0)
            self.assertFalse(proc.killed)

        asyncio.run(_drive())

    def test_timeout_kills_the_process_and_raises(self):
        async def _drive():
            proc = _FakeProc(delay=999.0, returncode=0)  # never finishes on its own
            with patch(
                "orchestrator.providers.ollama_signin.asyncio.create_subprocess_exec",
                lambda *a, **k: _mock_subprocess_exec(proc),
            ):
                with self.assertRaises(OllamaSigninTimeout):
                    await trigger_signin_and_wait(poll_interval_s=0.01, timeout_s=0.03)
            self.assertTrue(proc.killed)

        asyncio.run(_drive())

    def test_declined_or_failed_signin_raises(self):
        async def _drive():
            proc = _FakeProc(delay=0.0, returncode=1, stdout_data=b"sign-in cancelled\n")
            with patch(
                "orchestrator.providers.ollama_signin.asyncio.create_subprocess_exec",
                lambda *a, **k: _mock_subprocess_exec(proc),
            ):
                with self.assertRaises(OllamaSigninFailed) as ctx:
                    await trigger_signin_and_wait(poll_interval_s=0.01, timeout_s=1.0)
            self.assertIn("cancelled", str(ctx.exception))

        asyncio.run(_drive())

    def test_missing_ollama_cli_raises_unavailable(self):
        with patch(
            "orchestrator.providers.ollama_signin.asyncio.create_subprocess_exec",
            AsyncMock(side_effect=FileNotFoundError()),
        ):
            with self.assertRaises(OllamaSigninUnavailable):
                asyncio.run(trigger_signin_and_wait(poll_interval_s=0.01, timeout_s=1.0))

    def test_every_raised_error_is_a_permission_error(self):
        # Callers (OllamaProvider.complete()) rely on `except PermissionError`
        # to still catch every failure mode from this module.
        for exc_cls in (OllamaSigninUnavailable, OllamaSigninTimeout, OllamaSigninFailed):
            self.assertTrue(issubclass(exc_cls, PermissionError))


if __name__ == "__main__":
    unittest.main()

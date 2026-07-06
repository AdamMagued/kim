"""The live codex stderr drain: real errors surface, benign chatter doesn't.

Codex is launched with stdin=/dev/null; because that is not a TTY, codex
prints "Reading additional input from stdin..." (and immediately gets EOF).
Surfacing that in the user-visible activity feed as a codex error is pure
noise on every run.

The old version of this file unit-tested the dead
``codex_engine.engine._drain_stderr_to`` helper (deleted with
``run_codex_subtask``). The LIVE stderr drain is the closure inside
``orchestrator.codex_bridge_service._run_async``; these tests drive that path
behaviorally with a real fake codex binary (the K4 harness pattern) and
unit-test the ``_is_benign_codex_stderr`` classifier it uses.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_bridge_harness import run_bridge

from codex_engine.engine import _is_benign_codex_stderr


def _make_stderr_emitting_binary(dir_path: Path, *lines: str, exit_code: int = 0) -> Path:
    """A fake codex binary that writes the given lines to stderr, then exits."""
    dir_path.mkdir(parents=True, exist_ok=True)
    script = dir_path / "fake-codex-stderr"
    emitted = "".join(f"    print({line!r}, file=sys.stderr)\n" for line in lines)
    script.write_text(
        f"""#!{sys.executable}
import sys

def main():
{emitted or '    pass'}
    sys.exit({exit_code})

main()
"""
    )
    script.chmod(0o755)
    return script


class LiveStderrDrainTests(unittest.IsolatedAsyncioTestCase):
    """Drive _run_async with a fake codex that talks on stderr and assert
    exactly which lines reach the user-visible activity feed."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="kim-stderr-drain-")
        self.tmp = Path(self._tmp.name)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def _run_with_stderr(self, *lines: str) -> list[str]:
        """Run the bridge with a stderr-emitting fake codex; return every
        "codex error: …" status message that was surfaced."""
        from orchestrator import codex_bridge_service as svc

        binary = _make_stderr_emitting_binary(self.tmp / "stderr-bin", *lines)
        statuses: list[str] = []
        with patch.object(svc, "_status", side_effect=statuses.append):
            await run_bridge(self.tmp, binary_override=str(binary))
        return [s for s in statuses if s.startswith("codex error:")]

    async def test_benign_stdin_notice_is_not_surfaced(self):
        """codex prints "Reading additional input from stdin..." because our
        stdin is /dev/null (not a TTY). It must never be shown to the user as
        a codex error line, while a real error on the same run still is."""
        surfaced = await self._run_with_stderr(
            "Reading additional input from stdin...",
            "error: real failure",
        )
        self.assertEqual(surfaced, ["codex error: error: real failure"])

    async def test_real_errors_are_surfaced(self):
        surfaced = await self._run_with_stderr("something exploded")
        self.assertEqual(surfaced, ["codex error: something exploded"])

    async def test_quiet_stderr_produces_no_error_statuses(self):
        surfaced = await self._run_with_stderr()
        self.assertEqual(surfaced, [])


class BenignStderrTests(unittest.TestCase):
    def test_stdin_reading_notice_is_benign(self):
        self.assertTrue(_is_benign_codex_stderr("Reading additional input from stdin..."))
        self.assertTrue(_is_benign_codex_stderr("  reading prompt from stdin  "))
        self.assertTrue(_is_benign_codex_stderr(""))

    def test_real_errors_are_not_benign(self):
        self.assertFalse(_is_benign_codex_stderr("error: something went wrong"))
        self.assertFalse(_is_benign_codex_stderr("ModuleNotFoundError: No module named codex"))
        self.assertFalse(_is_benign_codex_stderr("stream disconnected before completion"))


if __name__ == "__main__":
    unittest.main()

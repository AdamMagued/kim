"""
Tests for _drain_stderr_to() in mcp_server/tools/codex_bridge.py.

Verifies that Codex subprocess stderr is surfaced in real time as [STATUS] lines
rather than silently accumulated and only shown on non-zero exit.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from mcp_server.tools.codex_bridge import _drain_stderr_to


def _make_stderr_stream(*lines: str) -> asyncio.StreamReader:
    """Build an asyncio.StreamReader whose content is the given lines."""
    reader = asyncio.StreamReader()
    for line in lines:
        reader.feed_data((line + "\n").encode())
    reader.feed_eof()
    return reader


class DrainStderrTests(unittest.IsolatedAsyncioTestCase):
    async def test_lines_printed_with_status_prefix(self):
        """Each stderr line must be printed as [STATUS] codex: {line}."""
        stream = _make_stderr_stream("error: something went wrong")
        collected: list[str] = []

        with patch("builtins.print", side_effect=lambda *a, **kw: collected.append(a[0])):
            await _drain_stderr_to(stream, [])

        self.assertEqual(len(collected), 1)
        self.assertEqual(collected[0], "[STATUS] codex: error: something went wrong")

    async def test_multiple_lines_all_printed_in_order(self):
        """All non-empty stderr lines are printed, in order, without buffering."""
        stream = _make_stderr_stream("line one", "line two", "line three")
        collected: list[str] = []

        with patch("builtins.print", side_effect=lambda *a, **kw: collected.append(a[0])):
            await _drain_stderr_to(stream, [])

        self.assertEqual(collected, [
            "[STATUS] codex: line one",
            "[STATUS] codex: line two",
            "[STATUS] codex: line three",
        ])

    async def test_empty_lines_are_skipped(self):
        """Empty lines must not produce [STATUS] output or be accumulated."""
        stream = _make_stderr_stream("real error", "", "  ")
        accumulated: list[str] = []
        collected: list[str] = []

        with patch("builtins.print", side_effect=lambda *a, **kw: collected.append(a[0])):
            await _drain_stderr_to(stream, accumulated)

        # Only the non-empty, non-whitespace-only line is printed
        self.assertEqual(len(collected), 1)
        self.assertIn("real error", collected[0])
        # "  " (whitespace-only) is rstripped to "" and skipped
        self.assertEqual(len(accumulated), 1)

    async def test_lines_accumulated_for_exit_message(self):
        """Lines must also be appended to stderr_lines for the non-zero-exit summary."""
        stream = _make_stderr_stream("ModuleNotFoundError: No module named codex")
        accumulated: list[str] = []

        with patch("builtins.print"):
            await _drain_stderr_to(stream, accumulated)

        self.assertEqual(accumulated, ["ModuleNotFoundError: No module named codex"])

    async def test_long_line_truncated_in_status_output(self):
        """Lines longer than max_line_chars are truncated in the printed output."""
        long_line = "x" * 300
        stream = _make_stderr_stream(long_line)
        collected: list[str] = []

        with patch("builtins.print", side_effect=lambda *a, **kw: collected.append(a[0])):
            await _drain_stderr_to(stream, [], max_line_chars=200)

        self.assertEqual(len(collected), 1)
        # Printed line is truncated
        self.assertEqual(collected[0], f"[STATUS] codex: {'x' * 200}")
        # But full line is accumulated untruncated
        # (accumulated list is passed separately — test via a fresh call)

    async def test_long_line_accumulated_untruncated(self):
        """Full line content (untruncated) must be in stderr_lines for exit summary."""
        long_line = "y" * 300
        stream = _make_stderr_stream(long_line)
        accumulated: list[str] = []

        with patch("builtins.print"):
            await _drain_stderr_to(stream, accumulated, max_line_chars=200)

        # The full line is in the accumulation buffer, not truncated
        self.assertEqual(accumulated[0], long_line)

    async def test_print_called_with_flush(self):
        """print() must be called with flush=True so lines appear immediately."""
        stream = _make_stderr_stream("some error")
        flush_values: list[bool] = []

        def capture_print(*args, **kwargs):
            flush_values.append(kwargs.get("flush", False))

        with patch("builtins.print", side_effect=capture_print):
            await _drain_stderr_to(stream, [])

        self.assertEqual(flush_values, [True])

    async def test_empty_stream_produces_no_output(self):
        """An empty stderr stream must not print anything."""
        stream = _make_stderr_stream()
        collected: list[str] = []

        with patch("builtins.print", side_effect=lambda *a, **kw: collected.append(a[0])):
            await _drain_stderr_to(stream, [])

        self.assertEqual(collected, [])


if __name__ == "__main__":
    unittest.main()

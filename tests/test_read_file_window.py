"""
Tests for read_file's optional offset/limit windowing (codex-parity item 1).

Default behavior (no offset/limit args) must remain byte-identical to the
historical whole-file read. When either arg is passed, the result is
line-numbered ('<n>→<line>') with a footer noting the window when truncated.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

from mcp_server import config
from mcp_server.tools.files import handle_read_file


class _TmpDirMixin:
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self._orig_allowed = list(config.ALLOWED_PATHS)
        config.ALLOWED_PATHS.append(self.tmp)

    def tearDown(self):
        config.ALLOWED_PATHS[:] = self._orig_allowed
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read(self, name: str, **kwargs) -> str:
        args = {"path": str(self.tmp / name)}
        args.update(kwargs)
        return asyncio.run(handle_read_file(args))

    def _write(self, name: str, text: str) -> Path:
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return p


class DefaultBehaviorTests(_TmpDirMixin, unittest.TestCase):
    def test_no_args_byte_identical(self):
        text = "alpha\nbeta\ngamma\n"
        self._write("a.txt", text)
        self.assertEqual(self._read("a.txt"), text)

    def test_no_args_preserves_no_trailing_newline(self):
        text = "alpha\nbeta"
        self._write("a.txt", text)
        self.assertEqual(self._read("a.txt"), text)

    def test_no_args_never_numbered(self):
        self._write("a.txt", "one line\n")
        self.assertNotIn("→", self._read("a.txt"))


class WindowTests(_TmpDirMixin, unittest.TestCase):
    def _make_lines(self, n: int) -> None:
        self._write("n.txt", "".join(f"L{i}\n" for i in range(1, n + 1)))

    def test_offset_and_limit_window(self):
        self._make_lines(10)
        out = self._read("n.txt", offset=3, limit=2)
        self.assertEqual(
            out,
            "3→L3\n4→L4\n(showing lines 3-4 of 10 total lines)",
        )

    def test_offset_only_reads_to_eof(self):
        self._make_lines(5)
        out = self._read("n.txt", offset=4)
        self.assertEqual(out, "4→L4\n5→L5\n(showing lines 4-5 of 5 total lines)")

    def test_limit_only_starts_at_line_one(self):
        self._make_lines(5)
        out = self._read("n.txt", limit=2)
        self.assertEqual(out, "1→L1\n2→L2\n(showing lines 1-2 of 5 total lines)")

    def test_full_window_has_no_footer(self):
        self._make_lines(3)
        out = self._read("n.txt", offset=1, limit=3)
        self.assertEqual(out, "1→L1\n2→L2\n3→L3")

    def test_limit_past_eof_clamps(self):
        self._make_lines(3)
        out = self._read("n.txt", offset=2, limit=999)
        self.assertEqual(out, "2→L2\n3→L3\n(showing lines 2-3 of 3 total lines)")

    def test_offset_clamped_below_one(self):
        self._make_lines(3)
        out = self._read("n.txt", offset=0, limit=1)
        self.assertTrue(out.startswith("1→L1"))

    def test_offset_past_eof_errors(self):
        self._make_lines(3)
        out = self._read("n.txt", offset=4)
        self.assertTrue(out.startswith("ERROR:"))
        self.assertIn("past the end", out)
        self.assertIn("3 lines total", out)

    def test_non_integer_args_error(self):
        self._make_lines(3)
        out = self._read("n.txt", offset="abc")
        self.assertTrue(out.startswith("ERROR:"))

    def test_zero_limit_errors(self):
        self._make_lines(3)
        out = self._read("n.txt", limit=0)
        self.assertTrue(out.startswith("ERROR:"))

    def test_empty_file_with_window_args(self):
        self._write("empty.txt", "")
        out = self._read("empty.txt", offset=1)
        self.assertEqual(out, "(empty file: 0 lines)")


if __name__ == "__main__":
    unittest.main()

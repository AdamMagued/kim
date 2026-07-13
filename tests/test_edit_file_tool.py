"""
Tests for the `edit_file` MCP tool (mcp_server/tools/files.py).

edit_file is a surgical str-replace edit: find old_string in a file and
replace it with new_string, without rewriting the whole file. Mirrors the
sandbox-test style of tests/test_path_sandbox.py (_AllowExtraPathMixin).
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

from mcp_server import config
from mcp_server.config import _HOME
from mcp_server.tools.files import handle_edit_file


class _AllowExtraPathMixin:
    """Temporarily grant an extra allowed root (in-place mutation of the
    shared ALLOWED_PATHS list that validate_path reads)."""

    extra_paths: list[Path] = []

    def setUp(self):  # noqa: N802
        self._orig_allowed = list(config.ALLOWED_PATHS)
        config.ALLOWED_PATHS.extend(self.extra_paths)

    def tearDown(self):  # noqa: N802
        config.ALLOWED_PATHS[:] = self._orig_allowed


class _TmpDirMixin(_AllowExtraPathMixin):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.extra_paths = [self.tmp]
        super().setUp()

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _edit(self, name: str, **kwargs) -> str:
        args = {"path": str(self.tmp / name)}
        args.update(kwargs)
        return asyncio.run(handle_edit_file(args))

    def _write_raw(self, name: str, data: bytes) -> Path:
        p = self.tmp / name
        p.write_bytes(data)
        return p


# ── Happy path ───────────────────────────────────────────────────────────


class SingleReplaceTests(_TmpDirMixin, unittest.TestCase):
    def test_single_unique_replace(self):
        p = self._write_raw("a.txt", b"hello world\nsecond line\n")
        result = self._edit("a.txt", old_string="hello world", new_string="goodbye world")
        self.assertNotIn("ERROR", result)
        self.assertIn("1", result)
        self.assertEqual(p.read_text(encoding="utf-8"), "goodbye world\nsecond line\n")

    def test_result_includes_context(self):
        self._write_raw("a.txt", b"line1\nline2 target\nline3\n")
        result = self._edit("a.txt", old_string="target", new_string="replaced")
        self.assertIn("line2 target", result)


# ── Multi-occurrence handling ───────────────────────────────────────────


class MultiOccurrenceTests(_TmpDirMixin, unittest.TestCase):
    def test_multi_occurrence_rejected_without_replace_all(self):
        self._write_raw("a.txt", b"foo\nfoo\nfoo\n")
        result = self._edit("a.txt", old_string="foo", new_string="bar")
        self.assertTrue(result.startswith("ERROR:"))
        self.assertIn("3 times", result)

    def test_replace_all(self):
        p = self._write_raw("a.txt", b"foo\nfoo\nfoo\n")
        result = self._edit("a.txt", old_string="foo", new_string="bar", replace_all=True)
        self.assertNotIn("ERROR", result)
        self.assertEqual(p.read_text(encoding="utf-8"), "bar\nbar\nbar\n")

    def test_expected_occurrences_match(self):
        p = self._write_raw("a.txt", b"foo\nfoo\n")
        result = self._edit("a.txt", old_string="foo", new_string="bar", expected_occurrences=2)
        self.assertNotIn("ERROR", result)
        self.assertEqual(p.read_text(encoding="utf-8"), "bar\nbar\n")

    def test_expected_occurrences_mismatch(self):
        self._write_raw("a.txt", b"foo\nfoo\nfoo\n")
        result = self._edit("a.txt", old_string="foo", new_string="bar", expected_occurrences=2)
        self.assertTrue(result.startswith("ERROR:"))
        self.assertIn("expected_occurrences", result)

    def test_expected_occurrences_single_match_still_applies(self):
        p = self._write_raw("a.txt", b"foo\nbar\n")
        result = self._edit("a.txt", old_string="foo", new_string="baz", expected_occurrences=1)
        self.assertNotIn("ERROR", result)
        self.assertEqual(p.read_text(encoding="utf-8"), "baz\nbar\n")


# ── Not-found errors ─────────────────────────────────────────────────────


class NotFoundTests(_TmpDirMixin, unittest.TestCase):
    def test_not_found_plain(self):
        self._write_raw("a.txt", b"hello world\n")
        result = self._edit("a.txt", old_string="nope", new_string="x")
        self.assertTrue(result.startswith("ERROR:"))
        self.assertIn("not found", result)

    def test_not_found_whitespace_hint(self):
        # File has the text but with different indentation/line breaks than
        # old_string — should surface a hint, not apply it.
        self._write_raw("a.txt", b"def f():\n    return   1\n")
        result = self._edit("a.txt", old_string="return 1", new_string="return 2")
        self.assertTrue(result.startswith("ERROR:"))
        self.assertIn("whitespace-normalized match exists", result)
        # Must NOT have modified the file.
        self.assertEqual((self.tmp / "a.txt").read_bytes(), b"def f():\n    return   1\n")


# ── old==new / empty old_string ──────────────────────────────────────────


class DegenerateArgsTests(_TmpDirMixin, unittest.TestCase):
    def test_old_equals_new_rejected(self):
        self._write_raw("a.txt", b"same\n")
        result = self._edit("a.txt", old_string="same", new_string="same")
        self.assertTrue(result.startswith("ERROR:"))

    def test_empty_old_string_rejected(self):
        self._write_raw("a.txt", b"content\n")
        result = self._edit("a.txt", old_string="", new_string="new")
        self.assertTrue(result.startswith("ERROR:"))
        self.assertIn("empty", result)


# ── Sandbox denial ────────────────────────────────────────────────────────


class SandboxDenialTests(_TmpDirMixin, unittest.TestCase):
    def test_secret_file_denied(self):
        env_file = self.tmp / ".env"
        env_file.write_text("SECRET=1\n", encoding="utf-8")
        with self.assertRaises(PermissionError):
            asyncio.run(handle_edit_file({
                "path": str(env_file), "old_string": "SECRET=1", "new_string": "SECRET=2",
            }))

    def test_out_of_allowed_path_denied(self):
        outside = Path(tempfile.mkdtemp()).resolve()
        try:
            outside_file = outside / "b.txt"
            outside_file.write_text("hello\n", encoding="utf-8")
            with self.assertRaises(PermissionError):
                asyncio.run(handle_edit_file({
                    "path": str(outside_file), "old_string": "hello", "new_string": "bye",
                }))
        finally:
            shutil.rmtree(outside, ignore_errors=True)


class SandboxDenialViaServerTests(unittest.TestCase):
    """PERMISSION_ERROR formatting happens in server.py's call_tool; here we
    just confirm the underlying validate_path denial matches other tools'
    error style (a bare PermissionError, which server.py wraps as
    'PERMISSION_ERROR: ...' — see mcp_server/server.py L211-213)."""

    def test_home_ssh_denied(self):
        with self.assertRaises(PermissionError) as cm:
            asyncio.run(handle_edit_file({
                "path": str(_HOME / ".ssh" / "id_rsa"),
                "old_string": "x", "new_string": "y",
            }))
        # Same PermissionError type write_file/delete_file raise -- server.py
        # formats this uniformly as "PERMISSION_ERROR: ...".
        self.assertIsInstance(cm.exception, PermissionError)


# ── Atomicity ─────────────────────────────────────────────────────────────


class AtomicityTests(_TmpDirMixin, unittest.TestCase):
    def test_no_tmp_residue_after_edit(self):
        self._write_raw("a.txt", b"x\n")
        self._edit("a.txt", old_string="x", new_string="y")
        leftovers = list(self.tmp.glob("*.tmp"))
        self.assertEqual(leftovers, [], f"leftover tmp files: {leftovers}")

    def test_no_tmp_residue_after_failed_edit(self):
        self._write_raw("a.txt", b"x\n")
        self._edit("a.txt", old_string="nope", new_string="y")
        leftovers = list(self.tmp.glob("*.tmp"))
        self.assertEqual(leftovers, [], f"leftover tmp files after error: {leftovers}")


# ── CRLF / unicode preservation ────────────────────────────────────────────


class LineEndingAndUnicodeTests(_TmpDirMixin, unittest.TestCase):
    def test_crlf_preserved_outside_edited_span(self):
        raw = b"line1\r\nTARGET\r\nline3\r\n"
        p = self._write_raw("a.txt", raw)
        self._edit("a.txt", old_string="TARGET", new_string="REPLACED")
        out = p.read_bytes()
        self.assertEqual(out, b"line1\r\nREPLACED\r\nline3\r\n")

    def test_mixed_line_endings_untouched_elsewhere(self):
        raw = b"a\r\nb\nTARGET\r\nc\n"
        p = self._write_raw("a.txt", raw)
        self._edit("a.txt", old_string="TARGET", new_string="X")
        out = p.read_bytes()
        self.assertEqual(out, b"a\r\nb\nX\r\nc\n")

    def test_unicode_content(self):
        raw = "héllo wörld 日本語 🎉\ntarget-line\n".encode("utf-8")
        p = self._write_raw("a.txt", raw)
        self._edit("a.txt", old_string="target-line", new_string="日本語-replaced")
        out = p.read_text(encoding="utf-8")
        self.assertEqual(out, "héllo wörld 日本語 🎉\n日本語-replaced\n")


if __name__ == "__main__":
    unittest.main()

"""
Tests for mcp_server/tools/memory.py - project-scoped persistent agent memory.

All tests use a temporary directory to avoid touching the real kim_memory dir.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _make_handlers(tmp_dir: str):
    """Return (handle_write_memory, handle_read_memory, _memory_file) patched
    to use a temporary kim_memory directory instead of the real PROJECT_ROOT."""
    mem_dir = Path(tmp_dir) / "kim_memory"
    import mcp_server.tools.memory as mod

    with patch.object(mod, "_MEMORY_DIR", mem_dir):
        # Re-import helpers that close over _MEMORY_DIR by reference
        yield mod


class WriteMemoryTests(unittest.IsolatedAsyncioTestCase):

    async def test_write_and_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "kim_memory"
            import mcp_server.tools.memory as mod
            with patch.object(mod, "_MEMORY_DIR", mem_dir):
                result = await mod.handle_write_memory({"key": "db_host", "value": "localhost:5432"})
                self.assertIn("OK", result)
                self.assertIn("db_host", result)

                read = await mod.handle_read_memory({"key": "db_host"})
                self.assertEqual(read, "localhost:5432")

    async def test_write_creates_memory_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "kim_memory"
            import mcp_server.tools.memory as mod
            with patch.object(mod, "_MEMORY_DIR", mem_dir):
                self.assertFalse(mem_dir.exists())
                await mod.handle_write_memory({"key": "x", "value": "y"})
                self.assertTrue(mem_dir.exists())

    async def test_write_produces_readable_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "kim_memory"
            import mcp_server.tools.memory as mod
            with patch.object(mod, "_MEMORY_DIR", mem_dir):
                await mod.handle_write_memory({"key": "arch", "value": "FastAPI + React"})
                files = list(mem_dir.glob("*.json"))
                self.assertEqual(len(files), 1)
                data = json.loads(files[0].read_text())
                self.assertEqual(data["arch"], "FastAPI + React")

    async def test_multiple_writes_accumulate(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "kim_memory"
            import mcp_server.tools.memory as mod
            with patch.object(mod, "_MEMORY_DIR", mem_dir):
                await mod.handle_write_memory({"key": "k1", "value": "v1"})
                await mod.handle_write_memory({"key": "k2", "value": "v2"})
                data = json.loads(list(mem_dir.glob("*.json"))[0].read_text())
                self.assertEqual(data["k1"], "v1")
                self.assertEqual(data["k2"], "v2")

    async def test_overwrite_existing_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "kim_memory"
            import mcp_server.tools.memory as mod
            with patch.object(mod, "_MEMORY_DIR", mem_dir):
                await mod.handle_write_memory({"key": "status", "value": "old"})
                await mod.handle_write_memory({"key": "status", "value": "new"})
                read = await mod.handle_read_memory({"key": "status"})
                self.assertEqual(read, "new")

    async def test_missing_key_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "kim_memory"
            import mcp_server.tools.memory as mod
            with patch.object(mod, "_MEMORY_DIR", mem_dir):
                result = await mod.handle_write_memory({"value": "v"})
                self.assertIn("ERROR", result)
                self.assertIn("key", result.lower())

    async def test_key_too_long_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "kim_memory"
            import mcp_server.tools.memory as mod
            with patch.object(mod, "_MEMORY_DIR", mem_dir):
                result = await mod.handle_write_memory({"key": "x" * 300, "value": "v"})
                self.assertIn("ERROR", result)

    async def test_value_too_long_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "kim_memory"
            import mcp_server.tools.memory as mod
            with patch.object(mod, "_MEMORY_DIR", mem_dir):
                result = await mod.handle_write_memory({"key": "k", "value": "x" * 20_000})
                self.assertIn("ERROR", result)


class ReadMemoryTests(unittest.IsolatedAsyncioTestCase):

    async def test_read_empty_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "kim_memory"
            import mcp_server.tools.memory as mod
            with patch.object(mod, "_MEMORY_DIR", mem_dir):
                result = await mod.handle_read_memory({})
                self.assertIn("no memory", result.lower())

    async def test_read_missing_key_lists_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "kim_memory"
            import mcp_server.tools.memory as mod
            with patch.object(mod, "_MEMORY_DIR", mem_dir):
                await mod.handle_write_memory({"key": "alpha", "value": "a"})
                result = await mod.handle_read_memory({"key": "missing"})
                self.assertIn("NOT_FOUND", result)
                self.assertIn("alpha", result)

    async def test_read_all_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "kim_memory"
            import mcp_server.tools.memory as mod
            with patch.object(mod, "_MEMORY_DIR", mem_dir):
                await mod.handle_write_memory({"key": "a", "value": "val_a"})
                await mod.handle_write_memory({"key": "b", "value": "val_b"})
                result = await mod.handle_read_memory({})
                self.assertIn("a", result)
                self.assertIn("b", result)
                self.assertIn("2 entries", result)

    async def test_long_value_truncated_in_listing(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "kim_memory"
            import mcp_server.tools.memory as mod
            with patch.object(mod, "_MEMORY_DIR", mem_dir):
                long_val = "x" * 200
                await mod.handle_write_memory({"key": "big", "value": long_val})
                listing = await mod.handle_read_memory({})
                # Direct read returns full value
                full = await mod.handle_read_memory({"key": "big"})
                self.assertEqual(full, long_val)
                # Listing truncates
                self.assertIn("...", listing)


class CwdScopingTests(unittest.IsolatedAsyncioTestCase):

    async def test_different_cwd_uses_different_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "kim_memory"
            import mcp_server.tools.memory as mod
            with patch.object(mod, "_MEMORY_DIR", mem_dir):
                await mod.handle_write_memory({"key": "k", "value": "proj_a", "cwd": tmp + "/proj_a"})
                await mod.handle_write_memory({"key": "k", "value": "proj_b", "cwd": tmp + "/proj_b"})
                files = list(mem_dir.glob("*.json"))
                self.assertEqual(len(files), 2)

    async def test_same_cwd_uses_same_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "kim_memory"
            import mcp_server.tools.memory as mod
            with patch.object(mod, "_MEMORY_DIR", mem_dir):
                cwd = tmp + "/myproject"
                await mod.handle_write_memory({"key": "k1", "value": "v1", "cwd": cwd})
                await mod.handle_write_memory({"key": "k2", "value": "v2", "cwd": cwd})
                files = list(mem_dir.glob("*.json"))
                self.assertEqual(len(files), 1)
                data = json.loads(files[0].read_text())
                self.assertIn("k1", data)
                self.assertIn("k2", data)

    async def test_cwd_omitted_uses_project_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "kim_memory"
            import mcp_server.tools.memory as mod
            with patch.object(mod, "_MEMORY_DIR", mem_dir):
                r1 = await mod.handle_write_memory({"key": "k", "value": "v"})
                r2 = await mod.handle_write_memory({"key": "k", "value": "v", "cwd": None})
                # Both should write to the same file (PROJECT_ROOT-keyed)
                files = list(mem_dir.glob("*.json"))
                self.assertEqual(len(files), 1)


class MemoryFileNamingTests(unittest.TestCase):

    def test_filename_contains_basename(self):
        import mcp_server.tools.memory as mod
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "kim_memory"
            with patch.object(mod, "_MEMORY_DIR", mem_dir):
                path = mod._memory_file("/home/user/myproject")
                self.assertIn("myproject", path.name)

    def test_filename_has_hex_suffix(self):
        import mcp_server.tools.memory as mod
        path = mod._memory_file("/home/user/myproject")
        stem = path.stem  # e.g. "myproject-a1b2c3d4"
        parts = stem.rsplit("-", 1)
        self.assertEqual(len(parts), 2)
        # The last part should be 8 hex chars
        self.assertEqual(len(parts[1]), 8)
        self.assertTrue(all(c in "0123456789abcdef" for c in parts[1]))

    def test_different_paths_same_basename_different_files(self):
        import mcp_server.tools.memory as mod
        p1 = mod._memory_file("/projects/foo/myproject")
        p2 = mod._memory_file("/other/myproject")
        self.assertNotEqual(p1, p2)

    def test_same_path_stable_filename(self):
        import mcp_server.tools.memory as mod
        p1 = mod._memory_file("/home/user/myproject")
        p2 = mod._memory_file("/home/user/myproject")
        self.assertEqual(p1, p2)


_REGISTRY_PY = Path(__file__).resolve().parent.parent / "mcp_server" / "tool_registry.py"


class RegistryStaticTests(unittest.TestCase):
    """Static source scan - tool_registry.py imports mcp which is not available
    in the test Python 3.9 env (only in the project venv).  Source scanning is
    the established pattern for these checks throughout this project."""

    def setUp(self):
        self.source = _REGISTRY_PY.read_text(encoding="utf-8")

    def test_write_memory_registered_as_tool(self):
        self.assertIn('"write_memory"', self.source)

    def test_read_memory_registered_as_tool(self):
        self.assertIn('"read_memory"', self.source)

    def test_handlers_imported_from_memory_module(self):
        self.assertIn("from mcp_server.tools.memory import", self.source)
        self.assertIn("handle_write_memory", self.source)
        self.assertIn("handle_read_memory", self.source)

    def test_memory_dispatch_wired_into_aggregate(self):
        self.assertIn("_MEMORY_DISPATCH", self.source)

    def test_memory_tools_added_to_tools_list(self):
        self.assertIn("_MEMORY_TOOLS", self.source)

    def test_write_memory_requires_key_and_value(self):
        # The schema's required list must contain both key and value.
        # Use a wide window - the description text is long.
        idx = self.source.find('"write_memory"')
        snippet = self.source[idx:idx + 1200]
        self.assertIn('"key"', snippet)
        self.assertIn('"value"', snippet)
        self.assertIn('"required"', snippet)


class CorruptedFileTests(unittest.IsolatedAsyncioTestCase):
    """_load silently returns {} for corrupt/non-dict files; callers behave predictably."""

    async def test_read_corrupt_json_returns_no_memory_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "kim_memory"
            import mcp_server.tools.memory as mod
            with patch.object(mod, "_MEMORY_DIR", mem_dir):
                # Pre-create the memory dir and write a corrupt file
                mem_dir.mkdir(parents=True)
                corrupt_path = mod._memory_file(None)
                corrupt_path.write_text("this is not json", encoding="utf-8")
                result = await mod.handle_read_memory({})
                self.assertIn("no memory", result.lower())

    async def test_read_json_array_returns_no_memory_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "kim_memory"
            import mcp_server.tools.memory as mod
            with patch.object(mod, "_MEMORY_DIR", mem_dir):
                mem_dir.mkdir(parents=True)
                array_path = mod._memory_file(None)
                array_path.write_text('["a", "b", "c"]', encoding="utf-8")
                result = await mod.handle_read_memory({})
                self.assertIn("no memory", result.lower())

    async def test_write_into_corrupt_file_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "kim_memory"
            import mcp_server.tools.memory as mod
            with patch.object(mod, "_MEMORY_DIR", mem_dir):
                mem_dir.mkdir(parents=True)
                corrupt_path = mod._memory_file(None)
                corrupt_path.write_text("not json at all", encoding="utf-8")
                result = await mod.handle_write_memory({"key": "k", "value": "v"})
                self.assertIn("OK", result)
                read = await mod.handle_read_memory({"key": "k"})
                self.assertEqual(read, "v")

    async def test_cwd_whitespace_only_uses_project_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "kim_memory"
            import mcp_server.tools.memory as mod
            with patch.object(mod, "_MEMORY_DIR", mem_dir):
                # whitespace-only cwd must fall back to PROJECT_ROOT, same as omitted
                await mod.handle_write_memory({"key": "k", "value": "v", "cwd": None})
                await mod.handle_write_memory({"key": "k2", "value": "v2", "cwd": "   "})
                files = list(mem_dir.glob("*.json"))
                self.assertEqual(len(files), 1)


class AsciiOutputTests(unittest.IsolatedAsyncioTestCase):
    """Tool return values must be ASCII-clean so they render safely in all terminals
    and LLM contexts without encoding surprises."""

    def _is_ascii(self, s: str) -> bool:
        try:
            s.encode("ascii")
            return True
        except UnicodeEncodeError:
            return False

    async def test_write_ok_response_is_ascii(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "kim_memory"
            import mcp_server.tools.memory as mod
            with patch.object(mod, "_MEMORY_DIR", mem_dir):
                result = await mod.handle_write_memory({"key": "x", "value": "y"})
                self.assertTrue(self._is_ascii(result), f"Non-ASCII in write OK: {result!r}")

    async def test_read_empty_response_is_ascii(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "kim_memory"
            import mcp_server.tools.memory as mod
            with patch.object(mod, "_MEMORY_DIR", mem_dir):
                result = await mod.handle_read_memory({})
                self.assertTrue(self._is_ascii(result), f"Non-ASCII in empty read: {result!r}")

    async def test_not_found_response_is_ascii(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "kim_memory"
            import mcp_server.tools.memory as mod
            with patch.object(mod, "_MEMORY_DIR", mem_dir):
                await mod.handle_write_memory({"key": "a", "value": "v"})
                result = await mod.handle_read_memory({"key": "missing"})
                self.assertTrue(self._is_ascii(result), f"Non-ASCII in NOT_FOUND: {result!r}")

    async def test_listing_response_is_ascii(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "kim_memory"
            import mcp_server.tools.memory as mod
            with patch.object(mod, "_MEMORY_DIR", mem_dir):
                await mod.handle_write_memory({"key": "k", "value": "x" * 200})
                result = await mod.handle_read_memory({})
                self.assertTrue(self._is_ascii(result), f"Non-ASCII in listing: {result!r}")

    async def test_error_responses_are_ascii(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = Path(tmp) / "kim_memory"
            import mcp_server.tools.memory as mod
            with patch.object(mod, "_MEMORY_DIR", mem_dir):
                r1 = await mod.handle_write_memory({"value": "v"})
                r2 = await mod.handle_write_memory({"key": "x" * 300, "value": "v"})
                r3 = await mod.handle_write_memory({"key": "k", "value": "x" * 20_000})
                for r in (r1, r2, r3):
                    self.assertTrue(self._is_ascii(r), f"Non-ASCII in error: {r!r}")


if __name__ == "__main__":
    unittest.main()

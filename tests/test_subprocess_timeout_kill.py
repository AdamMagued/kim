"""
Contract tests for subprocess kill+reap on TimeoutError in git.py and search.py.

Both _run_git() and _run_search_cmd() use asyncio.wait_for(proc.communicate(), timeout=N).
Before this fix, a timeout cancelled communicate() but left the subprocess running.
After the fix, the TimeoutError handler calls proc.kill() then awaits proc.wait(timeout=2),
matching the pattern established for codex_bridge.py in Batch 10.

Test strategy:
  1. AST static tests — verify kill() + wait() appear inside the TimeoutError handler in source.
  2. Runtime tests — verify kill() and wait() are called when communicate() times out.
"""

from __future__ import annotations

import ast
import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

_GIT_PY = Path(__file__).resolve().parent.parent / "mcp_server" / "tools" / "git.py"
_SEARCH_PY = Path(__file__).resolve().parent.parent / "mcp_server" / "tools" / "search.py"


def _timeout_handler_calls_attr(source: str, attr: str) -> bool:
    """Return True if any except TimeoutError handler in source calls proc.<attr>()."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            exc = handler.type
            is_timeout = (
                (isinstance(exc, ast.Attribute) and exc.attr == "TimeoutError")
                or (isinstance(exc, ast.Name) and exc.id == "TimeoutError")
            )
            if not is_timeout:
                continue
            for stmt in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
                if (
                    isinstance(stmt, ast.Call)
                    and isinstance(stmt.func, ast.Attribute)
                    and stmt.func.attr == attr
                ):
                    return True
    return False


class GitTimeoutStaticTests(unittest.TestCase):
    """AST contract: _run_git TimeoutError handler must kill + reap the subprocess."""

    def setUp(self):
        self.source = _GIT_PY.read_text(encoding="utf-8")

    def test_timeout_handler_calls_kill(self):
        self.assertTrue(
            _timeout_handler_calls_attr(self.source, "kill"),
            "_run_git TimeoutError handler must call proc.kill() to terminate the subprocess",
        )

    def test_timeout_handler_calls_wait(self):
        self.assertTrue(
            _timeout_handler_calls_attr(self.source, "wait"),
            "_run_git TimeoutError handler must await proc.wait() to reap the zombie",
        )

    def test_kill_present_in_source(self):
        self.assertIn(
            "proc.kill()",
            self.source,
            "git.py must call proc.kill() on timeout",
        )

    def test_wait_for_reap_present_in_source(self):
        self.assertIn(
            "proc.wait()",
            self.source,
            "git.py must call proc.wait() to reap the process on timeout",
        )


class SearchTimeoutStaticTests(unittest.TestCase):
    """AST contract: _run_search_cmd TimeoutError handler must kill + reap the subprocess."""

    def setUp(self):
        self.source = _SEARCH_PY.read_text(encoding="utf-8")

    def test_timeout_handler_calls_kill(self):
        self.assertTrue(
            _timeout_handler_calls_attr(self.source, "kill"),
            "_run_search_cmd TimeoutError handler must call proc.kill() to terminate the subprocess",
        )

    def test_timeout_handler_calls_wait(self):
        self.assertTrue(
            _timeout_handler_calls_attr(self.source, "wait"),
            "_run_search_cmd TimeoutError handler must await proc.wait() to reap the zombie",
        )

    def test_kill_present_in_source(self):
        self.assertIn(
            "proc.kill()",
            self.source,
            "search.py must call proc.kill() on timeout",
        )

    def test_wait_for_reap_present_in_source(self):
        self.assertIn(
            "proc.wait()",
            self.source,
            "search.py must call proc.wait() to reap the process on timeout",
        )


class GitTimeoutRuntimeTests(unittest.IsolatedAsyncioTestCase):
    """Runtime: verify kill() and wait() are called when git communicate() times out."""

    async def test_kill_and_wait_called_on_timeout(self):
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.kill = MagicMock()
        mock_proc.wait = MagicMock(return_value=None)
        mock_proc.communicate = MagicMock(return_value=None)

        with (
            patch(
                "mcp_server.tools.git.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
            patch(
                "mcp_server.tools.git.asyncio.wait_for",
                side_effect=[asyncio.TimeoutError(), None],
            ),
        ):
            from mcp_server.tools.git import _run_git
            result = await _run_git("status", timeout=1)

        self.assertIn("timed out", result)
        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_called_once()

    async def test_normal_exit_does_not_call_kill(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.kill = MagicMock()

        async def fast_communicate():
            return b"On branch main\n", b""

        mock_proc.communicate = fast_communicate

        with patch(
            "mcp_server.tools.git.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            from mcp_server.tools.git import _run_git
            result = await _run_git("status", timeout=30)

        mock_proc.kill.assert_not_called()
        self.assertIn("exit_code", result)


class SearchTimeoutRuntimeTests(unittest.IsolatedAsyncioTestCase):
    """Runtime: verify kill() and wait() are called when search communicate() times out."""

    async def test_kill_and_wait_called_on_timeout(self):
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.kill = MagicMock()
        mock_proc.wait = MagicMock(return_value=None)

        with (
            patch(
                "mcp_server.tools.search.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
            patch(
                "mcp_server.tools.search.asyncio.wait_for",
                side_effect=[asyncio.TimeoutError(), None],
            ),
        ):
            from mcp_server.tools.search import _run_search_cmd
            result = await _run_search_cmd(["grep", "pattern", "."], cwd=".", timeout=1)

        self.assertIn("timed out", result)
        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_called_once()

    async def test_normal_exit_does_not_call_kill(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.kill = MagicMock()

        async def fast_communicate():
            return b"match found\n", b""

        mock_proc.communicate = fast_communicate

        with patch(
            "mcp_server.tools.search.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            from mcp_server.tools.search import _run_search_cmd
            result = await _run_search_cmd(["grep", "match", "."], cwd=".", timeout=30)

        mock_proc.kill.assert_not_called()
        self.assertIn("match found", result)


if __name__ == "__main__":
    unittest.main()

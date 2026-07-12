"""F-A-3 regression tests for file-diff line counting.

The write-op diff in ``KimAgent._execute_tool`` counts a file's lines before
and after the tool runs. That counting is synchronous, blocking I/O, so it is
offloaded to a worker thread via ``asyncio.to_thread(_count_lines_sync, ...)``
to keep the agent's event loop responsive during large writes.

These tests pin both halves of that contract:

1. ``_count_lines_sync`` returns correct counts (behavior unchanged by the
   offload).
2. The real ``_execute_tool`` write path performs both counts OFF the event
   loop (on worker threads), and emits the exact +added/-removed diff.
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import unittest

import orchestrator.agent as agent_mod
from orchestrator.agent import KimAgent, _count_lines_sync
from orchestrator.interaction_policy import InteractionPolicy


def _make_tool_result(text: str = "tool_output") -> MagicMock:
    item = MagicMock()
    item.text = text
    result = MagicMock()
    result.content = [item]
    return result


def _make_agent() -> KimAgent:
    """A minimal KimAgent wired only for the write-op branch of _execute_tool."""
    agent = KimAgent.__new__(KimAgent)
    agent._hitl_risk_threshold = None
    agent._interaction_policy = InteractionPolicy(block_high_risk=False)
    agent.config = {}
    agent._ui_bridge = None
    agent._log = MagicMock()
    agent._session_store = MagicMock()
    agent._session_store.append_tool_event = MagicMock()
    agent.session = MagicMock()
    return agent


class CountLinesSyncTest(unittest.TestCase):
    def test_counts_lines_correctly(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "f.txt"
            f.write_text("", encoding="utf-8")
            self.assertEqual(_count_lines_sync(str(f)), 0)
            f.write_text("only line, no newline", encoding="utf-8")
            self.assertEqual(_count_lines_sync(str(f)), 1)
            f.write_text("a\nb\nc\n", encoding="utf-8")
            self.assertEqual(_count_lines_sync(str(f)), 3)
            f.write_text("a\nb\nc", encoding="utf-8")  # last line unterminated
            self.assertEqual(_count_lines_sync(str(f)), 3)


class LineCountOffloadTest(unittest.IsolatedAsyncioTestCase):
    async def test_write_diff_counts_off_the_event_loop(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "out.txt"
            target.write_text("first\nsecond\n", encoding="utf-8")  # 2 lines before

            agent = _make_agent()

            async def _call_tool(*, name: str, arguments: dict) -> MagicMock:
                # The tool "writes" the file, growing it from 2 to 5 lines.
                Path(arguments["path"]).write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
                return _make_tool_result()

            agent.session.call_tool = AsyncMock(side_effect=_call_tool)

            loop_tid = threading.get_ident()
            worker_tids: list[int] = []
            real_count = agent_mod._count_lines_sync

            def _spy(path: str) -> int:
                worker_tids.append(threading.get_ident())
                return real_count(path)

            emit_diff_spy = MagicMock()
            with (
                patch.object(agent_mod, "_count_lines_sync", _spy),
                patch.object(agent_mod, "emit_diff", emit_diff_spy),
            ):
                out = await agent._execute_tool("write_file", {"path": str(target)})

            self.assertEqual(out, "tool_output")
            # Counted exactly twice: once before, once after the write.
            self.assertEqual(len(worker_tids), 2)
            # Both counts ran on a worker thread, not the event-loop thread.
            for tid in worker_tids:
                self.assertNotEqual(tid, loop_tid)
            # 2 -> 5 lines => +3 / -0, emitted verbatim.
            emit_diff_spy.assert_called_once_with("out.txt", 3, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

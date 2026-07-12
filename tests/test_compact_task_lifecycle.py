"""F-A-5 regression tests for the native codex-compact background task.

When the codex transcript crosses its compact budget, ``_on_token_usage`` fires
a ``thread/compact/start`` request as a fire-and-forget task so the token-usage
notification pump is never blocked waiting on the (up to 30 s) compact round
trip.

These tests pin two properties of that offload:

1. The pump handler returns promptly — it does NOT await the compact request.
2. The created task is held by a strong reference (``asyncio.create_task`` keeps
   only a weak one), so it cannot be garbage-collected mid-flight, and it runs
   to completion, removing itself from the retention set when done.
"""

from __future__ import annotations

import asyncio
import unittest

from orchestrator.codex_appserver_transport import AppServerTurnRunner


def _bare_runner(client: object, *, budget: int = 500) -> AppServerTurnRunner:
    """A minimally-wired runner exercising the real ``_on_token_usage`` path.

    Only the attributes that method touches are set, mirroring the ``__new__``
    construction used by the agent-level tests.
    """
    runner = AppServerTurnRunner.__new__(AppServerTurnRunner)
    runner._client = client
    runner._compact_budget = budget
    runner._compact_fired = False
    runner._thread_id = "th_1"
    runner._background_tasks = set()
    return runner


class _BlockingClient:
    """AppServerClient double whose ``request`` blocks until released."""

    def __init__(self, gate: asyncio.Event, started: asyncio.Event) -> None:
        self._gate = gate
        self._started = started
        self.requests: list[tuple[str, dict]] = []

    async def request(self, method: str, params: dict | None = None, timeout=None) -> dict:
        self.requests.append((method, dict(params or {})))
        self._started.set()
        await self._gate.wait()
        return {}


class CompactTaskLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_compact_task_retained_and_runs_to_completion(self) -> None:
        gate = asyncio.Event()
        started = asyncio.Event()
        client = _BlockingClient(gate, started)
        runner = _bare_runner(client)

        usage = {"tokenUsage": {"total": {
            "totalTokens": 999, "inputTokens": 900, "outputTokens": 99}}}

        # The pump handler must return promptly even though the compact request
        # is still blocked — proving the request was offloaded, not awaited.
        await asyncio.wait_for(runner._on_token_usage(usage), timeout=1.0)

        # A strong reference is retained (create_task alone keeps only a weak
        # one, so the coroutine could otherwise be GC'd before it completes).
        self.assertEqual(len(runner._background_tasks), 1)
        task = next(iter(runner._background_tasks))
        self.assertFalse(task.done())

        # The compact request was actually dispatched by the offloaded task.
        await asyncio.wait_for(started.wait(), timeout=1.0)
        self.assertEqual(client.requests, [("thread/compact/start", {"threadId": "th_1"})])

        # Release it and confirm the retained task runs to completion cleanly.
        gate.set()
        await asyncio.wait_for(task, timeout=1.0)
        self.assertTrue(task.done())
        self.assertIsNone(task.exception())

        # The done-callback discards the finished task from the retention set.
        await asyncio.sleep(0)
        self.assertEqual(len(runner._background_tasks), 0)

    async def test_compact_fires_only_once_across_repeated_usage(self) -> None:
        """The one-shot ``_compact_fired`` guard survives the async offload:
        repeated over-budget notifications still create at most one task."""
        gate = asyncio.Event()
        started = asyncio.Event()
        client = _BlockingClient(gate, started)
        runner = _bare_runner(client)

        usage = {"tokenUsage": {"total": {"totalTokens": 999}}}
        await asyncio.wait_for(runner._on_token_usage(usage), timeout=1.0)
        await asyncio.wait_for(runner._on_token_usage(usage), timeout=1.0)

        self.assertEqual(len(runner._background_tasks), 1)
        self.assertTrue(runner._compact_fired)

        gate.set()
        await asyncio.wait_for(
            asyncio.gather(*list(runner._background_tasks)), timeout=1.0
        )
        # Exactly one compact request was ever dispatched.
        self.assertEqual(
            [m for m, _ in client.requests], ["thread/compact/start"]
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

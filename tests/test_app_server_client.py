"""AppServerClient unit tests against the fake app-server (Part 1).

The fake (tests/fake_app_server.py) is a real subprocess speaking
newline-delimited JSON-RPC on stdio, so these tests exercise the actual
spawn / pipe / correlation machinery — no mocks of asyncio internals.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

from codex_engine.app_server import (
    AppServerClient,
    AppServerError,
    Notification,
    ServerRequest,
    check_schema_drift,
    decline_result_for,
    parse_codex_version,
    pinned_schema_version,
)

FAKE = str(Path(__file__).resolve().parent / "fake_app_server.py")


def _fake_argv(*extra: str) -> list[str]:
    return [sys.executable, FAKE, *extra]


class AppServerClientTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = AppServerClient(_fake_argv(), default_timeout=10.0)
        await self.client.start()

    async def asyncTearDown(self):
        await self.client.stop()

    async def _next_event(self, timeout: float = 5.0):
        agen = self.client.events()
        try:
            return await asyncio.wait_for(agen.__anext__(), timeout=timeout)
        finally:
            await agen.aclose()

    # ── Handshake & correlation ──────────────────────────────────────────────

    async def test_initialize_handshake(self):
        result = await self.client.initialize()
        self.assertEqual(result.get("userAgent"), "fake-app-server/0.0.1")

    async def test_thread_start_round_trip(self):
        started = await self.client.request("thread/start", {"cwd": "/tmp"})
        self.assertEqual(started["thread"]["id"], "th_fake_1")

    async def test_out_of_order_responses_resolve_correct_futures(self):
        hold = asyncio.ensure_future(self.client.request("echo/hold"))
        await asyncio.sleep(0.05)  # let the hold request reach the fake first
        release = await self.client.request("echo/release")
        self.assertEqual(release, {"which": "release"})
        self.assertEqual(await hold, {"which": "hold"})

    async def test_request_timeout_raises(self):
        with self.assertRaises(AppServerError) as ctx:
            await self.client.request("never/respond", timeout=0.2)
        self.assertIn("timed out", str(ctx.exception))

    async def test_error_response_raises_with_message(self):
        with self.assertRaises(AppServerError) as ctx:
            await self.client.request("error/trigger")
        self.assertIn("fake failure", str(ctx.exception))

    # ── Incoming taxonomy ────────────────────────────────────────────────────

    async def test_server_request_round_trip(self):
        await self.client.request("approval/trigger")
        event = await self._next_event()
        self.assertIsInstance(event, ServerRequest)
        assert isinstance(event, ServerRequest)
        self.assertEqual(event.method, "item/commandExecution/requestApproval")
        self.assertEqual(event.params["command"], "touch x")
        await self.client.respond(event.id, {"decision": "accept"})
        echoed = await self._next_event()
        self.assertIsInstance(echoed, Notification)
        assert isinstance(echoed, Notification)
        self.assertEqual(echoed.method, "test/approvalResult")
        self.assertEqual(echoed.params["decision"], "accept")

    async def test_unknown_notification_is_yielded_not_crashed(self):
        await self.client.request("notify/unknown")
        event = await self._next_event()
        self.assertIsInstance(event, Notification)
        assert isinstance(event, Notification)
        self.assertEqual(event.method, "totally/unknown")
        self.assertEqual(event.params, {"n": 1})

    async def test_non_json_line_is_skipped(self):
        result = await self.client.request("garbage/then-ok")
        self.assertEqual(result, {"ok": True})

    # ── Death & shutdown ─────────────────────────────────────────────────────

    async def test_dead_process_fails_inflight_request_with_stderr_tail(self):
        with self.assertRaises(AppServerError) as ctx:
            await self.client.request("die")
        message = str(ctx.exception)
        self.assertIn("exited unexpectedly", message)
        self.assertIn("fake server dying now", message)

    async def test_events_stream_ends_after_stop(self):
        await self.client.stop()
        events = [ev async for ev in self.client.events()]
        self.assertEqual(events, [])

    async def test_write_after_death_raises(self):
        with self.assertRaises(AppServerError):
            await self.client.request("die")
        with self.assertRaises(AppServerError):
            await self.client.request("thread/start")


class ShutdownAutoDeclineTest(unittest.IsolatedAsyncioTestCase):
    async def test_stop_auto_declines_outstanding_server_request(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "decision.json"
            client = AppServerClient(_fake_argv(str(out)), default_timeout=10.0)
            await client.start()
            try:
                await client.request("approval/trigger")
                agen = client.events()
                event = await asyncio.wait_for(agen.__anext__(), timeout=5.0)
                await agen.aclose()
                self.assertIsInstance(event, ServerRequest)
                # Never answer it — stop() must decline on our behalf.
            finally:
                await client.stop()
            for _ in range(50):
                if out.exists():
                    break
                await asyncio.sleep(0.05)
            self.assertTrue(out.exists(), "fake never received the auto-decline")
            self.assertEqual(json.loads(out.read_text())["decision"], "decline")


class HelpersTest(unittest.TestCase):
    def test_decline_result_vocabulary_v1_vs_v2(self):
        self.assertEqual(decline_result_for("item/commandExecution/requestApproval"),
                         {"decision": "decline"})
        self.assertEqual(decline_result_for("item/fileChange/requestApproval"),
                         {"decision": "decline"})
        self.assertEqual(decline_result_for("execCommandApproval"), {"decision": "denied"})
        self.assertEqual(decline_result_for("applyPatchApproval"), {"decision": "denied"})

    def test_parse_codex_version(self):
        self.assertEqual(parse_codex_version("codex-cli 0.134.0"), (0, 134, 0))
        self.assertEqual(parse_codex_version("codex-cli 1.2.30\n"), (1, 2, 30))
        self.assertIsNone(parse_codex_version("garbage"))
        self.assertIsNone(parse_codex_version(""))

    def test_pinned_schema_version_reads_snapshot(self):
        pinned = pinned_schema_version()
        self.assertIsNotNone(pinned)
        assert pinned is not None
        self.assertEqual(len(pinned), 3)

    def test_check_schema_drift_gate(self):
        pinned = pinned_schema_version()
        assert pinned is not None
        same = ".".join(map(str, pinned))
        ok, msg = check_schema_drift(f"codex-cli {same}")
        self.assertTrue(ok)
        self.assertIsNone(msg)
        # Patch drift: silent.
        ok, msg = check_schema_drift(f"codex-cli {pinned[0]}.{pinned[1]}.{pinned[2] + 7}")
        self.assertTrue(ok)
        self.assertIsNone(msg)
        # Minor drift: run, but warn.
        ok, msg = check_schema_drift(f"codex-cli {pinned[0]}.{pinned[1] + 1}.0")
        self.assertTrue(ok)
        self.assertIsNotNone(msg)
        # Major drift: refuse.
        ok, msg = check_schema_drift(f"codex-cli {pinned[0] + 1}.0.0")
        self.assertFalse(ok)
        assert msg is not None
        self.assertIn("MAJOR", msg)
        # Unparseable: not evidence of incompatibility.
        ok, msg = check_schema_drift("mystery build")
        self.assertTrue(ok)
        self.assertIsNone(msg)


if __name__ == "__main__":
    unittest.main()

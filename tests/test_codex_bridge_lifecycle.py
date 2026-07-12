"""F-H-2 / F-H-1 / F-H-8 — codex-bridge typed run-lifecycle terminal events.

The Code-tab (codex browser-bridge) path used to signal termination ONLY via
legacy magic-string lines (``TASK_COMPLETE:`` / ``[FAILED]``). Chat runs emit a
typed ``kim:run-done`` (from cli.py) which the frontend keys ``isRunning``
clearing on. This test pins the fix that makes the codex bridge emit the SAME
typed terminal event on every exit path, and that the event self-stamps the
run-identity envelope when ``KIM_RUN_ID`` / ``KIM_SESSION_ID`` are exported.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout

from orchestrator import codex_bridge_service as svc


def _capture_events(rc: int) -> list[dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        svc._emit_terminal_for_rc(rc)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


class TestCodexBridgeLifecycle(unittest.TestCase):
    def test_success_emits_typed_run_done_and_agent_done(self):
        payloads = _capture_events(0)
        self.assertEqual(len(payloads), 2)
        
        self.assertEqual(payloads[0]["type"], "run_done")
        self.assertTrue(payloads[0]["success"])
        self.assertEqual(payloads[0]["termination"], "task_complete")
        
        self.assertEqual(payloads[1]["type"], "agent_done")
        self.assertTrue(payloads[1]["success"])

    def test_failure_emits_typed_run_done_and_agent_error(self):
        payloads = _capture_events(1)
        self.assertEqual(len(payloads), 3)
        
        self.assertEqual(payloads[0]["type"], "run_done")
        self.assertFalse(payloads[0]["success"])
        self.assertEqual(payloads[0]["termination"], "failed")
        
        self.assertEqual(payloads[1]["type"], "agent_error")
        self.assertIn("bridge exited with code 1", payloads[1]["error"])
        
        self.assertEqual(payloads[2]["type"], "agent_done")
        self.assertFalse(payloads[2]["success"])

    def test_cancel_maps_to_cancelled_and_agent_cancelled(self):
        payloads = _capture_events(130)
        self.assertEqual(len(payloads), 2)
        
        self.assertEqual(payloads[0]["type"], "run_done")
        self.assertFalse(payloads[0]["success"])
        self.assertEqual(payloads[0]["termination"], "cancelled")
        
        self.assertEqual(payloads[1]["type"], "agent_cancelled")
        self.assertFalse(payloads[1]["success"])

    def test_terminal_event_carries_run_identity_envelope(self):
        """F-H-8: when the spawner exports run identity, the typed terminal
        event self-stamps run_id/session_id so the frontend can attribute the
        Code-tab run's termination to its owning session (defeats event-bleed).
        """
        prev = {k: os.environ.get(k) for k in ("KIM_RUN_ID", "KIM_SESSION_ID")}
        os.environ["KIM_RUN_ID"] = "run-abc"
        os.environ["KIM_SESSION_ID"] = "sess-xyz"
        try:
            payloads = _capture_events(0)
            payload = payloads[0]
        finally:
            for k, v in prev.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(payload.get("run_id"), "run-abc")
        self.assertEqual(payload.get("session_id"), "sess-xyz")

    def test_compact_codex_thread_resume_payload(self):
        """F-A-4: compact_codex_thread must pass the same common block on resume."""
        import asyncio
        from unittest.mock import AsyncMock
        from orchestrator.codex_appserver_transport import compact_codex_thread
        
        mock_client = AsyncMock()
        config = {
            "model": "my-model",
            "codex_bridge": {
                "approval_policy": "all",
                "sandbox_mode": "read-only",
            }
        }
        thread_state = {
            "codex_thread_id": "thread-123",
            "codex_thread_cwd": "/some/cwd",
        }
        
        async def _run():
            import os
            prev_env = os.environ.get("KIM_HITL_RISK_THRESHOLD")
            os.environ["KIM_HITL_RISK_THRESHOLD"] = "high"
            try:
                return await compact_codex_thread(
                    cwd="/some/cwd",
                    config=config,
                    thread_state=thread_state,
                    binary_path="codex",
                    client=mock_client,
                )
            finally:
                if prev_env is None:
                    os.environ.pop("KIM_HITL_RISK_THRESHOLD", None)
                else:
                    os.environ["KIM_HITL_RISK_THRESHOLD"] = prev_env
        
        ok = asyncio.run(_run())
        self.assertTrue(ok)
        
        # Verify the requests sent to client
        # Should have called resume first with common block
        calls = mock_client.request.mock_calls
        self.assertEqual(len(calls), 2)
        
        resume_call = calls[0]
        self.assertEqual(resume_call[1][0], "thread/resume")
        payload = resume_call[1][1]
        self.assertEqual(payload["threadId"], "thread-123")
        self.assertEqual(payload["cwd"], "/some/cwd")
        self.assertEqual(payload["approvalPolicy"], "all")
        self.assertEqual(payload["sandbox"], "read-only")
        self.assertEqual(payload["model"], "my-model")
        self.assertEqual(payload["modelProvider"], "kim-proxy")
        self.assertEqual(payload["config"]["model_providers.kim-proxy.env_key"], "CODEX_API_KEY")
        
        compact_call = calls[1]
        self.assertEqual(compact_call[1][0], "thread/compact/start")
        self.assertEqual(compact_call[1][1], {"threadId": "thread-123"})


if __name__ == "__main__":
    unittest.main()

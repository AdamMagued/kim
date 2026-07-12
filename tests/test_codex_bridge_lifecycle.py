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


def _capture_terminal(rc: int) -> dict:
    buf = io.StringIO()
    with redirect_stdout(buf):
        svc._emit_terminal_for_rc(rc)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    assert lines, "expected a typed terminal event line on stdout"
    return json.loads(lines[-1])


class TestCodexBridgeLifecycle(unittest.TestCase):
    def test_success_emits_typed_run_done(self):
        payload = _capture_terminal(0)
        self.assertEqual(payload["type"], "run_done")
        self.assertTrue(payload["success"])
        self.assertEqual(payload["termination"], "task_complete")

    def test_failure_emits_typed_run_done_not_success(self):
        payload = _capture_terminal(1)
        self.assertEqual(payload["type"], "run_done")
        self.assertFalse(payload["success"])
        self.assertEqual(payload["termination"], "failed")

    def test_cancel_maps_to_cancelled(self):
        payload = _capture_terminal(130)
        self.assertEqual(payload["type"], "run_done")
        self.assertFalse(payload["success"])
        self.assertEqual(payload["termination"], "cancelled")

    def test_terminal_event_carries_run_identity_envelope(self):
        """F-H-8: when the spawner exports run identity, the typed terminal
        event self-stamps run_id/session_id so the frontend can attribute the
        Code-tab run's termination to its owning session (defeats event-bleed).
        """
        prev = {k: os.environ.get(k) for k in ("KIM_RUN_ID", "KIM_SESSION_ID")}
        os.environ["KIM_RUN_ID"] = "run-abc"
        os.environ["KIM_SESSION_ID"] = "sess-xyz"
        try:
            payload = _capture_terminal(0)
        finally:
            for k, v in prev.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(payload.get("run_id"), "run-abc")
        self.assertEqual(payload.get("session_id"), "sess-xyz")


if __name__ == "__main__":
    unittest.main()

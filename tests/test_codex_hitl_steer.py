"""Finding 2 regression: the exec-path HITL stdin gate must not misread a
mid-run steer note (or any non-decision line) as an approval DENIAL.

The bridge's ``_await_hitl_decision`` used to do ``bool(data.get("approved"))``
on the next stdin line, so a steer line (which carries no ``approved`` key)
parsed as ``False`` and silently declined the pending Codex-launch approval.
It now routes each line through ``parse_decision_line``: a real accept/decline
is honored, while a steer / user-note / unparseable line is skipped and the
gate keeps waiting for the actual decision. (The app-server path already routed
steer lines to ``None`` via the same parser — this brings the exec path in line.)
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import orchestrator.codex_bridge_service as bridge_mod  # noqa: E402


class ExecHitlSteerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bridge_mod._pending_stdin_read = None
        self._r, self._w = os.pipe()
        self._stdin = os.fdopen(self._r, "r")

    def tearDown(self) -> None:
        bridge_mod._pending_stdin_read = None
        try:
            os.close(self._w)
        except OSError:
            pass
        self._stdin.close()

    async def test_steer_line_is_not_read_as_a_denial(self) -> None:
        # A steer note arrives first, THEN the real approval. The old code
        # returned False on the steer line and never saw the approval; the
        # fixed code skips the steer and honors the approval.
        with patch.object(sys, "stdin", self._stdin):
            os.write(self._w, b'{"type": "user_steer", "text": "use pytest"}\n')
            os.write(self._w, b'{"type": "hitl_approve", "approved": true}\n')
            self.assertTrue(await bridge_mod._await_hitl_decision(timeout=5.0))

    async def test_plain_text_steer_line_is_skipped(self) -> None:
        # A non-JSON user note must also be skipped, not denied.
        with patch.object(sys, "stdin", self._stdin):
            os.write(self._w, b"actually, target the failing test first\n")
            os.write(self._w, b'{"type": "approval_decision", "decision": "accept"}\n')
            self.assertTrue(await bridge_mod._await_hitl_decision(timeout=5.0))

    async def test_real_denial_still_denies(self) -> None:
        # Regression guard: an explicit decline must still parse as a denial.
        with patch.object(sys, "stdin", self._stdin):
            os.write(self._w, b'{"type": "hitl_approve", "approved": false}\n')
            self.assertFalse(await bridge_mod._await_hitl_decision(timeout=5.0))

    async def test_bare_approved_true_still_approves(self) -> None:
        # Legacy bare {"approved": bool} shape must still be honored.
        with patch.object(sys, "stdin", self._stdin):
            os.write(self._w, b'{"approved": true}\n')
            self.assertTrue(await bridge_mod._await_hitl_decision(timeout=5.0))

    async def test_steer_only_times_out_and_denies(self) -> None:
        # Steer with no following decision: the gate keeps waiting and then
        # denies on timeout (fail-closed) — it must NOT approve.
        with patch.object(sys, "stdin", self._stdin):
            os.write(self._w, b'{"type": "user_steer", "text": "hang on"}\n')
            self.assertFalse(await bridge_mod._await_hitl_decision(timeout=0.4))

    async def test_eof_denies(self) -> None:
        # Supervisor closed the pipe with no decision → deny.
        with patch.object(sys, "stdin", self._stdin):
            os.close(self._w)
            self.assertFalse(await bridge_mod._await_hitl_decision(timeout=5.0))


if __name__ == "__main__":
    unittest.main()

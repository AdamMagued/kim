"""Finding 5 regression: the exec-path HITL stdin gate must match a decision
to the pending request's id before applying it — the same T1 contract
codex_appserver_transport.py's ``_collect_decision`` already enforces.

Before this fix, ``_await_hitl_decision`` accepted the first well-formed
decision line regardless of id, so a late decision meant for an earlier,
already-timed-out prompt could authorize a completely different pending
request. Not exploitable today (single-pending-approval call graph), but
this closes the gap before a future second-approval code path makes it one.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import orchestrator.codex_bridge_service as bridge_mod  # noqa: E402


class ExecHitlRequestIdTest(unittest.IsolatedAsyncioTestCase):
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

    async def test_mismatched_id_is_discarded_not_applied(self) -> None:
        # A decision echoing a STALE request id (e.g. a late Approve for an
        # earlier, already-timed-out prompt) must be discarded — the gate
        # keeps waiting for a decision that actually matches, and denies on
        # timeout rather than being authorized by the stale one.
        with patch.object(sys, "stdin", self._stdin):
            os.write(
                self._w,
                b'{"type": "approval_decision", "id": "stale-id", "decision": "accept"}\n',
            )
            self.assertFalse(
                await bridge_mod._await_hitl_decision(
                    timeout=0.4, request_id="current-id"
                )
            )

    async def test_matching_id_is_applied(self) -> None:
        with patch.object(sys, "stdin", self._stdin):
            os.write(
                self._w,
                b'{"type": "approval_decision", "id": "current-id", "decision": "accept"}\n',
            )
            self.assertTrue(
                await bridge_mod._await_hitl_decision(
                    timeout=5.0, request_id="current-id"
                )
            )

    async def test_stale_decision_then_matching_decision_is_applied(self) -> None:
        # The stale line is skipped (not applied, not treated as a denial),
        # and the gate keeps reading until the matching decision arrives.
        with patch.object(sys, "stdin", self._stdin):
            os.write(
                self._w,
                b'{"type": "approval_decision", "id": "stale-id", "decision": "decline"}\n',
            )
            os.write(
                self._w,
                b'{"type": "approval_decision", "id": "current-id", "decision": "accept"}\n',
            )
            self.assertTrue(
                await bridge_mod._await_hitl_decision(
                    timeout=5.0, request_id="current-id"
                )
            )

    async def test_id_less_decision_still_accepted_for_backward_compat(self) -> None:
        # Legacy writers that never learned the T1 id contract must still
        # work — an id-less decision line is accepted regardless of the
        # pending request_id, matching codex_appserver_transport.py's own
        # backward-compatibility rule.
        with patch.object(sys, "stdin", self._stdin):
            os.write(self._w, b'{"approved": true}\n')
            self.assertTrue(
                await bridge_mod._await_hitl_decision(
                    timeout=5.0, request_id="current-id"
                )
            )

    async def test_no_request_id_skips_matching_entirely(self) -> None:
        # A caller that doesn't opt into id-scoped waiting (request_id=None,
        # the default) preserves the pre-fix behavior: any well-formed
        # decision is applied.
        with patch.object(sys, "stdin", self._stdin):
            os.write(
                self._w,
                b'{"type": "approval_decision", "id": "some-other-id", "decision": "accept"}\n',
            )
            self.assertTrue(await bridge_mod._await_hitl_decision(timeout=5.0))


if __name__ == "__main__":
    unittest.main()

"""Offline end-to-end smoke of the app-server transport against the REAL
codex binary (parity Part 2 acceptance, headless edition).

The full production loop runs for real:

    codex_bridge_service._run_async (transport: app-server)
      → codex app-server (REAL binary, JSON-RPC over stdio)
        → kim-proxy HTTP provider (REAL _CodexProxy, loopback, bearer auth)
          → CannedProvider (the ONLY stub: scripted browser-LLM replies)

Proves: session start + turn, real command execution inside the
workspace-write sandbox, sidecar codex_thread_id persistence, thread/resume
continuity on the NEXT message, and a native approval round-trip
(require_escalated → item/commandExecution/requestApproval → scripted
accept/decline). Skipped automatically when no codex binary is on PATH
(e.g. CI) — everything here also has fake-client coverage in
test_appserver_bridge.py / test_appserver_golden.py.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_bridge_harness import _SCRUBBED_VARS  # noqa: E402

CODEX = shutil.which(os.environ.get("CODEX_BIN", "").strip() or "codex")

_CONFIG = """\
browser_provider: {}
codex_bridge:
  transport: app-server
  task_timeout_s: 120
  approval_timeout_s: 20
"""


class CannedProvider:
    """Scripted browser-LLM replies in the codex-bridge JSON contract."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []
        self._sent_system_prompt = True

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        content = self.replies.pop(0) if self.replies else '{"text": "DONE"}'
        return {"type": "text", "content": content}


async def _run_message(
    tmp: Path,
    replies: list[str],
    *,
    task: str = "create smoke.txt",
    env_extra: dict | None = None,
    decisions: list | None = None,
) -> SimpleNamespace:
    from orchestrator import codex_bridge_service as svc
    from orchestrator import codex_appserver_transport as transport
    import codex_engine.thread_state as ts

    project = tmp / "project"
    project.mkdir(exist_ok=True)
    (project / ".git").mkdir(exist_ok=True)

    config_path = tmp / "config.yaml"
    config_path.write_text(_CONFIG)

    provider = CannedProvider(replies)
    parent_env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_VARS}
    parent_env["CODEX_BIN"] = str(CODEX)
    parent_env.update(env_extra or {})

    decision_queue = list(decisions or [])

    async def scripted_decisions(_timeout: float):
        return decision_queue.pop(0) if decision_queue else None

    args = Namespace(
        task=task,
        cwd=str(project),
        provider="browser:gemini",
        model=None,
        config=str(config_path),
        verbose=False,
    )

    buffer = io.StringIO()
    with (
        patch.object(os, "environ", parent_env),
        patch.object(ts, "_STATE_DIR", tmp / "state"),
        patch.object(svc, "create_provider", return_value=provider),
        patch.object(transport, "_read_stdin_decision", scripted_decisions),
        redirect_stdout(buffer),
    ):
        rc = await svc._run_async(args)

    with patch.object(ts, "_STATE_DIR", tmp / "state"):
        state = ts.load_thread_state(str(project), "browser:gemini")
    return SimpleNamespace(
        rc=rc, out=buffer.getvalue(), provider=provider, project=project, state=state
    )


def _typed(out: str, kind: str) -> list[dict]:
    events = []
    for line in out.splitlines():
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get("type") == kind:
            events.append(parsed)
    return events


@unittest.skipUnless(CODEX, "codex binary not installed")
class RealBinaryAppServerSmoke(unittest.IsolatedAsyncioTestCase):
    async def test_turn_executes_command_and_persists_thread_then_resumes(self):
        import tempfile

        write_reply = json.dumps({
            "text": "Creating the file.",
            "tool_calls": [{"name": "exec_command",
                            "input": {"cmd": "printf 'smoke' > smoke.txt"}}],
        })
        done_reply = json.dumps({"text": "Created smoke.txt with the marker content."})

        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            first = await _run_message(tmp, [write_reply, done_reply])
            self.assertEqual(first.rc, 0, first.out)
            # The command REALLY ran, natively, inside the sandboxed cwd.
            self.assertEqual((first.project / "smoke.txt").read_text(), "smoke")
            # Outward contract + sidecar continuity.
            self.assertIn("TASK_COMPLETE:", first.out)
            thread_id = first.state.get("codex_thread_id")
            self.assertTrue(thread_id)
            self.assertEqual(first.state.get("codex_thread_cwd"), str(first.project))
            self.assertTrue(_typed(first.out, "item_lifecycle"))
            self.assertTrue(_typed(first.out, "turn_lifecycle"))

            # Message 2: the SAME codex thread resumes (true session memory).
            second = await _run_message(
                tmp, [json.dumps({"text": "Still here — the file is already made."})],
                task="did you make the file already?",
            )
            self.assertEqual(second.rc, 0, second.out)
            self.assertEqual(second.state.get("codex_thread_id"), thread_id)
            self.assertIn("Resumed the previous codex session", second.out)

    async def test_native_approval_declined_blocks_escalated_command(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            outside = tmp / "outside-escalation-proof.txt"
            escalated = json.dumps({
                "text": "Trying an escalated write.",
                "tool_calls": [{"name": "exec_command", "input": {
                    "cmd": f"touch {outside}",
                    "sandbox_permissions": "require_escalated",
                    "justification": "smoke: prove the approval gate",
                }}],
            })
            done = json.dumps({"text": "Understood, stopping."})
            result = await _run_message(
                tmp, [escalated, done],
                env_extra={"KIM_HITL_RISK_THRESHOLD": "high", "KIM_STDIN_APPROVALS": "1"},
                decisions=[("decline", None)],
            )
            self.assertEqual(result.rc, 0, result.out)
            requests = _typed(result.out, "command_approval_request")
            self.assertEqual(len(requests), 1, result.out)
            self.assertIn("outside-escalation-proof", requests[0]["command"])
            # Declined → the escalated command never ran.
            self.assertFalse(outside.exists())

    async def test_native_approval_accept_runs_escalated_command(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            outside = tmp / "outside-approved.txt"
            escalated = json.dumps({
                "text": "Escalated write, please approve.",
                "tool_calls": [{"name": "exec_command", "input": {
                    "cmd": f"touch {outside}",
                    "sandbox_permissions": "require_escalated",
                    "justification": "smoke: approved escalation",
                }}],
            })
            done = json.dumps({"text": "Done."})
            result = await _run_message(
                tmp, [escalated, done],
                env_extra={"KIM_HITL_RISK_THRESHOLD": "high", "KIM_STDIN_APPROVALS": "1"},
                decisions=[("accept", None)],
            )
            self.assertEqual(result.rc, 0, result.out)
            self.assertEqual(len(_typed(result.out, "command_approval_request")), 1)
            # Accepted → codex actually ran it outside the workspace.
            self.assertTrue(outside.exists(), result.out)


if __name__ == "__main__":
    unittest.main()

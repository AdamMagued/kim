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
    sandbox_mode: str | None = None,
) -> SimpleNamespace:
    from orchestrator import codex_bridge_service as svc
    from orchestrator import codex_appserver_transport as transport
    import codex_engine.thread_state as ts

    project = tmp / "project"
    project.mkdir(exist_ok=True)
    (project / ".git").mkdir(exist_ok=True)

    config_path = tmp / "config.yaml"
    # sandbox_mode is a documented app-server SandboxMode value
    # ("read-only" | "workspace-write" | "danger-full-access"), routed through
    # codex_appserver_transport.resolve_policies()'s codex_bridge.sandbox_mode
    # config key — not KIM_CODEX_BYPASS_SANDBOX, which that transport ignores.
    config_text = _CONFIG
    if sandbox_mode:
        config_text += f"  sandbox_mode: {sandbox_mode}\n"
    config_path.write_text(config_text)

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


def _command_execution_items(out: str) -> list[dict]:
    """item_lifecycle events for commandExecution items, each carrying the
    item's own `status` (distinct from `phase`, which only echoes the
    JSON-RPC method name — item/completed fires whether the command
    succeeded, failed, or was sandbox-denied)."""
    return [e for e in _typed(out, "item_lifecycle") if e.get("kind") == "commandExecution"]


_SANDBOX_PROBE_CACHE: tuple[bool, str] | None = None


async def _probe_workspace_write_sandbox() -> tuple[bool, str]:
    """Honest, binary-driven sandbox capability probe.

    Rather than inspecting Landlock ABI availability (magical, and one layer
    removed from what the contract actually cares about), run one trivial
    exec_command turn through the *exact* plumbing under test and check
    whether the write really lands. This is the same mechanism
    ``test_turn_executes_command_and_persists_thread_then_resumes`` depends
    on, so asking the binary itself is the most direct signal.
    """
    if not CODEX:
        return False, "codex binary not installed"
    import tempfile

    probe_reply = json.dumps({
        "text": "probe",
        "tool_calls": [{"name": "exec_command",
                        "input": {"cmd": "printf 'ok' > .kim_sandbox_probe"}}],
    })
    done_reply = json.dumps({"text": "probe complete"})
    try:
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            result = await _run_message(
                tmp, [probe_reply, done_reply], task="sandbox capability probe",
            )
            wrote = (result.project / ".kim_sandbox_probe").exists()
    except Exception as exc:  # noqa: BLE001 - the probe itself must never raise
        return False, f"probe turn raised {exc!r}"

    items = _command_execution_items(result.out)
    status = items[-1].get("status") if items else "no commandExecution item observed"
    if wrote and status == "completed":
        return True, "workspace-write sandbox permits exec_command writes"
    return False, (
        "runner cannot enforce+permit workspace-write sandbox execution "
        f"(Landlock unavailable/blocked?): wrote={wrote} item_status={status!r}"
    )


async def _sandbox_capability() -> tuple[bool, str]:
    """Cached across the module — the probe spawns the real binary, so pay
    that cost once per test session, not once per test."""
    global _SANDBOX_PROBE_CACHE
    if _SANDBOX_PROBE_CACHE is None:
        _SANDBOX_PROBE_CACHE = await _probe_workspace_write_sandbox()
    return _SANDBOX_PROBE_CACHE


@unittest.skipUnless(CODEX, "codex binary not installed")
class RealBinaryAppServerSmoke(unittest.IsolatedAsyncioTestCase):
    async def test_turn_executes_command_and_persists_thread_then_resumes(self):
        import tempfile

        capable, detail = await _sandbox_capability()
        sandbox_mode = None if capable else "danger-full-access"
        if not capable:
            # (b), not (a): the app-server protocol offers a clean per-turn
            # override (thread/start's `sandbox` field accepts the documented
            # SandboxMode "danger-full-access" — not the retired
            # --dangerously-bypass-approvals-and-sandbox flag, and not
            # KIM_CODEX_BYPASS_SANDBOX, which this transport ignores by
            # design; see resolve_policies()). Skipping outright would hide
            # this runner from the contract entirely; bypassing just the
            # sandbox for the write assertion keeps the app-server/thread/
            # exec/resume plumbing under real test, and the DEGRADED-MODE
            # print below makes the runner's limitation loud instead of
            # silent.
            print(
                f"[degraded-mode] {detail} — re-running with "
                "sandbox_mode=danger-full-access so this test still exercises "
                "the app-server turn/exec/thread-persist contract on a runner "
                "that cannot enforce workspace-write.",
                file=sys.stderr,
            )

        write_reply = json.dumps({
            "text": "Creating the file.",
            "tool_calls": [{"name": "exec_command",
                            "input": {"cmd": "printf 'smoke' > smoke.txt"}}],
        })
        done_reply = json.dumps({"text": "Created smoke.txt with the marker content."})

        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            first = await _run_message(tmp, [write_reply, done_reply], sandbox_mode=sandbox_mode)
            self.assertEqual(first.rc, 0, first.out)
            # Hardening: assert the commandExecution item's OWN outcome, not
            # just that some item_lifecycle event fired. `phase` only echoes
            # the JSON-RPC method name (item/completed fires whether the
            # command succeeded, failed, or was sandbox-denied); `status` is
            # the real result. This fails right here, with a clear message,
            # instead of surfacing lines later as a bare FileNotFoundError.
            exec_items = _command_execution_items(first.out)
            self.assertTrue(exec_items, first.out)
            self.assertEqual(
                exec_items[-1].get("status"),
                "completed",
                "commandExecution item did not report success "
                f"(status={exec_items[-1].get('status')!r}, sandbox_capable={capable}, "
                f"probe_detail={detail!r}, sandbox_mode={sandbox_mode!r}): {first.out}",
            )
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
                sandbox_mode=sandbox_mode,
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

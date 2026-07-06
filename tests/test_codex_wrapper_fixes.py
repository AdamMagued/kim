"""Regression tests for the "code mode is a real codex wrapper" fixes.

Findings covered (audit APP_AUDIT.md, area C + external 3.5):

- C1: the app-server transport is the DEFAULT (config.yaml.example ships it;
  transport_name defaults to it; the service routes there), with `exec`
  still selectable and an automatic per-run exec fallback when the codex
  binary's protocol major version drifts from Kim's snapshot.
- C3: the exec path no longer points CODEX_HOME at a throwaway temp dir —
  the user's real codex home applies and the kim-proxy routing is layered
  on top via `-c` CLI config overrides.
- C4: codex asking the user something (item/tool/requestUserInput,
  mcpServer/elicitation/request, item/tool/call) is surfaced and answered
  with a schema-valid response instead of being silently auto-declined
  with the wrong shape.
- 3.5: the normal-completion `process.wait()` on the exec path is bounded.

(C2 — cross-message continuity via thread/resume — is pinned by
tests/test_appserver_bridge.py::ResumeOrStartTest and becomes the shipped
behavior through the C1 default flip.)
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_engine.app_server import ServerRequest, decline_result_for
from orchestrator.codex_appserver_transport import (
    AppServerTurnRunner,
    parse_user_input_line,
    run_app_server_task,
    transport_name,
)
from codex_bridge_harness import FAKE_PROXY_PORT, run_bridge
from test_appserver_bridge import FakeClient, _turn_completed

_REPO = Path(__file__).resolve().parent.parent


def _run(coro):
    return asyncio.run(coro)


# ── C1: default transport ────────────────────────────────────────────────────


class DefaultTransportTest(unittest.TestCase):
    def test_shipped_example_config_defaults_to_app_server(self):
        """config.yaml.example must ship the app-server transport."""
        import yaml

        config = yaml.safe_load((_REPO / "config.yaml.example").read_text())
        self.assertEqual(transport_name(config), "app-server")

    def test_missing_and_unknown_transport_default_to_app_server(self):
        self.assertEqual(transport_name({}), "app-server")
        self.assertEqual(transport_name({"codex_bridge": {"transport": "nonsense"}}), "app-server")

    def test_exec_stays_selectable(self):
        self.assertEqual(transport_name({"codex_bridge": {"transport": "exec"}}), "exec")


class VersionGateFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_run_app_server_task_returns_none_on_drift_with_fallback(self):
        proxy = mock.MagicMock()
        proxy._port = 1
        proxy._bearer_token = "tok"
        with mock.patch(
            "orchestrator.codex_appserver_transport.check_binary_version",
            mock.AsyncMock(return_value=(False, "major drift")),
        ):
            rc = await run_app_server_task(
                task="t", cwd="/proj", model=None, config={}, proxy=proxy,
                thread_state={}, binary_path="/bin/codex",
                version_gate_fallback=True,
            )
        self.assertIsNone(rc)

    async def test_run_app_server_task_still_fails_without_fallback(self):
        proxy = mock.MagicMock()
        proxy._port = 1
        proxy._bearer_token = "tok"
        with mock.patch(
            "orchestrator.codex_appserver_transport.check_binary_version",
            mock.AsyncMock(return_value=(False, "major drift")),
        ):
            rc = await run_app_server_task(
                task="t", cwd="/proj", model=None, config={}, proxy=proxy,
                thread_state={}, binary_path="/bin/codex",
            )
        self.assertEqual(rc, 1)

    async def test_service_degrades_to_exec_on_version_drift(self):
        """Default config + drifted binary → the run completes over exec."""
        with tempfile.TemporaryDirectory(prefix="kim-fallback-") as tmpd:
            with mock.patch(
                "orchestrator.codex_appserver_transport.check_binary_version",
                mock.AsyncMock(return_value=(False, "major drift")),
            ):
                result = await run_bridge(
                    Path(tmpd), config_yaml="browser_provider: {}\n"
                )
        self.assertEqual(result.rc, 0)
        self.assertIsNotNone(result.capture, "exec fallback never spawned codex")
        self.assertEqual(result.capture["argv"][:2], ["exec", "--json"])


# ── C3: real CODEX_HOME + -c overrides on the exec path ─────────────────────


class ExecCodexHomeTest(unittest.IsolatedAsyncioTestCase):
    async def test_exec_argv_layers_kim_proxy_via_config_overrides(self):
        with tempfile.TemporaryDirectory(prefix="kim-c3-") as tmpd:
            result = await run_bridge(Path(tmpd))
        argv = result.capture["argv"]
        overrides = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-c"]
        self.assertIn('model_provider="kim-proxy"', overrides)
        self.assertIn(
            f'model_providers.kim-proxy.base_url="http://127.0.0.1:{FAKE_PROXY_PORT}/v1"',
            overrides,
        )
        self.assertIn('model_providers.kim-proxy.env_key="CODEX_API_KEY"', overrides)
        self.assertIn('model="kim-proxy-model"', overrides)
        # Tail contract unchanged.
        self.assertEqual(argv[-3], "-C")

    async def test_exec_env_does_not_override_codex_home(self):
        with tempfile.TemporaryDirectory(prefix="kim-c3-") as tmpd:
            result = await run_bridge(Path(tmpd))
        self.assertNotIn("CODEX_HOME", result.capture["env"])

    async def test_exec_env_forwards_an_explicit_parent_codex_home(self):
        with tempfile.TemporaryDirectory(prefix="kim-c3-") as tmpd:
            result = await run_bridge(
                Path(tmpd), env={"CODEX_HOME": "/users/real/.codex"}
            )
        self.assertEqual(result.capture["env"].get("CODEX_HOME"), "/users/real/.codex")


# ── C4: user-facing server requests are surfaced, not auto-declined ─────────


def _make_runner(client, *, user_inputs=None, interactive=True, config=None):
    inputs = list(user_inputs or [])

    async def user_input_reader(_timeout: float):
        return inputs.pop(0) if inputs else None

    async def decision_reader(_timeout: float):
        return None

    runner = AppServerTurnRunner(
        task="do it",
        cwd="/proj",
        model=None,
        config=config or {"codex_bridge": {"approval_timeout_s": 1}},
        proxy_port=12345,
        bearer_token="tok",
        thread_state={},
        binary_path="/bin/codex",
        client=client,
        decision_reader=decision_reader,
        user_input_reader=user_input_reader,
        install_signal_handler=False,
    )
    runner._interactive = interactive
    return runner


class UserInputSurfacingTest(unittest.TestCase):
    def _question_request(self, req_id="rq_1"):
        return ServerRequest(
            id=req_id,
            method="item/tool/requestUserInput",
            params={
                "itemId": "it_1",
                "threadId": "th_1",
                "turnId": "turn_1",
                "questions": [
                    {
                        "id": "q1",
                        "header": "Database",
                        "question": "Which database should I use?",
                        "options": [
                            {"label": "sqlite", "description": "file-based"},
                            {"label": "postgres", "description": "server"},
                        ],
                    }
                ],
            },
        )

    def test_question_is_surfaced_and_answer_is_relayed(self):
        client = FakeClient([self._question_request(), _turn_completed()])
        answer = ({"q1": {"answers": ["sqlite"]}}, "rq_1")
        runner = _make_runner(client, user_inputs=[answer])
        statuses: list[str] = []
        events: list[tuple] = []
        with mock.patch(
            "orchestrator.codex_appserver_transport.emit_status",
            side_effect=lambda m: statuses.append(m),
        ), mock.patch(
            "orchestrator.codex_appserver_transport.emit_event",
            side_effect=lambda t, **p: events.append((t, p)),
        ):
            rc = _run(runner.run())
        self.assertEqual(rc, 0)
        # The user SAW the question…
        self.assertTrue(any("Which database should I use?" in s for s in statuses))
        self.assertTrue(any(t == "user_input_request" for t, _ in events))
        # …and codex got the actual answer, schema-valid.
        self.assertEqual(
            client.responses[0], ("rq_1", {"answers": {"q1": {"answers": ["sqlite"]}}})
        )

    def test_no_answer_yields_empty_answers_not_a_decline(self):
        client = FakeClient([self._question_request(), _turn_completed()])
        runner = _make_runner(client, user_inputs=[])
        with mock.patch("orchestrator.codex_appserver_transport.emit_status"), \
             mock.patch("orchestrator.codex_appserver_transport.emit_event"):
            rc = _run(runner.run())
        self.assertEqual(rc, 0)
        self.assertEqual(client.responses[0], ("rq_1", {"answers": {}}))

    def test_non_interactive_answers_empty_and_still_surfaces(self):
        client = FakeClient([self._question_request(), _turn_completed()])
        runner = _make_runner(client, interactive=False)
        statuses: list[str] = []
        with mock.patch(
            "orchestrator.codex_appserver_transport.emit_status",
            side_effect=lambda m: statuses.append(m),
        ), mock.patch("orchestrator.codex_appserver_transport.emit_event"):
            rc = _run(runner.run())
        self.assertEqual(rc, 0)
        self.assertEqual(client.responses[0], ("rq_1", {"answers": {}}))
        self.assertTrue(any("Which database" in s for s in statuses))

    def test_mismatched_answer_id_is_discarded(self):
        client = FakeClient([self._question_request("rq_1"), _turn_completed()])
        wrong = ({"q1": {"answers": ["postgres"]}}, "rq_OTHER")
        runner = _make_runner(client, user_inputs=[wrong])
        with mock.patch("orchestrator.codex_appserver_transport.emit_status"), \
             mock.patch("orchestrator.codex_appserver_transport.emit_event"):
            _run(runner.run())
        self.assertEqual(client.responses[0], ("rq_1", {"answers": {}}))

    def test_elicitation_is_surfaced_and_declined_validly(self):
        req = ServerRequest(
            id="rq_e",
            method="mcpServer/elicitation/request",
            params={"message": "Enter your API token", "mode": "form"},
        )
        client = FakeClient([req, _turn_completed()])
        runner = _make_runner(client)
        statuses: list[str] = []
        with mock.patch(
            "orchestrator.codex_appserver_transport.emit_status",
            side_effect=lambda m: statuses.append(m),
        ), mock.patch("orchestrator.codex_appserver_transport.emit_event"):
            rc = _run(runner.run())
        self.assertEqual(rc, 0)
        self.assertEqual(client.responses[0], ("rq_e", {"action": "decline"}))
        self.assertTrue(any("Enter your API token" in s for s in statuses))

    def test_dynamic_tool_call_gets_structured_failure(self):
        req = ServerRequest(
            id="rq_t",
            method="item/tool/call",
            params={"tool": "my_tool", "callId": "c1", "arguments": {}},
        )
        client = FakeClient([req, _turn_completed()])
        runner = _make_runner(client)
        with mock.patch("orchestrator.codex_appserver_transport.emit_status"):
            rc = _run(runner.run())
        self.assertEqual(rc, 0)
        req_id, result = client.responses[0]
        self.assertEqual(req_id, "rq_t")
        self.assertIs(result["success"], False)
        self.assertEqual(result["contentItems"][0]["type"], "inputText")


class DeclineShapeTest(unittest.TestCase):
    """stop()-time auto-declines must also be schema-valid per method (C4)."""

    def test_request_user_input_declines_with_empty_answers(self):
        self.assertEqual(decline_result_for("item/tool/requestUserInput"), {"answers": {}})

    def test_elicitation_declines_with_action(self):
        self.assertEqual(
            decline_result_for("mcpServer/elicitation/request"), {"action": "decline"}
        )

    def test_dynamic_tool_call_declines_with_failure(self):
        result = decline_result_for("item/tool/call")
        self.assertIs(result["success"], False)
        self.assertTrue(result["contentItems"])

    def test_approvals_keep_their_decision_vocabulary(self):
        self.assertEqual(decline_result_for("execCommandApproval"), {"decision": "denied"})
        self.assertEqual(
            decline_result_for("item/commandExecution/requestApproval"),
            {"decision": "decline"},
        )


class ParseUserInputLineTest(unittest.TestCase):
    def test_full_shape(self):
        line = '{"type":"user_input","id":"7","answers":{"q1":{"answers":["a","b"]}}}'
        self.assertEqual(
            parse_user_input_line(line), ({"q1": {"answers": ["a", "b"]}}, "7")
        )

    def test_bare_string_and_list_values_are_normalized(self):
        line = '{"type":"user_input","answers":{"q1":"yes","q2":["x","y"]}}'
        self.assertEqual(
            parse_user_input_line(line),
            ({"q1": {"answers": ["yes"]}, "q2": {"answers": ["x", "y"]}}, None),
        )

    def test_non_user_input_lines_are_ignored(self):
        self.assertIsNone(parse_user_input_line('{"type":"approval_decision","decision":"accept"}'))
        self.assertIsNone(parse_user_input_line("not json"))
        self.assertIsNone(parse_user_input_line('{"type":"user_input","answers":"nope"}'))


# ── 3.5: bounded wait after pipe EOF on the exec path ────────────────────────


class BoundedExecWaitTest(unittest.IsolatedAsyncioTestCase):
    async def test_child_that_lingers_after_eof_is_killed(self):
        """A codex child that closes its pipes but never exits must be reaped."""
        from orchestrator import codex_bridge_service as svc

        with tempfile.TemporaryDirectory(prefix="kim-35-") as tmpd:
            tmp = Path(tmpd)
            bin_dir = tmp / "lingerbin"
            bin_dir.mkdir()
            binary = bin_dir / "fake-codex-linger"
            binary.write_text(
                f"""#!{sys.executable}
import os, sys, time
if "--version" in sys.argv:
    sys.exit(0)
os.close(1)
os.close(2)
time.sleep(60)
"""
            )
            binary.chmod(0o755)
            with mock.patch.object(svc, "_EXEC_WAIT_TIMEOUT_S", 0.5):
                result = await asyncio.wait_for(
                    run_bridge(tmp, binary_override=str(binary)), timeout=20
                )
        # The child was killed (non-zero / signal exit), not waited on forever.
        self.assertNotEqual(result.rc, 0)


if __name__ == "__main__":
    unittest.main()

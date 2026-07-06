"""App-server transport flow tests (parity Part 2) with a fake AppServerClient.

Covers: transport flag parsing, S5 policy mapping, env building, stdin
decision-line parsing, resume-or-start, sidecar id persistence, approval
round trips (v2 + v1, timeout→decline, non-interactive→decline),
turn-event translation basics, proxy begin_turn reset, codex-side compact
trigger, and reset_thread_state preserving codex thread identity.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Optional
from unittest import mock

from codex_engine.app_server import AppServerError, Notification, ServerRequest
from codex_engine.thread_state import reset_thread_state, save_thread_state, load_thread_state
from orchestrator.codex_appserver_transport import (
    AppServerTurnRunner,
    build_appserver_env,
    parse_decision_line,
    resolve_policies,
    run_app_server_task,
    transport_name,
)


def _turn_completed(status: str = "completed", error: Optional[dict] = None) -> Notification:
    turn: dict = {"id": "turn_1", "status": status}
    if error:
        turn["error"] = error
    return Notification("turn/completed", {"threadId": "th_1", "turn": turn})


def _agent_message(text: str) -> list[Notification]:
    item = {"type": "agentMessage", "id": "msg_1", "text": text}
    return [
        Notification("item/started", {"item": item}),
        Notification("item/completed", {"item": item}),
    ]


class FakeClient:
    """Duck-typed AppServerClient double driven by a scripted event list."""

    def __init__(
        self,
        events: Optional[list] = None,
        *,
        resume_fails: bool = False,
        thread_id: str = "th_new",
    ) -> None:
        self.scripted = list(events or [])
        self.requests: list[tuple[str, dict]] = []
        self.responses: list[tuple[object, dict]] = []
        self.started = False
        self.stopped = False
        self._resume_fails = resume_fails
        self._thread_id = thread_id

    async def start(self) -> None:
        self.started = True

    async def initialize(self, client_info=None) -> dict:
        return {"userAgent": "fake"}

    async def request(self, method: str, params: Optional[dict] = None, timeout=None) -> dict:
        self.requests.append((method, dict(params or {})))
        if method == "thread/resume":
            if self._resume_fails:
                raise AppServerError("thread not found")
            return {"thread": {"id": str((params or {}).get("threadId"))}}
        if method == "thread/start":
            return {"thread": {"id": self._thread_id}}
        if method == "turn/start":
            return {"turn": {"id": "turn_1"}}
        return {}

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        pass

    async def respond(self, request_id, result: dict) -> None:
        self.responses.append((request_id, dict(result)))

    async def events(self):
        for event in self.scripted:
            yield event

    async def stop(self) -> None:
        self.stopped = True

    def stderr_tail(self) -> str:
        return ""


def _make_runner(client: FakeClient, *, thread_state: Optional[dict] = None,
                 config: Optional[dict] = None, decisions: Optional[list] = None,
                 interactive: bool = True) -> AppServerTurnRunner:
    decision_queue = list(decisions or [])

    async def reader(_timeout: float):
        return decision_queue.pop(0) if decision_queue else None

    runner = AppServerTurnRunner(
        task="make pong.html",
        cwd="/proj",
        model=None,
        config=config or {},
        proxy_port=12345,
        bearer_token="tok",
        thread_state=thread_state if thread_state is not None else {},
        binary_path="/bin/codex",
        client=client,
        decision_reader=reader,
        install_signal_handler=False,
    )
    runner._interactive = interactive
    return runner


def _run(coro):
    return asyncio.run(coro)


class TransportFlagTest(unittest.TestCase):
    def test_transport_name_default_and_aliases(self):
        self.assertEqual(transport_name({}), "exec")
        self.assertEqual(transport_name({"codex_bridge": {}}), "exec")
        self.assertEqual(transport_name({"codex_bridge": {"transport": "app-server"}}), "app-server")
        self.assertEqual(transport_name({"codex_bridge": {"transport": "APP_SERVER"}}), "app-server")
        self.assertEqual(transport_name({"codex_bridge": {"transport": "appserver"}}), "app-server")
        self.assertEqual(transport_name({"codex_bridge": {"transport": "garbage"}}), "exec")
        self.assertEqual(transport_name({"codex_bridge": {"transport": None}}), "exec")


class PolicyMappingTest(unittest.TestCase):
    def test_full_auto_maps_to_never_inside_sandbox(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("KIM_HITL_RISK_THRESHOLD", None)
            approval, sandbox = resolve_policies({})
        self.assertEqual(approval, "never")
        self.assertEqual(sandbox, "workspace-write")

    def test_threshold_set_keeps_on_request(self):
        with mock.patch.dict("os.environ", {"KIM_HITL_RISK_THRESHOLD": "high"}):
            approval, sandbox = resolve_policies({})
        self.assertEqual(approval, "on-request")
        self.assertEqual(sandbox, "workspace-write")

    def test_configured_policy_and_sandbox_win(self):
        cfg = {"codex_bridge": {"approval_policy": "untrusted", "sandbox_mode": "read-only"}}
        with mock.patch.dict("os.environ", {"KIM_HITL_RISK_THRESHOLD": "medium"}):
            approval, sandbox = resolve_policies(cfg)
        self.assertEqual(approval, "untrusted")
        self.assertEqual(sandbox, "read-only")

    def test_bypass_flag_is_ignored_s5(self):
        with mock.patch.dict(
            "os.environ",
            {"KIM_CODEX_BYPASS_SANDBOX": "1", "KIM_HITL_RISK_THRESHOLD": "high"},
        ):
            approval, sandbox = resolve_policies({})
        # No danger-full-access, no never: the flag has no effect here.
        self.assertEqual(approval, "on-request")
        self.assertEqual(sandbox, "workspace-write")


class EnvBuildingTest(unittest.TestCase):
    def test_env_minimal_with_bearer_and_approval_socket(self):
        with mock.patch.dict(
            "os.environ",
            {"KIM_APPROVAL_SOCK": "/tmp/kim.sock", "SECRET_TOKEN": "leak-me"},
        ):
            env = build_appserver_env("bearer-1")
        self.assertEqual(env["CODEX_API_KEY"], "bearer-1")
        self.assertEqual(env["KIM_APPROVAL_SOCK"], "/tmp/kim.sock")
        self.assertNotIn("SECRET_TOKEN", env)
        self.assertNotIn("CODEX_HOME", env)  # user's real ~/.codex applies
        self.assertNotIn("OPENAI_API_KEY", env)  # base_url is inline config


class DecisionLineTest(unittest.TestCase):
    def test_part3_vocabulary(self):
        line = json.dumps({"type": "approval_decision", "id": "7", "decision": "acceptForSession"})
        self.assertEqual(parse_decision_line(line), ("acceptForSession", "7"))

    def test_legacy_hitl_approve_bool(self):
        self.assertEqual(
            parse_decision_line('{"type":"hitl_approve","approved":true}'), ("accept", None)
        )
        self.assertEqual(
            parse_decision_line('{"type":"hitl_approve","approved":false,"decision":"decline"}'),
            ("decline", None),
        )

    def test_bare_approved_bool(self):
        self.assertEqual(parse_decision_line('{"approved": true}'), ("accept", None))
        self.assertEqual(parse_decision_line('{"approved": false}'), ("decline", None))

    def test_unrelated_and_garbage_lines_return_none(self):
        self.assertIsNone(parse_decision_line('{"type":"user_steer","text":"hi"}'))
        self.assertIsNone(parse_decision_line("not json"))
        self.assertIsNone(parse_decision_line('{"decision":"explode"}'))


class ResumeOrStartTest(unittest.TestCase):
    def test_fresh_start_persists_thread_id_and_cwd(self):
        state: dict = {}
        client = FakeClient([*_agent_message("done!"), _turn_completed()])
        rc = _run(_make_runner(client, thread_state=state).run())
        self.assertEqual(rc, 0)
        methods = [m for m, _ in client.requests]
        self.assertIn("thread/start", methods)
        self.assertNotIn("thread/resume", methods)
        self.assertEqual(state["codex_thread_id"], "th_new")
        self.assertEqual(state["codex_thread_cwd"], "/proj")
        self.assertTrue(client.stopped)

    def test_resume_used_when_id_and_cwd_match(self):
        state = {"codex_thread_id": "th_old", "codex_thread_cwd": "/proj"}
        client = FakeClient([_turn_completed()])
        rc = _run(_make_runner(client, thread_state=state).run())
        self.assertEqual(rc, 0)
        methods = [m for m, _ in client.requests]
        self.assertIn("thread/resume", methods)
        self.assertNotIn("thread/start", methods)
        resume_params = dict(client.requests)[("thread/resume")]
        self.assertEqual(resume_params["threadId"], "th_old")
        # Inline proxy routing must ride along on resume too.
        self.assertIn("model_providers.kim-proxy.base_url", resume_params["config"])
        self.assertEqual(state["codex_thread_id"], "th_old")

    def test_cwd_mismatch_starts_fresh(self):
        state = {"codex_thread_id": "th_old", "codex_thread_cwd": "/elsewhere"}
        client = FakeClient([_turn_completed()])
        _run(_make_runner(client, thread_state=state).run())
        methods = [m for m, _ in client.requests]
        self.assertNotIn("thread/resume", methods)
        self.assertIn("thread/start", methods)
        self.assertEqual(state["codex_thread_id"], "th_new")
        self.assertEqual(state["codex_thread_cwd"], "/proj")

    def test_resume_failure_falls_back_to_start(self):
        state = {"codex_thread_id": "th_gone", "codex_thread_cwd": "/proj"}
        client = FakeClient([_turn_completed()], resume_fails=True)
        rc = _run(_make_runner(client, thread_state=state).run())
        self.assertEqual(rc, 0)
        methods = [m for m, _ in client.requests]
        self.assertEqual(methods.count("thread/resume"), 1)
        self.assertEqual(methods.count("thread/start"), 1)
        self.assertEqual(state["codex_thread_id"], "th_new")

    def test_thread_start_params_carry_policies_and_proxy_config(self):
        client = FakeClient([_turn_completed()])
        with mock.patch.dict("os.environ", {"KIM_HITL_RISK_THRESHOLD": "high"}):
            _run(_make_runner(client).run())
        params = dict(client.requests)["thread/start"]
        self.assertEqual(params["approvalPolicy"], "on-request")
        self.assertEqual(params["sandbox"], "workspace-write")
        self.assertEqual(params["modelProvider"], "kim-proxy")
        self.assertEqual(params["cwd"], "/proj")
        self.assertEqual(
            params["config"]["model_providers.kim-proxy.base_url"],
            "http://127.0.0.1:12345/v1",
        )


def _approval_request(method: str = "item/commandExecution/requestApproval",
                      req_id: object = 5, **extra) -> ServerRequest:
    params = {
        "itemId": "call_1",
        "threadId": "th_1",
        "turnId": "turn_1",
        "command": "npx playwright install",
        "cwd": "/proj",
        "reason": "needs browsers",
        **extra,
    }
    return ServerRequest(id=req_id, method=method, params=params)


class ApprovalRoundTripTest(unittest.TestCase):
    def test_accept_decision_reaches_codex(self):
        client = FakeClient([_approval_request(), _turn_completed()])
        rc = _run(_make_runner(client, decisions=[("accept", "5")]).run())
        self.assertEqual(rc, 0)
        self.assertEqual(client.responses, [(5, {"decision": "accept"})])

    def test_accept_for_session_passes_through(self):
        client = FakeClient([_approval_request(), _turn_completed()])
        _run(_make_runner(client, decisions=[("acceptForSession", None)]).run())
        self.assertEqual(client.responses, [(5, {"decision": "acceptForSession"})])

    def test_v1_method_uses_v1_vocabulary(self):
        client = FakeClient([
            _approval_request(method="execCommandApproval", req_id=9),
            _turn_completed(),
        ])
        _run(_make_runner(client, decisions=[("acceptForSession", None)]).run())
        self.assertEqual(client.responses, [(9, {"decision": "approved_for_session"})])

    def test_timeout_declines(self):
        client = FakeClient([_approval_request(), _turn_completed()])
        _run(_make_runner(client, decisions=[]).run())  # reader returns None
        self.assertEqual(client.responses, [(5, {"decision": "decline"})])

    def test_non_interactive_auto_declines_without_reading(self):
        client = FakeClient([_approval_request(), _turn_completed()])

        async def exploding_reader(_timeout: float):
            raise AssertionError("decision reader must not be called")

        runner = _make_runner(client, interactive=False)
        runner._decision_reader = exploding_reader
        _run(runner.run())
        self.assertEqual(client.responses, [(5, {"decision": "decline"})])

    def test_unknown_server_request_is_safely_declined(self):
        client = FakeClient([
            ServerRequest(id=3, method="item/tool/requestUserInput", params={}),
            _turn_completed(),
        ])
        _run(_make_runner(client).run())
        self.assertEqual(client.responses, [(3, {"decision": "decline"})])

    def test_approval_event_is_emitted(self):
        client = FakeClient([_approval_request(networkApprovalContext={"host": "x"}),
                             _turn_completed()])
        with mock.patch(
            "orchestrator.codex_appserver_transport.emit_command_approval_request"
        ) as emitted:
            _run(_make_runner(client, decisions=[("accept", None)]).run())
        kwargs = emitted.call_args.kwargs
        self.assertEqual(kwargs["id"], "5")
        self.assertEqual(kwargs["command"], "npx playwright install")
        self.assertEqual(kwargs["cwd"], "/proj")
        self.assertEqual(kwargs["risk"], "network")
        self.assertTrue(kwargs["network"])


class TurnOutcomeTest(unittest.TestCase):
    def test_failed_turn_returns_1(self):
        client = FakeClient([_turn_completed(status="failed", error={"message": "boom"})])
        rc = _run(_make_runner(client).run())
        self.assertEqual(rc, 1)

    def test_interrupted_turn_returns_130(self):
        client = FakeClient([_turn_completed(status="interrupted")])
        rc = _run(_make_runner(client).run())
        self.assertEqual(rc, 130)

    def test_child_death_without_completion_fails(self):
        client = FakeClient([])  # events end immediately
        rc = _run(_make_runner(client).run())
        self.assertEqual(rc, 1)

    def test_answer_contract_emits_task_complete(self):
        client = FakeClient([*_agent_message("Built pong.html"), _turn_completed()])
        printed: list[str] = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(str(a[0]))):
            with mock.patch("orchestrator.codex_appserver_transport.emit_answer") as ans:
                rc = _run(_make_runner(client).run())
        self.assertEqual(rc, 0)
        ans.assert_called_once_with("Built pong.html")
        self.assertTrue(any(line.startswith("TASK_COMPLETE:") for line in printed))


class TokenBudgetTest(unittest.TestCase):
    def test_codex_compact_fires_once_over_budget(self):
        usage = Notification("thread/tokenUsage/updated", {
            "threadId": "th_1", "turnId": "turn_1",
            "tokenUsage": {"total": {"totalTokens": 999, "inputTokens": 900, "outputTokens": 99}},
        })
        client = FakeClient([usage, usage, _turn_completed()])
        config = {"codex_bridge": {"codex_compact_tokens": 500}}
        _run(_make_runner(client, config=config).run())
        compacts = [m for m, _ in client.requests if m == "thread/compact/start"]
        self.assertEqual(len(compacts), 1)

    def test_zero_budget_disables_compaction(self):
        usage = Notification("thread/tokenUsage/updated", {
            "tokenUsage": {"total": {"totalTokens": 10 ** 9}},
        })
        client = FakeClient([usage, _turn_completed()])
        config = {"codex_bridge": {"codex_compact_tokens": 0}}
        _run(_make_runner(client, config=config).run())
        self.assertNotIn("thread/compact/start", [m for m, _ in client.requests])


class RunTaskEntryTest(unittest.TestCase):
    def test_begin_turn_resets_proxy_relay_budget(self):
        class FakeProxy:
            _port = 4242
            _bearer_token = "tok"

            def __init__(self):
                self.begun = 0

            def begin_turn(self):
                self.begun += 1

        proxy = FakeProxy()
        client = FakeClient([_turn_completed()])
        with mock.patch(
            "orchestrator.codex_appserver_transport.check_binary_version",
            new=mock.AsyncMock(return_value=(True, None)),
        ):
            rc = _run(run_app_server_task(
                task="t", cwd="/proj", model=None, config={}, proxy=proxy,
                thread_state={}, binary_path="/bin/codex", client=client,
                install_signal_handler=False,
            ))
        self.assertEqual(rc, 0)
        self.assertEqual(proxy.begun, 1)

    def test_major_version_drift_refuses(self):
        proxy = mock.Mock(_port=1, _bearer_token="tok")
        with mock.patch(
            "orchestrator.codex_appserver_transport.check_binary_version",
            new=mock.AsyncMock(return_value=(False, "MAJOR drift")),
        ):
            rc = _run(run_app_server_task(
                task="t", cwd="/proj", model=None, config={}, proxy=proxy,
                thread_state={}, binary_path="/bin/codex",
                client=FakeClient([]), install_signal_handler=False,
            ))
        self.assertEqual(rc, 1)


class SidecarPreservationTest(unittest.TestCase):
    def test_reset_thread_state_preserves_codex_thread_identity(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("codex_engine.thread_state._STATE_DIR", Path(tmp)):
                save_thread_state("/proj", "browser:gemini", {
                    "sent_instructions": True,
                    "turns": 12,
                    "codex_thread_id": "th_keep",
                    "codex_thread_cwd": "/proj",
                })
                state = reset_thread_state("/proj", "browser:gemini", handoff="summary")
                self.assertEqual(state["codex_thread_id"], "th_keep")
                self.assertEqual(state["codex_thread_cwd"], "/proj")
                self.assertEqual(state["turns"], 0)
                self.assertFalse(state["sent_instructions"])
                on_disk = load_thread_state("/proj", "browser:gemini")
                self.assertEqual(on_disk["codex_thread_id"], "th_keep")

    def test_reset_without_codex_keys_stays_clean(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("codex_engine.thread_state._STATE_DIR", Path(tmp)):
                state = reset_thread_state("/proj", "browser:gemini")
                self.assertNotIn("codex_thread_id", state)


class ServiceDispatchTest(unittest.IsolatedAsyncioTestCase):
    """The service branches on codex_bridge.transport; exec stays the default."""

    async def test_transport_flag_routes_to_app_server(self):
        import tempfile
        from pathlib import Path
        from codex_bridge_harness import run_bridge
        from orchestrator import codex_bridge_service as svc

        called: dict = {}

        async def fake_run(**kwargs):
            called.update(kwargs)
            return 0

        with tempfile.TemporaryDirectory() as tmpd:
            with mock.patch.object(svc, "run_app_server_task", fake_run):
                result = await run_bridge(
                    Path(tmpd),
                    config_yaml="browser_provider: {}\ncodex_bridge:\n  transport: app-server\n",
                )
        self.assertEqual(result.rc, 0)
        self.assertEqual(called["task"], "write hello.py")
        self.assertIn("thread_state", called)
        # The legacy exec binary was never spawned on this path.
        self.assertIsNone(result.capture)

    async def test_default_config_stays_on_exec_path(self):
        import tempfile
        from pathlib import Path
        from codex_bridge_harness import run_bridge

        with tempfile.TemporaryDirectory() as tmpd:
            result = await run_bridge(Path(tmpd))
        self.assertIsNotNone(result.capture)
        argv = result.capture["argv"]
        self.assertIn("exec", argv)
        self.assertIn("--json", argv)


if __name__ == "__main__":
    unittest.main()

"""Golden-file translation test (parity Part 3 acceptance).

Feeds the REAL recorded app-server transcript
(codex_engine/appserver_schema/SAMPLE_TURN.jsonl — one canned turn including
a live v2 approval round-trip, recorded against codex-cli 0.134.0) through
the AppServerTurnRunner and asserts the exact ordered sequence of typed Kim
events that comes out the other side. If a codex upgrade changes the
protocol, re-record the sample with scripts/probe_appserver.py and this test
shows precisely what the UX would see differently.
"""

from __future__ import annotations

import asyncio
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Optional

from codex_engine.app_server import Notification, ServerRequest
from orchestrator.codex_appserver_transport import AppServerTurnRunner

SAMPLE = Path(__file__).resolve().parents[1] / "codex_engine" / "appserver_schema" / "SAMPLE_TURN.jsonl"


def _load_incoming() -> list:
    """recv protocol lines → Incoming objects (skip responses to our requests)."""
    incoming = []
    for line in SAMPLE.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("dir") != "recv":
            continue
        msg = record["msg"]
        method = msg.get("method")
        if method is None:
            continue  # response to one of our requests — handled by FakeClient
        if "id" in msg:
            incoming.append(ServerRequest(id=msg["id"], method=method,
                                          params=msg.get("params") or {}))
        else:
            incoming.append(Notification(method=method, params=msg.get("params") or {}))
    return incoming


class GoldenFakeClient:
    def __init__(self, events: list) -> None:
        self.scripted = events
        self.responses: list[tuple[object, dict]] = []

    async def start(self) -> None:
        pass

    async def initialize(self, client_info=None) -> dict:
        return {}

    async def request(self, method: str, params: Optional[dict] = None, timeout=None) -> dict:
        if method in ("thread/start", "thread/resume"):
            return {"thread": {"id": "th_golden"}}
        return {}

    def notify(self, method, params=None) -> None:
        pass

    async def respond(self, request_id, result: dict) -> None:
        self.responses.append((request_id, dict(result)))

    async def events(self):
        for event in self.scripted:
            yield event

    async def stop(self) -> None:
        pass

    def stderr_tail(self) -> str:
        return ""


def _run_sample(decision: str = "decline") -> tuple[int, list[dict], list[str], GoldenFakeClient]:
    client = GoldenFakeClient(_load_incoming())

    async def reader(_timeout: float):
        return (decision, None)

    runner = AppServerTurnRunner(
        task="create a file named kim_probe.txt",
        cwd="/proj",
        model=None,
        config={},
        proxy_port=7777,
        bearer_token="tok",
        thread_state={},
        binary_path="/bin/codex",
        client=client,
        decision_reader=reader,
        install_signal_handler=False,
    )
    runner._interactive = True
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        rc = asyncio.run(runner.run())
    typed: list[dict] = []
    raw: list[str] = []
    for line in buffer.getvalue().splitlines():
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict) and "type" in parsed:
                typed.append(parsed)
                continue
        except json.JSONDecodeError:
            pass
        raw.append(line)
    return rc, typed, raw, client


class GoldenTranslationTest(unittest.TestCase):
    def test_sample_turn_translates_to_expected_kim_events(self):
        rc, typed, raw, client = _run_sample(decision="decline")
        self.assertEqual(rc, 0)

        golden = [
            (event.get("type"), self._key_field(event)) for event in typed
        ]
        expected = [
            ("turn_lifecycle", "started"),
            # userMessage items are our own echo — no lifecycle events.
            ("item_lifecycle", "started:commandExecution"),
            ("command_approval_request", "/bin/zsh -lc 'touch ~/kim_probe_escalated.txt'"),
            ("item_lifecycle", "completed:commandExecution"),
            ("token_usage", 15),
            ("item_lifecycle", "started:agentMessage"),
            ("assistant_delta", "Probe turn complete (the command was declined)."),
            ("item_lifecycle", "completed:agentMessage"),
            ("token_usage", 30),
            ("turn_lifecycle", "completed"),
            ("answer", None),  # emitted by _finish after the stream ends
        ]
        self.assertEqual(golden, expected)

        # The recorded approval request was answered with our scripted decision.
        self.assertEqual(client.responses, [(0, {"decision": "decline"})])

        # Outward contract: final TASK_COMPLETE line + typed answer event.
        answers = [event for event in typed if event["type"] == "answer"]
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0]["text"], "Probe turn complete (the command was declined).")
        self.assertTrue(any(line.startswith("TASK_COMPLETE:") for line in raw))

    def test_sample_turn_accept_reaches_codex_untranslated(self):
        _, _, _, client = _run_sample(decision="acceptForSession")
        self.assertEqual(client.responses, [(0, {"decision": "acceptForSession"})])

    def test_approval_event_carries_command_cwd_and_amendment(self):
        _, typed, _, _ = _run_sample()
        req = next(event for event in typed if event["type"] == "command_approval_request")
        self.assertEqual(req["id"], "0")
        self.assertIn("kim_probe_escalated", req["command"])
        self.assertEqual(req["cwd"], "/tmp/kim-appserver-probe")
        self.assertEqual(req["reason"], "probe: record the approval request shape")
        self.assertEqual(req["amendment"], ["touch", "~/kim_probe_escalated.txt"])
        self.assertFalse(req["network"])

    def _key_field(self, event: dict):
        kind = event["type"]
        if kind == "turn_lifecycle":
            return event["phase"]
        if kind == "item_lifecycle":
            return f"{event['phase']}:{event['kind']}"
        if kind == "command_approval_request":
            return event["command"]
        if kind == "assistant_delta":
            return event["chunk"]
        if kind == "token_usage":
            return event["total"]
        return None

    def test_answer_events_excluded_from_stream_ordering_are_present(self):
        # (The `answer` event fires in _finish, after turn/completed — assert
        # it exists but keep the golden ordering list focused on the stream.)
        _, typed, _, _ = _run_sample()
        kinds = [event["type"] for event in typed]
        self.assertIn("answer", kinds)


if __name__ == "__main__":
    unittest.main()

"""
Contract lock-down tests (Phase 2).

These tests verify the inviolable interfaces documented in CONTRACTS.md by
exercising the REAL product code (agent methods, events_gen emitters, the
provider factory, and the tool-error classifier).

An earlier version of this file constructed its own data and asserted on it
(e.g. formatting a marker string inside the test and asserting the format it
just wrote) — those tests could never fail on any product change and have
been replaced or deleted.
"""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from tests.conftest import make_test_agent


def _capture_agent_logs(agent):
    logs: list[str] = []
    agent._log = lambda level, message: logs.append(message)
    return logs


# ── 1. Stdout Protocol Contracts (real _emit_plan_markers output) ───────────


class TestStdoutProtocolFormat(unittest.TestCase):
    """Verify the stdout marker formats the REAL agent emits match what
    consumers (ChatView parseLogLine / parsePlanFromActivity) expect."""

    def _markers_for(self, content: str) -> list[str]:
        agent = make_test_agent()
        logs = _capture_agent_logs(agent)
        agent._emit_plan_markers(content)
        return logs

    def test_plan_marker_has_no_space_before_json(self):
        logs = self._markers_for("PLAN: 2 steps\n1. First\n2. Second\n")
        plan_lines = [line for line in logs if "[PLAN]" in line]
        self.assertEqual(len(plan_lines), 1)
        line = plan_lines[0]
        # Contract: '[STATUS] [PLAN]{...}' — NO space between ] and {.
        idx = line.index("[PLAN]")
        self.assertEqual(line[idx + len("[PLAN]")], "{",
                         f"[PLAN] must be immediately followed by {{ — got: {line}")

    def test_plan_json_shape(self):
        logs = self._markers_for("PLAN: 2 steps\n1. step one\n2. step two\n")
        line = next(line for line in logs if "[PLAN]" in line)
        payload = json.loads(line.split("[PLAN]", 1)[1])
        self.assertIn("steps", payload)
        self.assertIsInstance(payload["steps"], list)
        for step in payload["steps"]:
            self.assertIsInstance(step, str)
        self.assertEqual(payload["steps"], ["step one", "step two"])

    def test_step_json_shape(self):
        logs = self._markers_for("STEP 1: Do the thing\n")
        line = next(line for line in logs if "[STEP]" in line)
        payload = json.loads(line.split("[STEP]", 1)[1])
        self.assertIsInstance(payload["index"], int)
        self.assertIsInstance(payload["name"], str)
        self.assertEqual(payload, {"index": 1, "name": "Do the thing"})

    def test_done_json_shape(self):
        logs = self._markers_for("DONE 1: Completed step\n")
        line = next(line for line in logs if "[DONE]" in line)
        payload = json.loads(line.split("[DONE]", 1)[1])
        self.assertIsInstance(payload["index"], int)
        self.assertIsInstance(payload["summary"], str)
        self.assertEqual(payload, {"index": 1, "summary": "Completed step"})

    def test_plan_steps_max_12_items_max_120_chars(self):
        steps_text = "\n".join(f"{i}. {'x' * 200}" for i in range(1, 16))
        logs = self._markers_for(f"PLAN: 15 steps\n{steps_text}\n")
        line = next(line for line in logs if "[PLAN]" in line)
        payload = json.loads(line.split("[PLAN]", 1)[1])
        self.assertLessEqual(len(payload["steps"]), 12)
        for step in payload["steps"]:
            self.assertLessEqual(len(step), 120)


class TestTypedEventEmitters(unittest.TestCase):
    """The generated events_gen emitters must write one JSON line per event
    with the wire `type` discriminator the Rust/TS decoders key on."""

    def _capture(self, fn, *args, **kwargs):
        import io
        import sys
        buf = io.BytesIO()
        real_stdout = sys.stdout

        class _Stdout:
            buffer = buf

            def fileno(self):
                raise ValueError("no fd in test")

        sys.stdout = _Stdout()
        try:
            fn(*args, **kwargs)
        finally:
            sys.stdout = real_stdout
        return buf.getvalue().decode("utf-8")

    def test_emit_status_wire_format(self):
        from orchestrator.events_gen import emit_status
        out = self._capture(emit_status, "hello world")
        record = json.loads(out.strip())
        self.assertEqual(record["type"], "status")
        self.assertEqual(record["message"], "hello world")

    def test_emit_plan_wire_format(self):
        from orchestrator.events_gen import emit_plan
        out = self._capture(emit_plan, ["a", "b"])
        record = json.loads(out.strip())
        self.assertEqual(record["type"], "plan")
        self.assertEqual(record["steps"], ["a", "b"])

    def test_emit_step_and_done_wire_format(self):
        from orchestrator.events_gen import emit_done, emit_step
        out = self._capture(emit_step, 2, {"index": 2, "name": "x"})
        record = json.loads(out.strip())
        self.assertEqual(record["type"], "step")
        self.assertEqual(record["n"], 2)
        out = self._capture(emit_done, 3)
        record = json.loads(out.strip())
        self.assertEqual(record["type"], "done")
        self.assertEqual(record["n"], 3)


# ── 2. Provider Response Shape Contracts ───────────────────────────────────


class TestProviderResponseShape(unittest.TestCase):
    """Verify a REAL provider (FakeProvider — the reference implementation of
    the response contract) produces dicts matching CONTRACTS.md."""

    def _validate_response(self, response: dict):
        self.assertIn("type", response)
        self.assertIn(response["type"], ("tool_call", "text"))
        if response["type"] == "tool_call":
            self.assertIn("tool", response)
            self.assertIn("args", response)
            self.assertIsInstance(response["tool"], str)
            self.assertIsInstance(response["args"], dict)
        elif response["type"] == "text":
            self.assertIn("content", response)
            self.assertIsInstance(response["content"], str)

    def test_fake_provider_default_responses_conform(self):
        from orchestrator.providers.fake import FakeProvider
        provider = FakeProvider()
        for _ in range(3):  # includes the looping terminal response
            response = asyncio.run(provider.complete(messages=[], tools=[], system=""))
            self._validate_response(response)

    def test_fake_provider_scripted_responses_conform(self):
        from orchestrator.providers.fake import FakeProvider
        provider = FakeProvider(responses=[
            {"type": "tool_call", "tool": "read_file", "args": {"path": "x"},
             "content": "Let me read that"},
            {"type": "text", "content": "TASK_COMPLETE: done"},
        ])
        first = asyncio.run(provider.complete(messages=[], tools=[], system=""))
        second = asyncio.run(provider.complete(messages=[], tools=[], system=""))
        self._validate_response(first)
        self._validate_response(second)
        self.assertEqual(first["type"], "tool_call")
        self.assertEqual(second["type"], "text")


# ── 3. Provider Names Contract ─────────────────────────────────────────────


class TestProviderNames(unittest.TestCase):
    """Verify the provider factory knows all documented names."""

    VALID_NAMES = ["claude", "openai", "gemini", "deepseek", "browser", "ollama"]
    EXTENDED_NAMES = ["browser:claude", "browser:chatgpt", "browser:gemini"]

    def test_create_provider_accepts_all_valid_names(self):
        from orchestrator.providers.base import create_provider
        for name in self.VALID_NAMES:
            try:
                provider = create_provider(name, {"project_root": "."})
                self.assertIsNotNone(provider)
            except (ImportError, ModuleNotFoundError, OSError):
                # Missing optional dep or credentials is OK here — this test
                # only locks down that the factory recognizes the provider name.
                pass
            except ValueError:
                self.fail(f"create_provider rejected valid name: {name!r}")

    def test_create_provider_rejects_unknown_name(self):
        from orchestrator.providers.base import create_provider
        with self.assertRaises(ValueError):
            create_provider("nonexistent_provider", {})

    def test_extended_browser_names(self):
        from orchestrator.providers.base import create_provider
        for name in self.EXTENDED_NAMES:
            try:
                provider = create_provider(name, {"project_root": "."})
                self.assertIsNotNone(provider)
            except (ImportError, ModuleNotFoundError):
                pass
            except ValueError:
                self.fail(f"create_provider rejected extended name: {name!r}")


# ── 4+5. Run-loop contracts: completion signals + tool-result format ───────


def _build_run_agent(responses):
    """Agent whose run() loop is REAL (response handling, memory, completion
    detection) with only the environment collaborators stubbed."""
    from orchestrator.providers.fake import FakeProvider

    agent = make_test_agent(provider=FakeProvider(responses=responses))
    agent._refresh_tools = AsyncMock()
    agent._tools = [
        {"name": "run_command", "description": "d", "parameters": {}},
        {"name": "task_complete", "description": "d", "parameters": {}},
    ]
    agent._check_context_pressure = AsyncMock(return_value=None)
    agent._emit_context_snapshot = lambda: None
    agent._persist_context_state_extra = lambda *a, **k: None
    agent._build_system_prompt = lambda task: "system"
    agent._is_stuck = lambda _: False
    agent._log = lambda level, msg: None
    agent._track_context_usage = lambda *a, **k: None

    from types import SimpleNamespace
    result = MagicMock()
    result.content = [SimpleNamespace(text="tool ran ok")]
    agent.session = MagicMock()
    agent.session.call_tool = AsyncMock(return_value=result)
    return agent


class TestTaskCompletionSignals(unittest.TestCase):
    """The real run loop must recognize TASK_COMPLETE / NEED_HELP text and the
    task_complete tool call, and terminate with the documented result dict."""

    def test_task_complete_text_terminates_run(self):
        agent = _build_run_agent([
            {"type": "text", "content": "All files written.\nTASK_COMPLETE: All files written successfully."},
        ])
        result = asyncio.run(agent.run("do the thing"))
        self.assertTrue(result["success"])
        self.assertEqual(result["termination"], "task_complete")
        self.assertIn("All files written successfully", result["summary"])

    def test_task_complete_case_insensitive(self):
        agent = _build_run_agent([{"type": "text", "content": "task_complete: Done"}])
        result = asyncio.run(agent.run("do the thing"))
        self.assertTrue(result["success"])
        self.assertEqual(result["termination"], "task_complete")

    def test_need_help_text_terminates_run(self):
        agent = _build_run_agent([
            {"type": "text", "content": "I'm stuck.\nNEED_HELP: Cannot find the config file."},
        ])
        result = asyncio.run(agent.run("do the thing"))
        self.assertFalse(result["success"])
        self.assertEqual(result["termination"], "need_help")
        self.assertIn("Cannot find the config file", result["summary"])

    def test_task_complete_tool_call_is_intercepted(self):
        agent = _build_run_agent([
            {"type": "tool_call", "tool": "task_complete", "args": {"summary": "All done"}},
        ])
        result = asyncio.run(agent.run("do the thing"))
        self.assertTrue(result["success"])
        self.assertEqual(result["termination"], "task_complete")
        self.assertIn("All done", result["summary"])


class TestCanonicalMessageFormat(unittest.TestCase):
    """Tool results must be fed back as user messages with the documented
    '[Tool result: <name>]' prefix — produced by the real run loop."""

    def test_tool_result_message_prefix(self):
        agent = _build_run_agent([
            {"type": "tool_call", "tool": "run_command", "args": {"command": "ls"}},
            {"type": "text", "content": "TASK_COMPLETE: done"},
        ])
        result = asyncio.run(agent.run("list files"))
        self.assertTrue(result["success"])
        tool_result_msgs = [
            m for m in agent.memory.get_messages()
            if m["role"] == "user" and isinstance(m["content"], str)
            and m["content"].startswith("[Tool result: ")
        ]
        self.assertTrue(tool_result_msgs, "no [Tool result: ...] user message produced")
        self.assertTrue(
            tool_result_msgs[0]["content"].startswith("[Tool result: run_command]"))


# ── 7. MCP Tool Error Envelope ─────────────────────────────────────────────


class TestMCPErrorEnvelope(unittest.TestCase):
    """Verify the REAL classifier recognizes the documented error prefixes."""

    def test_error_prefixes_are_recognized(self):
        from orchestrator.tool_errors import classify_tool_output
        expectations = {
            "ERROR: Something went wrong": "execution_error",
            "PERMISSION_ERROR: Something went wrong": "permission_denied",
            "OS_LIMITATION: Something went wrong": "os_limitation",
            "BLOCKED: dangerous command": "blocked",
            "TIMEOUT: took too long": "timeout",
            "NOT_FOUND: no such key": "not_found",
            "ERROR calling read_file: transport died": "internal_error",
        }
        for message, code in expectations.items():
            self.assertEqual(classify_tool_output(message), code, message)

    def test_normal_result_not_confused_with_error(self):
        from orchestrator.tool_errors import classify_tool_output
        self.assertIsNone(classify_tool_output("Written successfully"))
        self.assertIsNone(classify_tool_output(""))


if __name__ == "__main__":
    unittest.main()

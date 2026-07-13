"""Tests for issue #60 browser-provider parity tools."""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server.policy import _PATH_ARGS, _ShellAnalysis, enforce
from mcp_server.tool_registry import DISPATCH, TIER_DISPATCH, TOOLS
from mcp_server.tools.browser_parity import (
    _BACKGROUND_JOBS,
    _BACKGROUND_JOBS_LOCK,
    handle_ask_user,
    handle_background_cancel,
    handle_background_poll,
    handle_background_start,
    handle_web_search,
)
from orchestrator.providers.browser.prompt_builder import format_prompt
from orchestrator.tool_risk import classify_tool_risk


class RegistrationTests(unittest.TestCase):
    def test_all_new_tools_have_schema_dispatch_and_tier(self):
        names = {tool.name for tool in TOOLS}
        expected = {
            "web_search",
            "background_start",
            "background_poll",
            "background_cancel",
            "ask_user",
        }
        self.assertTrue(expected <= names)
        self.assertTrue(expected <= set(DISPATCH))
        self.assertIn("web_search", TIER_DISPATCH["web"])
        self.assertIn("background_start", TIER_DISPATCH["shell"])
        self.assertIn("background_poll", TIER_DISPATCH["shell"])
        self.assertIn("background_cancel", TIER_DISPATCH["shell"])
        self.assertIn("ask_user", TIER_DISPATCH["screen"])

    def test_background_start_path_argument_is_policy_registered(self):
        self.assertEqual(_PATH_ARGS["background_start"], ("cwd",))


class RiskAndPolicyTests(unittest.TestCase):
    def test_new_tool_risk_classifications(self):
        self.assertEqual(
            classify_tool_risk("background_start"),
            {"level": "high", "reason": "arbitrary_code_execution"},
        )
        self.assertEqual(
            classify_tool_risk("background_cancel"),
            {"level": "medium", "reason": "process_control"},
        )
        self.assertEqual(
            classify_tool_risk("background_poll"),
            {"level": "low", "reason": "read_only"},
        )
        self.assertEqual(
            classify_tool_risk("web_search"),
            {"level": "low", "reason": "read_only"},
        )
        self.assertEqual(
            classify_tool_risk("ask_user"),
            {"level": "low", "reason": "user_question"},
        )

    def test_background_cancel_has_human_readable_approval_preview(self):
        with patch("mcp_server.policy.hitl_threshold", return_value="medium"):
            decision = enforce("background_cancel", {"job_id": "job-123"})
        self.assertEqual(decision.action, "approve")
        self.assertEqual(decision.preview, "Cancel background command job-123")

    def test_background_start_sensitive_cwd_is_denied_before_dispatch(self):
        decision = enforce(
            "background_start",
            {"cmd": "pwd", "cwd": str(Path.home() / ".ssh")},
        )
        self.assertEqual(decision.action, "deny")
        self.assertEqual(decision.reason, "sensitive_path_argument")

    def test_background_start_uses_run_command_shell_analysis(self):
        analysis = _ShellAnalysis(effective_risk="low", binary="pwd")
        with (
            patch("mcp_server.policy._analyze_shell", return_value=analysis),
            patch("mcp_server.policy.hitl_threshold", return_value=None),
        ):
            decision = enforce("background_start", {"cmd": "pwd"})
        self.assertEqual(decision.action, "allow")
        self.assertEqual(decision.risk, "low")
        self.assertEqual(decision.signature, "background_start:bin=pwd")

    def test_escalated_background_command_requires_exact_command_approval(self):
        analysis = _ShellAnalysis(
            escalations=("non_allowlisted_binary",),
            effective_risk="high",
            binary="custom-tool",
        )
        with (
            patch("mcp_server.policy._analyze_shell", return_value=analysis),
            patch("mcp_server.policy.hitl_threshold", return_value=None),
        ):
            decision = enforce("background_start", {"cmd": "custom-tool --flag"})
        self.assertEqual(decision.action, "approve")
        self.assertEqual(decision.preview, "custom-tool --flag")
        self.assertEqual(
            decision.signature,
            "background_start:cmd=custom-tool --flag",
        )


class WebSearchAndAskUserTests(unittest.TestCase):
    def test_web_search_returns_provider_native_request(self):
        result = asyncio.run(
            handle_web_search({
                "query": "latest Python release",
                "max_results": 7,
                "recency_days": 30,
            })
        )
        payload = json.loads(result)
        self.assertEqual(payload["action"], "provider_native_web_search")
        self.assertEqual(payload["query"], "latest Python release")
        self.assertEqual(payload["max_results"], 7)
        self.assertEqual(payload["recency_days"], 30)
        self.assertIn("source names and URLs", payload["instructions"])

    def test_web_search_rejects_empty_query(self):
        result = asyncio.run(handle_web_search({"query": "  "}))
        self.assertTrue(result.startswith("ERROR:"))

    def test_ask_user_uses_need_help_protocol_and_renders_choices(self):
        result = asyncio.run(
            handle_ask_user({
                "question": "Which environment should I change?",
                "choices": ["Development", "Production"],
            })
        )
        self.assertEqual(
            result,
            "NEED_HELP: Which environment should I change?\n"
            "1. Development\n"
            "2. Production",
        )

    def test_prompt_explains_provider_native_search_and_pause_protocol(self):
        prompt, _attachments, _completion_hash, _new_sent = format_prompt(
            [{"role": "user", "content": "Do the task"}],
            [],
            "You are Kim.",
            sent_system_prompt=False,
            max_inject_chars=120000,
            use_webview_bridge=False,
        )
        self.assertIn("action=provider_native_web_search", prompt)
        self.assertIn("do not busy-loop polls", prompt)
        self.assertIn("echo that exact NEED_HELP message", prompt)


class BackgroundExecutionTests(unittest.TestCase):
    def setUp(self):
        with _BACKGROUND_JOBS_LOCK:
            _BACKGROUND_JOBS.clear()

    def tearDown(self):
        with _BACKGROUND_JOBS_LOCK:
            _BACKGROUND_JOBS.clear()

    def test_start_poll_and_completion(self):
        async def scenario() -> None:
            started = asyncio.Event()
            release = asyncio.Event()

            async def fake_run_command(args: dict) -> str:
                self.assertEqual(args, {"cmd": "slow-command", "timeout": 60})
                started.set()
                await release.wait()
                return "stdout: done\nstderr: \nexit code: 0"

            with patch(
                "mcp_server.tools.browser_parity.handle_run_command",
                side_effect=fake_run_command,
            ):
                start_payload = json.loads(
                    await handle_background_start({
                        "cmd": "slow-command",
                        "timeout": 60,
                    })
                )
                job_id = start_payload["job_id"]
                self.assertEqual(start_payload["status"], "running")
                await started.wait()

                running = json.loads(
                    await handle_background_poll({"job_id": job_id})
                )
                self.assertEqual(running["status"], "running")
                self.assertNotIn("result", running)

                release.set()
                await asyncio.sleep(0)
                completed = json.loads(
                    await handle_background_poll({"job_id": job_id})
                )
                self.assertEqual(completed["status"], "completed")
                self.assertEqual(
                    completed["result"],
                    "stdout: done\nstderr: \nexit code: 0",
                )

        asyncio.run(scenario())

    def test_cancel_waits_for_wrapped_handler_cleanup(self):
        async def scenario() -> None:
            started = asyncio.Event()
            cleaned = asyncio.Event()

            async def fake_run_command(_args: dict) -> str:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
                return "unreachable"

            with patch(
                "mcp_server.tools.browser_parity.handle_run_command",
                side_effect=fake_run_command,
            ):
                start_payload = json.loads(
                    await handle_background_start({"cmd": "slow-command"})
                )
                job_id = start_payload["job_id"]
                await started.wait()
                cancelled = json.loads(
                    await handle_background_cancel({"job_id": job_id})
                )
                self.assertEqual(cancelled["status"], "cancelled")
                self.assertTrue(cleaned.is_set())

        asyncio.run(scenario())

    def test_unknown_job_is_an_error(self):
        result = asyncio.run(handle_background_poll({"job_id": "missing"}))
        self.assertTrue(result.startswith("ERROR:"))
        self.assertIn("not found", result)


if __name__ == "__main__":
    unittest.main()

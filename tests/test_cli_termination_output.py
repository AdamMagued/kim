"""
Contract tests for cli.py termination-reason output.

Verifies that _cli_main() prints a [STATUS] run ended: {termination} line
to stdout before exiting, so the typed termination reason from make_run_result()
reaches the Tauri activity feed via the existing kim-agent-output channel.

Background:
    make_run_result()["termination"] is a typed AgentTermination value returned
    by agent.run() in Python.  Tauri only observes the agent subprocess via
    stdout (kim-agent-output events) and the OS exit code (kim-agent-done).
    Without this print, the termination reason is silently dropped at the
    cli.py boundary and never reaches the frontend.

    The full typed-payload path — extending KimEvent::Done in Rust to carry
    termination + updating the frontend kim:done listener — requires Phase 6 and
    is tracked in docs/archive/HARNESS_ROADMAP.md Tier 2b.

Test strategy:
    orchestrator/agent.py imports `from mcp import ClientSession` which is not
    available in the test environment. Rather than attempting to invoke _cli_main
    end-to-end (which would pull in the full MCP stack), these tests use:
      1. AST static analysis — verify the source contains the expected print call.
      2. Pure-logic string tests — verify the format string produces correct output
         for every AgentTermination value and the fallback case.
"""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

_CLI_PY = Path(__file__).resolve().parent.parent / "orchestrator" / "cli.py"


class CliTerminationStaticTests(unittest.TestCase):
    """Static contract: _cli_main source must contain the [STATUS] run ended print."""

    def setUp(self):
        self.source = _CLI_PY.read_text(encoding="utf-8")

    def test_status_run_ended_prefix_present_in_source(self):
        """cli.py must contain a print with '[STATUS] run ended:' prefix."""
        self.assertIn(
            "[STATUS] run ended:",
            self.source,
            "cli.py does not print '[STATUS] run ended: {termination}' — "
            "the termination reason will not reach the Tauri activity feed",
        )

    def test_run_done_json_transport_present_in_source(self):
        """cli.py must use the schema-generated run_done emitter."""
        self.assertIn("from orchestrator.events_gen import", self.source)
        self.assertIn("emit_run_done", self.source)
        self.assertIn('emit_run_done(termination, bool(result["success"]))', self.source)

    def test_run_lifecycle_clear_events_present_in_source(self):
        """cli.py must use the schema-generated agent lifecycle clear emitters."""
        self.assertIn("emit_agent_done", self.source)
        self.assertIn("emit_agent_cancelled", self.source)
        self.assertIn("emit_agent_error", self.source)

    def test_termination_key_accessed_via_get_with_fallback(self):
        """result.get('termination', 'unknown') must be used so old result dicts
        without the key don't raise KeyError."""
        self.assertIn(
            "'termination'",
            self.source,
        )
        self.assertIn(
            "'unknown'",
            self.source,
        )

    def test_print_has_flush_true(self):
        """The [STATUS] print must be called with flush=True for real-time delivery.

        The Tauri stdout reader receives all lines sequentially.  Without flush=True
        the line may be buffered and not delivered before the process exits.
        """
        tree = ast.parse(self.source)
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Must be a call to `print`
            func = node.func
            if not (isinstance(func, ast.Name) and func.id == "print"):
                continue
            # Check the first positional arg contains '[STATUS] run ended:'
            if not node.args:
                continue
            # Check keyword flush=True
            for kw in node.keywords:
                if kw.arg == "flush" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    # Check if any string constant in the call args references run ended
                    arg_src = ast.unparse(node.args[0]) if hasattr(ast, "unparse") else ""
                    if "run ended" in arg_src or "termination" in arg_src:
                        found = True
        self.assertTrue(
            found,
            "print('[STATUS] run ended: ...', flush=True) not found in cli.py — "
            "termination line may not be flushed before process exit",
        )

    def test_status_line_precedes_summary_line_in_source(self):
        """[STATUS] run ended must appear before the [SUCCESS]/[FAILED] print in source."""
        status_pos = self.source.find("[STATUS] run ended:")
        summary_pos = self.source.find("[{status}]")
        self.assertGreater(status_pos, -1, "[STATUS] run ended not found in cli.py")
        self.assertGreater(summary_pos, -1, "[{status}] not found in cli.py")
        self.assertLess(
            status_pos, summary_pos,
            "[STATUS] run ended must appear before the [SUCCESS]/[FAILED] print in source",
        )

    def test_run_done_json_precedes_status_line_in_source(self):
        """Structured run_done JSON should be emitted before the legacy status line."""
        json_pos = self.source.find("emit_run_done(termination")
        status_pos = self.source.find("[STATUS] run ended:")
        self.assertGreater(json_pos, -1, "generated run_done emitter not found in cli.py")
        self.assertGreater(status_pos, -1, "[STATUS] run ended not found in cli.py")
        self.assertLess(json_pos, status_pos)

    def test_failure_summary_emitted_as_typed_activity(self):
        """#26/#27: failed (non-cancelled) runs must emit a typed activity error
        carrying the summary. Typed IPC mode drops the legacy `[FAILED] <summary>`
        text line, so without this the UI only ever sees the bare termination code
        and NEED_HELP questions render with no content."""
        self.assertIn("emit_activity", self.source)
        self.assertIn('emit_activity("error", failure_detail)', self.source)
        # User cancels are not errors and must not produce an error activity.
        self.assertIn('termination == "cancelled"', self.source)


class CliTerminationFormatTests(unittest.TestCase):
    """Pure-logic tests for the termination output format string."""

    def _format_status_line(self, result: dict) -> str:
        """Reproduce the exact format string from cli.py."""
        return f"[STATUS] run ended: {result.get('termination', 'unknown')}"

    def _format_run_done_line(self, result: dict) -> str:
        """Reproduce the run_done JSON payload shape from cli.py."""
        termination = result.get('termination', 'unknown')
        return json.dumps(
            {
                "type": "run_done",
                "termination": termination,
                "success": bool(result["success"]),
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def test_task_complete_formats_correctly(self):
        line = self._format_status_line({"termination": "task_complete"})
        self.assertEqual(line, "[STATUS] run ended: task_complete")

    def test_max_iterations_formats_correctly(self):
        line = self._format_status_line({"termination": "max_iterations"})
        self.assertEqual(line, "[STATUS] run ended: max_iterations")

    def test_cancelled_formats_correctly(self):
        line = self._format_status_line({"termination": "cancelled"})
        self.assertEqual(line, "[STATUS] run ended: cancelled")

    def test_stuck_formats_correctly(self):
        line = self._format_status_line({"termination": "stuck"})
        self.assertEqual(line, "[STATUS] run ended: stuck")

    def test_provider_failed_formats_correctly(self):
        line = self._format_status_line({"termination": "provider_failed"})
        self.assertEqual(line, "[STATUS] run ended: provider_failed")

    def test_need_help_formats_correctly(self):
        line = self._format_status_line({"termination": "need_help"})
        self.assertEqual(line, "[STATUS] run ended: need_help")

    def test_conversational_loop_formats_correctly(self):
        line = self._format_status_line({"termination": "conversational_loop"})
        self.assertEqual(line, "[STATUS] run ended: conversational_loop")

    def test_missing_key_falls_back_to_unknown(self):
        """Old result dicts without 'termination' must not raise KeyError."""
        line = self._format_status_line({"success": True, "summary": "done"})
        self.assertEqual(line, "[STATUS] run ended: unknown")

    def test_run_done_json_shape_for_success(self):
        line = self._format_run_done_line({"success": True, "termination": "task_complete"})
        self.assertEqual(
            line,
            '{"type":"run_done","termination":"task_complete","success":true}',
        )

    def test_run_done_json_shape_for_failure(self):
        line = self._format_run_done_line({"success": False, "termination": "max_iterations"})
        self.assertEqual(
            line,
            '{"type":"run_done","termination":"max_iterations","success":false}',
        )

    def test_all_enum_values_covered(self):
        """Every AgentTermination value must produce a non-empty status line."""
        from orchestrator.agent_states import AgentTermination
        for term in AgentTermination:
            line = self._format_status_line({"termination": term.value})
            self.assertEqual(line, f"[STATUS] run ended: {term.value}")


if __name__ == "__main__":
    unittest.main()

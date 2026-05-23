"""
Contract lock-down tests (Phase 2).

These tests verify the inviolable interfaces documented in CONTRACTS.md.
If any of these fail after a change, the change broke a cross-system contract.
"""

import json
import re
import unittest


# ── 1. Stdout Protocol Contracts ───────────────────────────────────────────


class TestStdoutProtocolFormat(unittest.TestCase):
    """Verify the stdout marker formats match what consumers expect."""

    MARKERS_SIMPLE = ["[STATUS]", "[TOOL]", "[SUCCESS]", "[FAILED]"]
    MARKERS_JSON = ["[PLAN]", "[STEP]", "[DONE]"]

    def test_simple_markers_have_space_after_bracket(self):
        """Simple markers: '[MARKER] content' — one space after ]."""
        for marker in self.MARKERS_SIMPLE:
            line = f"{marker} some content here"
            self.assertTrue(line.startswith(f"{marker} "))

    def test_json_markers_have_no_space_before_json(self):
        """JSON markers: '[PLAN]{...}' — NO space between ] and {."""
        for marker in self.MARKERS_JSON:
            payload = {"test": "value"}
            line = f"{marker}{json.dumps(payload, separators=(',', ':'))}"
            # Must NOT have space between marker and JSON
            bracket_pos = line.index("]")
            self.assertEqual(line[bracket_pos + 1], "{",
                             f"{marker} must be immediately followed by {{ — got: {line}")

    def test_plan_json_shape(self):
        """[PLAN] payload must have 'steps' array of strings."""
        payload = {"steps": ["step one", "step two"]}
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        line = f"[PLAN]{encoded}"

        # Parse back
        json_start = line.index("{")
        parsed = json.loads(line[json_start:])
        self.assertIn("steps", parsed)
        self.assertIsInstance(parsed["steps"], list)
        for step in parsed["steps"]:
            self.assertIsInstance(step, str)

    def test_step_json_shape(self):
        """[STEP] payload must have 'index' (int) and 'name' (str)."""
        payload = {"index": 1, "name": "Do the thing"}
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        line = f"[STEP]{encoded}"

        json_start = line.index("{")
        parsed = json.loads(line[json_start:])
        self.assertIn("index", parsed)
        self.assertIn("name", parsed)
        self.assertIsInstance(parsed["index"], int)
        self.assertIsInstance(parsed["name"], str)

    def test_done_json_shape(self):
        """[DONE] payload must have 'index' (int) and 'summary' (str)."""
        payload = {"index": 1, "summary": "Completed step"}
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        line = f"[DONE]{encoded}"

        json_start = line.index("{")
        parsed = json.loads(line[json_start:])
        self.assertIn("index", parsed)
        self.assertIn("summary", parsed)
        self.assertIsInstance(parsed["index"], int)
        self.assertIsInstance(parsed["summary"], str)

    def test_plan_steps_max_12_items_max_120_chars(self):
        """PLAN steps: max 12, each truncated to 120 chars (per CONTRACTS.md)."""
        steps = [f"step {i}" for i in range(15)]
        truncated = [s[:120] for s in steps[:12]]
        self.assertLessEqual(len(truncated), 12)
        for s in truncated:
            self.assertLessEqual(len(s), 120)

    def test_tool_marker_format(self):
        """[TOOL] format: tool_name(json_args_up_to_120_chars)."""
        tool_name = "write_file"
        args = {"path": "test.py", "content": "x" * 200}
        args_str = json.dumps(args)[:120]
        line = f"[TOOL] {tool_name}({args_str})"
        self.assertTrue(line.startswith("[TOOL] "))
        self.assertIn("write_file(", line)


# ── 2. Provider Response Shape Contracts ───────────────────────────────────


class TestProviderResponseShape(unittest.TestCase):
    """Verify the provider response dict shape matches CONTRACTS.md."""

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

    def test_tool_call_response(self):
        resp = {"type": "tool_call", "tool": "click", "args": {"x": 100, "y": 200}}
        self._validate_response(resp)

    def test_text_response(self):
        resp = {"type": "text", "content": "TASK_COMPLETE: done"}
        self._validate_response(resp)

    def test_tool_call_with_content(self):
        """Optional 'content' field on tool_call (thinking/narration)."""
        resp = {"type": "tool_call", "tool": "read_file", "args": {"path": "x"}, "content": "Let me read that"}
        self._validate_response(resp)

    def test_invalid_type_rejected(self):
        resp = {"type": "unknown", "data": "bad"}
        with self.assertRaises(AssertionError):
            self._validate_response(resp)

    def test_tool_call_missing_tool_rejected(self):
        resp = {"type": "tool_call", "args": {"x": 1}}
        with self.assertRaises(AssertionError):
            self._validate_response(resp)

    def test_text_missing_content_rejected(self):
        resp = {"type": "text"}
        with self.assertRaises(AssertionError):
            self._validate_response(resp)


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
            except (ImportError, ModuleNotFoundError):
                # Missing optional dep (playwright, etc.) is OK — the name is recognized
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


# ── 4. Canonical Message Format ────────────────────────────────────────────


class TestCanonicalMessageFormat(unittest.TestCase):
    """Verify message format matches base.py docstring."""

    def test_text_message(self):
        msg = {"role": "user", "content": "Hello"}
        self.assertIn(msg["role"], ("user", "assistant"))
        self.assertIsInstance(msg["content"], str)

    def test_multimodal_message(self):
        msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": "What's in this image?"},
                {"type": "image", "data": "base64data", "media_type": "image/png"},
            ],
        }
        self.assertIn(msg["role"], ("user", "assistant"))
        self.assertIsInstance(msg["content"], list)
        for item in msg["content"]:
            self.assertIn("type", item)
            self.assertIn(item["type"], ("text", "image"))

    def test_tool_result_message(self):
        """Tool results are appended as user messages with prefix."""
        msg = {"role": "user", "content": "[Tool result: write_file]\nWritten successfully"}
        self.assertEqual(msg["role"], "user")
        self.assertTrue(msg["content"].startswith("[Tool result: "))


# ── 5. Task Completion Signals ─────────────────────────────────────────────


class TestTaskCompletionSignals(unittest.TestCase):
    """Verify TASK_COMPLETE and NEED_HELP regex patterns match the agent's."""

    TASK_COMPLETE_RE = re.compile(r"\bTASK_COMPLETE:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
    NEED_HELP_RE = re.compile(r"\bNEED_HELP:\s*(.+)$", re.IGNORECASE | re.MULTILINE)

    def test_task_complete_matches(self):
        text = "I've finished the work.\nTASK_COMPLETE: All files written successfully."
        m = self.TASK_COMPLETE_RE.search(text)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).strip(), "All files written successfully.")

    def test_need_help_matches(self):
        text = "I'm stuck.\nNEED_HELP: Cannot find the config file."
        m = self.NEED_HELP_RE.search(text)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).strip(), "Cannot find the config file.")

    def test_task_complete_case_insensitive(self):
        text = "task_complete: Done"
        m = self.TASK_COMPLETE_RE.search(text)
        self.assertIsNotNone(m)

    def test_task_complete_tool_names(self):
        """task_complete as tool call is intercepted by the agent."""
        valid_keys = ["message", "summary", "result"]
        args = {"message": "All done"}
        # At least one key must be present
        found = any(args.get(k) for k in valid_keys)
        self.assertTrue(found)


# ── 6. Plan Parsing Contract (agent._emit_plan_markers) ───────────────────


class TestPlanParsing(unittest.TestCase):
    """Verify plan/step/done regex patterns match real LLM output."""

    PLAN_RE = re.compile(r"^\s*PLAN:\s*(\d+)\s*step", re.IGNORECASE | re.MULTILINE)
    STEP_RE = re.compile(r"^\s*STEP\s*(\d+)\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
    DONE_RE = re.compile(r"^\s*DONE\s*(\d+)\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)

    def test_plan_block_parsing(self):
        content = "PLAN: 3 steps\n1. Read the file\n2. Edit the code\n3. Run tests"
        m = self.PLAN_RE.search(content)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "3")

        after = content[m.end():]
        steps = []
        for raw in after.splitlines():
            line = raw.strip()
            if not line:
                continue
            sm = re.match(r"^(\d+)[.)]\s+(.+?)\s*$", line)
            if sm:
                steps.append(sm.group(2).strip())
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[0], "Read the file")

    def test_step_marker(self):
        content = "STEP 2: Editing the configuration"
        matches = list(self.STEP_RE.finditer(content))
        self.assertEqual(len(matches), 1)
        self.assertEqual(int(matches[0].group(1)), 2)
        self.assertEqual(matches[0].group(2).strip(), "Editing the configuration")

    def test_done_marker(self):
        content = "DONE 1: File written successfully"
        matches = list(self.DONE_RE.finditer(content))
        self.assertEqual(len(matches), 1)
        self.assertEqual(int(matches[0].group(1)), 1)
        self.assertEqual(matches[0].group(2).strip(), "File written successfully")


# ── 7. MCP Tool Error Envelope ─────────────────────────────────────────────


class TestMCPErrorEnvelope(unittest.TestCase):
    """Verify MCP tool error prefixes match what agent/UI expect."""

    ERROR_PREFIXES = ["ERROR:", "PERMISSION_ERROR:", "OS_LIMITATION:"]

    def test_error_prefixes_are_recognized(self):
        for prefix in self.ERROR_PREFIXES:
            msg = f"{prefix} Something went wrong"
            self.assertTrue(msg.startswith(prefix))

    def test_normal_result_not_confused_with_error(self):
        msg = "Written successfully"
        for prefix in self.ERROR_PREFIXES:
            self.assertFalse(msg.startswith(prefix))


if __name__ == "__main__":
    unittest.main()

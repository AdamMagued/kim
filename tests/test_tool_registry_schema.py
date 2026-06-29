"""
Regression guards for mcp_server/tool_registry.py schema correctness.

Covers:
  1. shell_schema_additional_properties_false — run_command schema sets
     additionalProperties:false so model-injected keys are rejected.
  2. shell_schema_omits_operator_only_args — run_command/run_powershell
     input schemas do NOT declare sandbox_mode or allow_chaining.
  3. file_tools_present — registry exposes read_file/list_dir/write_file/
     delete_file with valid schemas after the tier split.
"""
from __future__ import annotations

import unittest

try:
    from mcp_server.tool_registry import TOOLS as _TOOLS
    _REGISTRY_AVAILABLE = True
except ImportError:
    _REGISTRY_AVAILABLE = False


def _get_tool(name: str):
    """Return the Tool object with the given name, or None."""
    for t in _TOOLS:
        if t.name == name:
            return t
    return None


@unittest.skipUnless(_REGISTRY_AVAILABLE, "mcp_server.tool_registry unavailable (mcp not installed)")
class TestShellSchemaAdditionalPropertiesFalse(unittest.TestCase):
    """run_command must carry additionalProperties:false to block injected keys."""

    def test_run_command_additional_properties_false(self):
        tool = _get_tool("run_command")
        self.assertIsNotNone(tool, "run_command tool must exist in the registry")
        schema = tool.inputSchema
        self.assertIn(
            "additionalProperties",
            schema,
            "run_command inputSchema must declare additionalProperties",
        )
        self.assertIs(
            schema["additionalProperties"],
            False,
            "run_command inputSchema must set additionalProperties:false "
            "(prevents model-injected sandbox_mode/allow_chaining keys)",
        )

    def test_run_powershell_additional_properties_false(self):
        tool = _get_tool("run_powershell")
        self.assertIsNotNone(tool, "run_powershell tool must exist in the registry")
        schema = tool.inputSchema
        self.assertIn(
            "additionalProperties",
            schema,
            "run_powershell inputSchema must declare additionalProperties",
        )
        self.assertIs(
            schema["additionalProperties"],
            False,
            "run_powershell inputSchema must set additionalProperties:false "
            "(prevents model-injected sandbox_mode/allow_chaining keys)",
        )


@unittest.skipUnless(_REGISTRY_AVAILABLE, "mcp_server.tool_registry unavailable (mcp not installed)")
class TestShellSchemaOmitsOperatorOnlyArgs(unittest.TestCase):
    """run_command and run_powershell must not declare sandbox_mode or allow_chaining."""

    FORBIDDEN_PROPERTIES = ("sandbox_mode", "allow_chaining")

    def _assert_properties_absent(self, tool_name: str) -> None:
        tool = _get_tool(tool_name)
        self.assertIsNotNone(tool, f"{tool_name} tool must exist in the registry")
        declared = set(tool.inputSchema.get("properties", {}).keys())
        for forbidden in self.FORBIDDEN_PROPERTIES:
            self.assertNotIn(
                forbidden,
                declared,
                f"{tool_name} inputSchema must not declare '{forbidden}' as an "
                "accepted property — it is an operator-only security control key",
            )

    def test_run_command_omits_sandbox_mode(self):
        self._assert_properties_absent("run_command")

    def test_run_powershell_omits_sandbox_mode(self):
        self._assert_properties_absent("run_powershell")


@unittest.skipUnless(_REGISTRY_AVAILABLE, "mcp_server.tool_registry unavailable (mcp not installed)")
class TestFileToolsPresent(unittest.TestCase):
    """read_file/list_dir/write_file/delete_file must survive the tier split."""

    FILE_TOOLS = ("read_file", "list_dir", "write_file", "delete_file")

    def _assert_tool_has_valid_schema(self, name: str) -> None:
        tool = _get_tool(name)
        self.assertIsNotNone(tool, f"'{name}' must be present in the TOOLS registry")
        schema = tool.inputSchema
        self.assertIsInstance(schema, dict, f"'{name}' inputSchema must be a dict")
        self.assertEqual(
            schema.get("type"),
            "object",
            f"'{name}' inputSchema type must be 'object'",
        )
        self.assertIn(
            "properties",
            schema,
            f"'{name}' inputSchema must have a 'properties' key",
        )

    def test_read_file_present_with_valid_schema(self):
        self._assert_tool_has_valid_schema("read_file")

    def test_list_dir_present_with_valid_schema(self):
        self._assert_tool_has_valid_schema("list_dir")

    def test_write_file_present_with_valid_schema(self):
        self._assert_tool_has_valid_schema("write_file")

    def test_delete_file_present_with_valid_schema(self):
        self._assert_tool_has_valid_schema("delete_file")

    def test_read_file_requires_path(self):
        tool = _get_tool("read_file")
        self.assertIsNotNone(tool)
        required = tool.inputSchema.get("required", [])
        self.assertIn("path", required, "read_file must require 'path'")

    def test_write_file_requires_path_and_content(self):
        tool = _get_tool("write_file")
        self.assertIsNotNone(tool)
        required = tool.inputSchema.get("required", [])
        self.assertIn("path", required, "write_file must require 'path'")
        self.assertIn("content", required, "write_file must require 'content'")

    def test_delete_file_requires_path(self):
        tool = _get_tool("delete_file")
        self.assertIsNotNone(tool)
        required = tool.inputSchema.get("required", [])
        self.assertIn("path", required, "delete_file must require 'path'")

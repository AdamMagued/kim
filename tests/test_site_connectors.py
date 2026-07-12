"""Site-connector registry hygiene (F-C-7).

The guc_cms / guc_mail connectors shipped as dead stubs: real MCP tools whose
handlers only ever returned a "not implemented yet" placeholder string. A user
who toggled such a connector on handed the agent a callable no-op — a wasted
turn and a confusing loop. They were removed; these tests keep the registry
honest so a placeholder connector can't quietly ship again.
"""

from __future__ import annotations

import asyncio
import inspect
import unittest
from unittest import mock

from mcp.types import Tool

from mcp_server.sites import (
    SiteConnector,
    enabled_connectors,
    get_connector,
    iter_connectors,
    load_builtin_connectors,
    register_site,
)
from mcp_server.sites import base as sites_base


class BuiltinConnectorHygieneTests(unittest.TestCase):
    """What `load_builtin_connectors()` actually registers."""

    def setUp(self):
        # The registry is process-global; isolate each test.
        self._saved = dict(sites_base._REGISTRY)
        sites_base._REGISTRY.clear()

    def tearDown(self):
        sites_base._REGISTRY.clear()
        sites_base._REGISTRY.update(self._saved)

    def test_dead_guc_stubs_are_gone(self):
        # F-C-7 regression: the stub connectors must not re-register.
        load_builtin_connectors()
        self.assertIsNone(get_connector("guc_cms"))
        self.assertIsNone(get_connector("guc_mail"))

    def test_no_builtin_connector_ships_placeholder_handlers(self):
        # Generic form of F-C-7: a no-arg tool whose handler returns
        # "not implemented" boilerplate is a dead stub — don't ship it.
        load_builtin_connectors()
        for connector in iter_connectors():
            for tool in connector.tools:
                handler = connector.handlers[tool.name]
                self.assertTrue(
                    inspect.iscoroutinefunction(handler),
                    f"{connector.id}.{tool.name}: handler must be async",
                )
                schema = tool.inputSchema or {}
                takes_no_args = not schema.get("required") and not schema.get(
                    "properties"
                )
                if takes_no_args:
                    result = asyncio.run(handler({}))
                    self.assertNotIn(
                        "not implemented",
                        result.lower(),
                        f"{connector.id}.{tool.name}: callable no-op stub "
                        "(F-C-7) — implement it or keep it off a release "
                        "branch",
                    )


class ConnectorFrameworkTests(unittest.TestCase):
    """The framework itself still works with the stubs gone."""

    def setUp(self):
        self._saved = dict(sites_base._REGISTRY)
        sites_base._REGISTRY.clear()

    def tearDown(self):
        sites_base._REGISTRY.clear()
        sites_base._REGISTRY.update(self._saved)

    @staticmethod
    def _tool(name: str) -> Tool:
        return Tool(
            name=name,
            description="test tool",
            inputSchema={"type": "object", "properties": {}, "required": []},
        )

    def test_register_and_enable_roundtrip(self):
        async def handler(arguments: dict) -> str:
            return "ok"

        connector = register_site(
            SiteConnector(
                id="my_site",
                label="My Site",
                description="test connector",
                tools=[self._tool("my_site_do_thing")],
                handlers={"my_site_do_thing": handler},
            )
        )
        self.assertIs(get_connector("my_site"), connector)
        self.assertEqual(enabled_connectors(["my_site"]), [connector])
        # Unknown ids are dropped with a warning, never a crash.
        self.assertEqual(enabled_connectors(["nope"]), [])

    def test_tool_without_handler_fails_loudly_at_import(self):
        with self.assertRaises(ValueError):
            SiteConnector(
                id="broken",
                label="Broken",
                description="tool with no handler",
                tools=[self._tool("broken_orphan")],
                handlers={},
            )

    def test_enabled_connectors_warns_on_unknown_id(self):
        with mock.patch.object(sites_base.logger, "warning") as warn:
            out = enabled_connectors(["ghost"])
        self.assertEqual(out, [])
        warn.assert_called_once()


if __name__ == "__main__":
    unittest.main()

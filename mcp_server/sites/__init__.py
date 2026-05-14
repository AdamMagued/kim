"""Site-specific connector framework.

A "connector" is a bundle of MCP tools scoped to one external system
(GUC CMS, a webmail, a CRM, etc.). When the user enables a connector in
settings, its tools get added to every agent call alongside the base tool
set. When disabled, they're invisible to the LLM and the agent has no idea
the connector exists.

Adding a connector takes three steps:

    1. Create a file under `mcp_server/sites/`, e.g. `guc_cms.py`.
    2. Build a `SiteConnector` instance, registering MCP `Tool`s and the
       handlers that execute them.
    3. Call `register_site(connector)` at module import time.

The connector is then discoverable via `iter_connectors()` and gets
auto-merged into the server's tool list / dispatch map when the user has
enabled it in `config.yaml` under `connectors.enabled: [...]`.
"""

from mcp_server.sites.base import (
    SiteConnector,
    SiteToolHandler,
    register_site,
    get_connector,
    iter_connectors,
    enabled_connectors,
)

__all__ = [
    "SiteConnector",
    "SiteToolHandler",
    "register_site",
    "get_connector",
    "iter_connectors",
    "enabled_connectors",
]


def load_builtin_connectors() -> None:
    """Import every connector module in this package so each one's
    `register_site(...)` call runs.

    Called once from `mcp_server.server` on startup. Adding a new connector
    is just dropping a new file in this directory — no manual registration
    needed at the call site.
    """
    import importlib
    import pkgutil

    import mcp_server.sites as pkg

    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name.startswith("_") or mod.name in {"base", "registry"}:
            continue
        # Importing the module triggers its top-level `register_site(...)`.
        importlib.import_module(f"mcp_server.sites.{mod.name}")

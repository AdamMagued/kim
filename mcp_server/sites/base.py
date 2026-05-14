"""SiteConnector base + registry primitives.

The registry is a process-global dict keyed by connector id. Each entry
carries its MCP tools, dispatch handlers, optional system-prompt appendix,
and optional URL patterns for future auto-activate logic (URL-aware
connectors aren't required for v1 — the simple "user toggles it on, tools
go out" model works for everything we want today).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable, Iterator

from mcp.types import Tool

logger = logging.getLogger("kim.sites")


# A tool handler receives the raw arguments dict and returns a string result
# (matching the existing pattern in `mcp_server/server.py`'s `_DISPATCH`).
SiteToolHandler = Callable[[dict], Awaitable[str]]


@dataclass
class SiteConnector:
    """One connector — a coherent toolkit for a single external system."""

    # Stable id used in config + the dispatch map (e.g. "guc_cms").
    id: str

    # Human-readable label shown in the Settings UI ("GUC CMS").
    label: str

    # One-sentence description ("Fetch grades, assignments, and downloads
    # from the GUC student portal.").
    description: str

    # MCP `Tool` definitions that get broadcast to the LLM. Names should be
    # prefixed with the connector id (e.g. "guc_cms_list_grades") so they
    # don't collide with base tools or other connectors.
    tools: list[Tool] = field(default_factory=list)

    # Maps tool name -> async handler that runs when the LLM calls it.
    # Keys MUST match `tools[*].name` exactly.
    handlers: dict[str, SiteToolHandler] = field(default_factory=dict)

    # Optional text appended to the system prompt when this connector is
    # active. Useful for telling the LLM how / when to use these tools.
    system_prompt_appendix: str = ""

    # Optional URL patterns that mark a page as belonging to this site.
    # Reserved for future URL-aware filtering — unused in v1 (we use the
    # explicit toggle from config instead).
    url_patterns: list[re.Pattern] = field(default_factory=list)

    # If True, this connector is on by default in `config.yaml`. Most
    # connectors should default to False so users opt in explicitly.
    default_enabled: bool = False

    def __post_init__(self) -> None:
        # Cheap sanity check — a tool listed in `tools` without a matching
        # handler will 500 the agent loop at call time, so fail loudly here.
        tool_names = {t.name for t in self.tools}
        handler_names = set(self.handlers.keys())
        missing_handler = tool_names - handler_names
        extra_handler = handler_names - tool_names
        if missing_handler:
            raise ValueError(
                f"SiteConnector {self.id!r}: tools without handlers: "
                f"{sorted(missing_handler)}"
            )
        if extra_handler:
            logger.warning(
                "SiteConnector %s has handlers without matching tool "
                "definitions: %s",
                self.id,
                sorted(extra_handler),
            )


# Process-global registry — populated by `register_site()` at import time.
_REGISTRY: dict[str, SiteConnector] = {}


def register_site(connector: SiteConnector) -> SiteConnector:
    """Register a connector. Called at module import. Returns the connector
    so callers can chain (`MY_CONNECTOR = register_site(SiteConnector(...))`).
    """
    if connector.id in _REGISTRY:
        # Re-registration is almost always a programming error; warn but
        # don't crash — module-reload during dev can legitimately trigger it.
        logger.warning("Site connector %r re-registered (overwriting).", connector.id)
    _REGISTRY[connector.id] = connector
    logger.info(
        "Registered site connector %r (%d tools, default_enabled=%s)",
        connector.id,
        len(connector.tools),
        connector.default_enabled,
    )
    return connector


def get_connector(connector_id: str) -> SiteConnector | None:
    return _REGISTRY.get(connector_id)


def iter_connectors() -> Iterator[SiteConnector]:
    """Iterate over every registered connector, enabled or not."""
    return iter(_REGISTRY.values())


def enabled_connectors(enabled_ids: Iterable[str]) -> list[SiteConnector]:
    """Filter the registry to the connectors whose ids appear in
    `enabled_ids`. Unknown ids are silently dropped (with a warning), so a
    stale config doesn't crash the server.
    """
    enabled_set = set(enabled_ids)
    out: list[SiteConnector] = []
    for cid in enabled_set:
        c = _REGISTRY.get(cid)
        if c is None:
            logger.warning(
                "Config enables connector %r but no such connector is registered.",
                cid,
            )
            continue
        out.append(c)
    return out

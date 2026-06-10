"""
Capability tier filtering for MCP tools.

Set KIM_ENABLED_TOOL_TIERS to a comma-separated list of tier names to expose
only those tools to the agent.  Unset (or empty) means all tools are exposed
-- default behavior is unchanged.

Granular tiers (one per existing tool group):
    file, shell, screen, web, mouse, keyboard, windows, git, code, search, memory

Compound aliases (expand to the granular tiers listed):
    core    -> file, shell, search
    ui      -> screen, mouse, keyboard, windows
    browser -> web

Example:
    export KIM_ENABLED_TOOL_TIERS=core,git
    # Exposes: file + shell + search + git tools only
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Compound aliases expand to multiple granular tier names.
# These match the vocabulary used in HARNESS_ROADMAP.md.
TIER_ALIASES: dict[str, tuple[str, ...]] = {
    "core":    ("file", "shell", "search"),
    "ui":      ("screen", "mouse", "keyboard", "windows"),
    "browser": ("web",),
}


def parse_enabled_tiers(env_value: str | None) -> "frozenset[str] | None":
    """Parse a KIM_ENABLED_TOOL_TIERS value into a frozenset of tier names.

    Returns None when the value is absent or blank (all tiers active).
    Normalizes to lowercase and strips whitespace around each entry.
    Does not validate names -- filter_tools handles unknown names.
    """
    if not env_value or not env_value.strip():
        return None
    return frozenset(part.strip().lower() for part in env_value.split(",") if part.strip())


def filter_tools(
    tier_dispatch: "dict[str, dict]",
    enabled_tiers: "frozenset[str] | None",
) -> "frozenset[str] | None":
    """Return the set of tool names to expose given a set of enabled tiers.

    tier_dispatch: mapping of tier_name -> {tool_name: handler, ...}
    enabled_tiers: frozenset of tier/alias names, or None for all tools

    Returns None when all tools should be exposed (callers skip filtering).
    Unknown tier names log a WARNING and contribute no tools so that a typo
    produces a visible, predictable empty contribution rather than silently
    granting access to all tools.
    """
    if enabled_tiers is None:
        return None

    known_tiers = set(tier_dispatch) | set(TIER_ALIASES)
    result: set[str] = set()
    for tier in sorted(enabled_tiers):
        if tier in tier_dispatch:
            result.update(tier_dispatch[tier].keys())
        elif tier in TIER_ALIASES:
            for component in TIER_ALIASES[tier]:
                if component in tier_dispatch:
                    result.update(tier_dispatch[component].keys())
                else:
                    logger.warning(
                        "KIM_ENABLED_TOOL_TIERS: alias %r references unknown tier %r (ignored)",
                        tier, component,
                    )
        else:
            logger.warning(
                "KIM_ENABLED_TOOL_TIERS: unknown tier %r ignored (known: %s)",
                tier,
                ", ".join(sorted(known_tiers)),
            )
    return frozenset(result)


def get_active_tool_names(tier_dispatch: "dict[str, dict]") -> "frozenset[str] | None":
    """Read KIM_ENABLED_TOOL_TIERS and return the set of enabled tool names.

    Returns None when the env var is unset or empty (all tools active).
    Pass TIER_DISPATCH from tool_registry as tier_dispatch.
    """
    raw = os.environ.get("KIM_ENABLED_TOOL_TIERS", "")
    tiers = parse_enabled_tiers(raw)
    return filter_tools(tier_dispatch, tiers)

"""GUC CMS connector — stub.

Tools and handlers will be filled in once we know the exact selectors and
endpoints. For now this file exists as the canonical example of how to add
a connector, and it auto-registers so the Settings UI can show the toggle
end-to-end before the real tools land.

Once you give me the tool list, drop them into `_TOOLS` below and implement
each handler. They have access to `mcp_server.tools.web`'s active Playwright
page singleton, so login state from `web_open` carries across.
"""

from __future__ import annotations

from mcp.types import Tool

from mcp_server.sites.base import SiteConnector, register_site


# ──────────────────────────────────────────────────────────────────────────
# Tool stubs
# Each handler receives the raw `arguments` dict and returns a string.
# They share the Playwright page that `mcp_server/tools/web.py` opens, so
# login flows can use generic `web_open` / `web_fill` first, then GUC-
# specific tools take over once logged in.
# ──────────────────────────────────────────────────────────────────────────

async def _guc_cms_placeholder(arguments: dict) -> str:
    """Placeholder — real handlers replace this once the tool list is fixed."""
    return (
        "GUC CMS connector is registered but no real handler is implemented "
        "yet. Wire the actual scrape/fetch logic here."
    )


_TOOLS: list[Tool] = [
    # Example shape — leave one stub so the Settings UI has something to
    # show in its tooltip. Real tools should look like:
    #
    # Tool(
    #     name="guc_cms_list_courses",
    #     description="List the user's currently enrolled GUC courses with "
    #                 "course_id, name, semester, and assignment counts.",
    #     inputSchema={"type": "object", "properties": {}, "required": []},
    # ),
    Tool(
        name="guc_cms_ping",
        description=(
            "Stub tool — confirms the GUC CMS connector is loaded. "
            "Will be replaced with real GUC tools (list courses, grades, "
            "files, announcements, downloads) once the selector map is "
            "decided."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
]


GUC_CMS = register_site(SiteConnector(
    id="guc_cms",
    label="GUC CMS",
    description=(
        "Scrape the GUC student portal — list courses, grades, assignments, "
        "and download course files. Tools become available to the agent "
        "when this connector is on."
    ),
    tools=_TOOLS,
    handlers={
        "guc_cms_ping": _guc_cms_placeholder,
    },
    system_prompt_appendix=(
        "GUC CMS tools are available (prefixed `guc_cms_`). Use them when "
        "the user asks about their GUC courses, grades, assignments, "
        "announcements, or course files. Sign-in is handled by web_open + "
        "the GUC SSO page; the GUC tools then operate on the already-"
        "authenticated session."
    ),
    default_enabled=False,
))

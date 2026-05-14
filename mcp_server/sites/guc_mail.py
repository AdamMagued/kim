"""GUC student-mail connector — stub.

Same shape as `guc_cms.py`. Will be filled in once the tool list is fixed.
GUC mail is hosted on Microsoft 365 / OWA, so handlers will likely drive
either OWA-DOM via Playwright or the Microsoft Graph API depending on the
authentication path you want.
"""

from __future__ import annotations

from mcp.types import Tool

from mcp_server.sites.base import SiteConnector, register_site


async def _guc_mail_placeholder(arguments: dict) -> str:
    return (
        "GUC mail connector is registered but no real handler is implemented "
        "yet. Wire the actual fetch/send logic here."
    )


_TOOLS: list[Tool] = [
    Tool(
        name="guc_mail_ping",
        description=(
            "Stub — confirms the GUC mail connector is loaded. Real tools "
            "(list inbox, read message, send mail, reply) will replace this."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
]


GUC_MAIL = register_site(SiteConnector(
    id="guc_mail",
    label="GUC Mail",
    description=(
        "Read and send GUC student mail (Outlook Web Access). Tools "
        "become available to the agent when this connector is on."
    ),
    tools=_TOOLS,
    handlers={
        "guc_mail_ping": _guc_mail_placeholder,
    },
    system_prompt_appendix=(
        "GUC mail tools are available (prefixed `guc_mail_`). Use them "
        "when the user asks Kim to check, read, search, send, or reply "
        "to GUC student mail. Sign-in is handled by the standard Microsoft "
        "365 / OWA flow via web_open before the GUC mail tools take over."
    ),
    default_enabled=False,
))

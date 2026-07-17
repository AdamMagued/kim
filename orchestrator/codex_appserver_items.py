"""Pure interpretation of app-server ``item`` payloads (parity Part 2).

Split out of ``codex_appserver_transport.py`` (file-size gate, Q6 in
``docs/ROADMAP_TO_10.md``) — ``summarize_item`` has no instance-state side
effects, so it is independently testable and keeps
``AppServerTurnRunner._on_item`` a thin glue method.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class ItemSummary:
    """Everything ``AppServerTurnRunner._on_item`` needs from one
    ``item/started`` | ``item/completed`` notification's ``item`` payload.

    ``status`` is the item's own outcome (e.g. commandExecution's
    "completed" | "failed" | "declined") — distinct from the notification's
    ``phase``, which only echoes the JSON-RPC method name (item/started vs
    item/completed) and fires regardless of whether the command actually
    succeeded; a sandbox-denied command still reaches item/completed.
    """

    kind: str
    item_id: str
    title: str
    status: str
    agent_message_text: str = ""
    changes_json: str | None = None


def summarize_item(item: dict) -> ItemSummary:
    """Interpret one app-server ``item`` dict. Pure — no side effects."""
    kind = str(item.get("type") or "")
    item_id = str(item.get("id") or "")
    status = str(item.get("status") or "")
    title = ""
    agent_message_text = ""
    changes_json: str | None = None

    if kind == "commandExecution":
        title = str(item.get("command") or "")
    elif kind == "fileChange":
        changes = item.get("changes")
        if isinstance(changes, list):
            paths = [str(c.get("path") or "") for c in changes if isinstance(c, dict)]
            title = ", ".join(p for p in paths if p)
            with contextlib.suppress(Exception):
                changes_json = json.dumps([
                    {"path": c.get("path"), "kind": c.get("kind")}
                    for c in changes if isinstance(c, dict)
                ])
    elif kind == "agentMessage":
        text = str(item.get("text") or "")
        if text.strip():
            agent_message_text = text

    return ItemSummary(
        kind=kind, item_id=item_id, title=title, status=status,
        agent_message_text=agent_message_text, changes_json=changes_json,
    )

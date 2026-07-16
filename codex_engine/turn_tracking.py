"""Turn-boundary and conversation-reset detection for the codex proxy.

The exec transport spawns one ``codex exec`` subprocess per turn, so
``MAX_RELAYS`` (a runaway guard on ONE turn) is naturally reset by process
death between turns. The app-server transport calls ``_CodexProxy.begin_turn``
explicitly between turns for the same reason.

The standalone TUI proxy (``codex_engine/standalone_proxy.py``, spawned once
by the Rust launcher and pointed at by a long-lived ``kimcli`` process) has
neither: one process serves every turn of a whole interactive session, and
nothing calls ``begin_turn`` for it. These two pure helpers give
``_CodexProxy._handle_responses`` the turn/conversation boundaries it needs
from the shape of the incoming Responses API ``input`` list alone:

  * ``contains_new_user_turn`` — a genuine new user message (not a Codex
    "Continue." keepalive, not a tool result) beyond the last-forwarded
    cursor means a new turn started — the per-turn relay budget should reset.
  * ``detect_conversation_reset`` — codex's TUI ``/new`` command restarts the
    item list from a (shorter) beginning instead of growing it — the proxy's
    whole cached-conversation state (sent cursor, cached reply, tool-loop
    guard) is stale and must be dropped.
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional


def is_continue_keepalive_text(text: str) -> bool:
    """True for Codex's 'Continue.' keepalive prefix (see engine._is_continue_only_delta)."""
    return text.strip().startswith("Continue.")


def _item_text(item: dict) -> str:
    content = item.get("content", "")
    if isinstance(content, str):
        return content
    text = ""
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("input_text", "text"):
                text += str(block.get("text", ""))
    return text


def contains_new_user_turn(delta_items: list) -> bool:
    """True if `delta_items` (items beyond the last-forwarded cursor) contains
    a genuine new user message — signals that a new turn has started.

    Tool results (``function_call_output``) and Codex's "Continue." keepalive
    messages are part of the SAME ongoing turn, not a new one.
    """
    for item in delta_items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call_output":
            continue
        if item.get("role") != "user":
            continue
        text = _item_text(item).strip()
        if text and not is_continue_keepalive_text(text):
            return True
    return False


def _first_item_fingerprint(items: list) -> Optional[str]:
    """Stable fingerprint of items[0], used to detect a discontinuous restart."""
    if not items:
        return None
    try:
        raw = json.dumps(items[0], sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = str(items[0])
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def detect_conversation_reset(
    items: list,
    last_sent_count: int,
    last_first_fingerprint: Optional[str],
) -> tuple[bool, Optional[str]]:
    """Detect a fresh conversation (codex TUI's ``/new``) from the shape of
    the incoming Responses API `input` list.

    Codex resends the full running item list on every ``/v1/responses`` call;
    a continuing conversation only ever grows that list and keeps its first
    item unchanged. ``/new`` (or a fresh thread) restarts the list from a
    (shorter) beginning — either it is now shorter than what was last
    forwarded, or its first item no longer matches what was fingerprinted
    last time. Either signal alone means "treat as a fresh conversation".

    Returns ``(is_reset, new_first_fingerprint)`` — the caller should store
    ``new_first_fingerprint`` for the next call regardless of ``is_reset``.
    """
    fingerprint = _first_item_fingerprint(items)
    is_reset = False
    if last_sent_count > 0:
        if len(items) < last_sent_count:
            is_reset = True
        elif last_first_fingerprint is not None and fingerprint != last_first_fingerprint:
            is_reset = True
    return is_reset, fingerprint

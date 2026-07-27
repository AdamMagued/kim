"""Codex Responses API <-> BaseProvider canonical translation.

For ``_CodexProxy``'s ``responses-passthrough`` mode: codex 0.144.3 removed
the chat-completions wire API entirely (``WireApi`` in
``codex-rs/model-provider-info/src/lib.rs`` has only the ``Responses``
variant), so ``chat-passthrough`` (codex_engine/chat_passthrough.py,
/v1/chat/completions) is unreachable from a real kimcli launch. API
providers (claude, gemini, deepseek, ollama-behind-proxy) must instead be
served natively on /v1/responses — this module is that translation.

Unlike chat_passthrough.py's OpenAI-chat translation (which flattens
everything to one prompt string when relaying through a browser), this
module KEEPS STRUCTURE: each Responses input item (message content arrays,
``function_call``, ``function_call_output``) maps to its own canonical
turn, because codex's own multi-turn context management depends on that
structure surviving the round trip intact — this mode never flattens, never
runs a delta cursor, and never compacts (codex resends the full ``input``
list every relay and manages its own context; see ``_CodexProxy``'s module
docstring "Modes").

Canonical message / tool-call convention: identical to chat_passthrough.py
(see its module docstring) — assistant tool calls are
``{"role": "assistant", "content": '{"type": "tool_call", "tool": ..., "args": {...}}'}``
and tool results are ``{"role": "user", "content": "[Tool result: <name>]\\n<text>"}``.
The same one-tool-call-per-canonical-turn limitation applies on the way in
(chat_passthrough.py's ``_canonical_calls`` — reused here unchanged — still
expands a provider's ``"batch"`` reply into genuine parallel
``function_call`` output items on the way out).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Any, Optional

from codex_engine.turn_tracking import contains_new_user_turn

if TYPE_CHECKING:
    from codex_engine.engine import _CodexProxy

logger = logging.getLogger("kim.codex_bridge")

from codex_engine.chat_passthrough import _canonical_calls

# ── Request: Responses API -> canonical ──────────────────────────────────────


def _parse_data_uri(url: str) -> tuple[str, Optional[str]]:
    """``data:<media_type>;base64,<data>`` -> (media_type, base64 data)."""
    if not url.startswith("data:") or ";base64," not in url:
        return "image/png", None
    header, _, data = url.partition(",")
    media_type = header[len("data:"):].split(";", 1)[0].strip() or "image/png"
    return media_type, data.strip() or None


def _content_parts_to_canonical(parts: list) -> list:
    """Responses ``input_text``/``output_text``/``input_image`` parts -> canonical ContentItem list."""
    out: list[dict] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in ("input_text", "output_text", "text"):
            out.append({"type": "text", "text": str(part.get("text", ""))})
        elif ptype == "input_image":
            url = str(part.get("image_url") or "")
            media_type, data = _parse_data_uri(url)
            if data:
                out.append({"type": "image", "data": data, "media_type": media_type})
    return out


def _item_content_to_canonical(content: Any) -> Any:
    if isinstance(content, list):
        return _content_parts_to_canonical(content)
    return str(content or "")


def _function_call_output_text(output: Any) -> str:
    """``function_call_output.output`` is usually a plain string, but tolerate
    the block-list / dict shapes real tool runtimes sometimes send (mirrors
    engine.py's ``_extract_delta_prompt`` tolerance for the same field)."""
    if isinstance(output, list):
        texts = [str(b.get("text", "")) for b in output if isinstance(b, dict)]
        return "\n".join(texts) if any(texts) else "\n".join(str(o) for o in output)
    if isinstance(output, dict):
        return str(output.get("text") or output)
    return str(output or "")


def _tools_to_canonical(tools_raw: object) -> list[dict]:
    """Responses ``tools`` (flat ``{type:"function", name, description,
    parameters, strict}``) -> canonical ``[{"name","description","parameters"}]``.
    Also tolerates a ``function``-nested wrapper defensively, matching
    engine.py's ``_render_codex_tools`` dual-shape handling."""
    tools: list[dict] = []
    if not isinstance(tools_raw, list):
        return tools
    for tool in tools_raw:
        if not isinstance(tool, dict):
            continue
        fn_raw = tool.get("function")
        fn = fn_raw if isinstance(fn_raw, dict) else {}
        name = tool.get("name") or fn.get("name")
        if not name:
            continue
        tools.append({
            "name": name,
            "description": tool.get("description") or fn.get("description") or "",
            "parameters": tool.get("parameters") or fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return tools


def responses_request_to_canonical(body: dict) -> tuple[list, list, Optional[str]]:
    """Codex /v1/responses request body -> (messages, tools, system_prompt).

    ``messages`` is in BaseProvider canonical format, structure preserved:
    a ``function_call`` item becomes its own canonical assistant tool-call
    turn; a ``function_call_output`` is paired to the ``function_call`` with
    the same ``call_id`` and becomes a canonical tool-result turn. An orphan
    ``function_call_output`` (no ``function_call`` with that call_id seen
    yet in this same request — e.g. a resumed/trimmed history) falls back to
    "unknown", matching chat_passthrough.py's convention for an orphan
    ``role: "tool"`` message.
    """
    system_prompt = body.get("instructions") or None

    input_items = body.get("input")
    if isinstance(input_items, str):
        input_items = [{"role": "user", "content": input_items}]
    elif not isinstance(input_items, list):
        input_items = []

    messages: list[dict] = []
    call_names: dict[str, str] = {}  # call_id -> tool name, for output pairing

    for item in input_items:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        itype = item.get("type")

        if itype == "function_call":
            name = str(item.get("name") or "")
            call_id = str(item.get("call_id") or "")
            try:
                args = json.loads(item.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            if not isinstance(args, dict):
                args = {}
            if call_id:
                call_names[call_id] = name
            payload = {"type": "tool_call", "tool": name, "args": args, "content": ""}
            messages.append({"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)})
            continue

        if itype == "function_call_output":
            call_id = str(item.get("call_id") or "")
            name = call_names.pop(call_id, None) or "unknown"
            output_text = _function_call_output_text(item.get("output", ""))
            messages.append({"role": "user", "content": f"[Tool result: {name}]\n{output_text}"})
            continue

        # A plain message item — {"role", "content"} (content: str or parts list).
        role = str(item.get("role") or "user")
        messages.append({"role": role, "content": _item_content_to_canonical(item.get("content"))})

    tools = _tools_to_canonical(body.get("tools"))
    return messages, tools, system_prompt


# ── Response: canonical -> Responses API output items ───────────────────────


def canonical_reply_to_responses_parts(resp: object) -> tuple[str, Optional[list]]:
    """BaseProvider ProviderResponse -> (text, tool_calls) for engine.py's
    ``_make_responses_text_reply`` / ``_make_responses_tool_reply``.

    ``tool_calls`` (non-None only for a tool_call reply) is
    ``[{"name": str, "input": dict}, ...]`` — the exact shape those two
    emitters already expect. A ``"batch"`` tool_call (several providers'
    parallel tool use) expands into several entries via
    chat_passthrough.py's ``_canonical_calls``, unchanged.
    """
    if not isinstance(resp, dict):
        return str(resp), None
    if resp.get("type") == "tool_call":
        calls = _canonical_calls(resp)
        tool_calls = [{"name": c.get("tool"), "input": c.get("args") or {}} for c in calls]
        return str(resp.get("content") or ""), tool_calls
    return str(resp.get("content", "")), None


# ── _CodexProxy._handle_responses dispatch target ────────────────────────────
#
# Lives here (not as a _CodexProxy method) so engine.py — already over the
# Q6 800-line file-size gate and not allowed to grow past its previous push —
# only carries a 2-line dispatch (`if self._mode == "responses-passthrough":
# return await handle_responses_passthrough(self, body, _turn_items)`).


async def handle_responses_passthrough(proxy: "_CodexProxy", body: dict, input_items: list):
    """responses-passthrough mode: native BaseProvider tool-calling on
    /v1/responses (codex 0.144.3 removed wire_api="chat" entirely — see
    engine.py's module docstring "Modes"). Keeps item STRUCTURE (this
    module's ``responses_request_to_canonical``) — no prompt flattening, no
    delta cursor, no compaction, no browser-JSON-contract nudge/salvage;
    codex resends the full input every relay and manages its own context.

    Relay-cap turn-reset (TUI fix 1) still applies: ``proxy._last_sent_count``
    is reused here purely as a turn-boundary bookkeeping cursor (NOT a delta
    cursor — the full canonical translation is sent every relay regardless
    of its value).

    Reuses engine.py's existing Responses API emitters
    (``_make_responses_text_reply`` / ``_make_responses_tool_reply`` /
    ``_sse_or_json``) unchanged, via a deferred import — engine.py imports
    this function at module scope, so importing engine.py back at THIS
    module's top level would be circular; deferring it into the function
    body is safe because both modules are fully loaded by the time this is
    actually called.
    """
    from aiohttp import web

    from codex_engine.engine import (
        _make_responses_text_reply,
        _make_responses_tool_reply,
        _sse_or_json,
    )

    if contains_new_user_turn(input_items[proxy._last_sent_count:]):
        proxy._relay_count = 0
        try:
            from codex_engine.router import classify_task_mode
            prompt_str = str(body.get("input", ""))
            task_mode = classify_task_mode(prompt_str)
            logger.info("Hybrid Router: Task classified as %s mode", task_mode)
        except Exception:
            pass
    proxy._last_sent_count = len(input_items)

    proxy._relay_count += 1
    relay_num = proxy._relay_count
    if relay_num > proxy._max_relays:
        logger.error(f"Relay count exceeded {proxy._max_relays}")
        return web.json_response(
            {"error": {"message": "Too many relay attempts"}}, status=429,
        )

    stream = bool(body.get("stream", False))
    messages, tools, system_prompt = responses_request_to_canonical(body)

    try:
        response = await proxy._provider.complete(
            messages=messages, tools=tools, system=system_prompt or "",
        )
    except Exception as e:
        logger.error(f"[relay #{relay_num}] Provider call failed: {e}", exc_info=True)
        return web.json_response({"error": {"message": f"LLM call failed: {e}"}}, status=502)

    resp_id = f"resp_{uuid.uuid4().hex[:16]}"
    text, tool_calls = canonical_reply_to_responses_parts(response)
    responses_reply = (
        _make_responses_tool_reply(resp_id, text, tool_calls)
        if tool_calls else _make_responses_text_reply(resp_id, text)
    )
    return _sse_or_json(stream, responses_reply)

"""OpenAI /v1/chat/completions <-> BaseProvider canonical translation.

Pure translation functions (no I/O) for ``_CodexProxy``'s ``chat-passthrough``
mode: codex is pointed at this proxy with ``wire_api = "chat"`` and its own
context management, so the proxy's job is a faithful wire-format conversion
around one ``BaseProvider.complete()`` call — no browser-JSON-contract
parsing, no compaction (codex manages its own context in this mode).

Canonical message format (see ``orchestrator/providers/base.py`` and
``orchestrator/memory.py``), reproduced here as a comment rather than an
import — this module has no orchestrator dependency by design (codex_engine
does not import orchestrator's provider stack at module scope):

    {"role": "user" | "assistant", "content": str | list[ContentItem]}
    ContentItem: {"type": "text", "text": "..."}
                 {"type": "image", "data": "<base64>", "media_type": "image/png"}

Tool calls/results are NOT a distinct canonical role — they are encoded as
plain user/assistant turns by convention (matches
``orchestrator/providers/ollama.py``'s ``_assistant_tool_call_message`` /
``_tool_result_message`` and ``orchestrator/agent.py``'s memory writes):

    assistant tool call:  {"role": "assistant",
                            "content": '{"type": "tool_call", "tool": "...",
                                          "args": {...}, "content": "narration"}'}
    tool result:           {"role": "user",
                            "content": "[Tool result: <tool_name>]\\n<result text>"}

KNOWN LIMITATION — one tool call per canonical turn: ``BaseProvider`` only
returns ONE tool call per non-batch ``complete()`` reply (the ``"batch"``
tool wraps several as a single response for providers that support parallel
tool use, e.g. Claude/OpenAI). ``chat_request_to_canonical`` mirrors that on
the way IN: an incoming assistant message with several OpenAI ``tool_calls``
cannot be represented as one canonical assistant turn, so it degrades to
SEVERAL sequential (assistant tool-call, tool-result) canonical turn pairs,
one per call, in the original order. ``canonical_to_chat_response`` /
``stream_chat_response`` still emit genuine parallel ``tool_calls`` on the
way OUT when the provider itself returns ``tool == "batch"``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Iterator, Optional

# Mirrors orchestrator/providers/base.py's stop/finish-reason vocabularies.
# Duplicated (not imported) so this module stays free of orchestrator
# dependencies — see module docstring.
_TRUNCATION_STOP_REASONS = frozenset({"max_tokens", "length", "model_length", "max_output_tokens"})
_BLOCK_STOP_REASONS = frozenset({
    "content_filter", "safety", "recitation", "refusal",
    "prohibited_content", "blocklist", "spii", "other",
})


# ── Request: OpenAI chat completions -> canonical ────────────────────────────


def _content_parts_to_canonical(parts: list) -> list:
    """OpenAI message ``content`` parts -> canonical ContentItem list."""
    out: list[dict] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            out.append({"type": "text", "text": str(part.get("text", ""))})
        elif ptype == "image_url":
            image_url = part.get("image_url")
            url = image_url.get("url", "") if isinstance(image_url, dict) else str(image_url or "")
            media_type, data = _parse_data_uri(url)
            if data:
                out.append({"type": "image", "data": data, "media_type": media_type})
    return out


def _parse_data_uri(url: str) -> tuple[str, Optional[str]]:
    """``data:<media_type>;base64,<data>`` -> (media_type, base64 data). Non
    data-URIs (a real http(s) image URL) have no inline data to forward —
    returns (media_type, None) so the caller skips them."""
    if not url.startswith("data:") or ";base64," not in url:
        return "image/png", None
    header, _, data = url.partition(",")
    media_type = header[len("data:"):].split(";", 1)[0].strip() or "image/png"
    return media_type, data.strip() or None


def _message_content_to_canonical(content: Any) -> Any:
    if isinstance(content, list):
        return _content_parts_to_canonical(content)
    return str(content or "")


def _tool_call_to_canonical(fn_name: str, raw_arguments: Any, narration: str) -> dict:
    try:
        args = json.loads(raw_arguments) if isinstance(raw_arguments, str) else (raw_arguments or {})
    except (json.JSONDecodeError, TypeError):
        args = {}
    if not isinstance(args, dict):
        args = {}
    payload = {"type": "tool_call", "tool": fn_name, "args": args, "content": narration}
    return {"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)}


def chat_request_to_canonical(body: dict) -> tuple[list, list, Optional[str]]:
    """OpenAI /v1/chat/completions request body -> (messages, tools, system_prompt).

    ``messages`` is in BaseProvider canonical format; ``tools`` is the
    canonical ``[{"name", "description", "parameters"}]`` list;
    ``system_prompt`` is the concatenation of any system/developer messages
    (``None`` when there are none — the caller decides the default).
    """
    system_parts: list[str] = []
    messages: list[dict] = []
    # tool_call_id -> tool name, so a later role:"tool" message can build the
    # "[Tool result: <name>]" marker _tool_result_message() expects.
    pending_tool_names: dict[str, str] = {}

    for msg in body.get("messages", []) or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")

        if role in ("system", "developer"):
            text = msg.get("content")
            if isinstance(text, list):
                text = "\n".join(
                    str(p.get("text", "")) for p in text if isinstance(p, dict) and p.get("type") == "text"
                )
            if text:
                system_parts.append(str(text))
            continue

        if role == "assistant":
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                narration = str(msg.get("content") or "")
                for idx, tc in enumerate(tool_calls):
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    name = str(fn.get("name") or "")
                    if not name:
                        continue
                    # Narration only accompanies the FIRST call of a degraded
                    # parallel batch — see module docstring.
                    canonical = _tool_call_to_canonical(
                        name, fn.get("arguments"), narration if idx == 0 else "",
                    )
                    messages.append(canonical)
                    call_id = str(tc.get("id") or name)
                    pending_tool_names[call_id] = name
                continue
            messages.append({"role": "assistant", "content": _message_content_to_canonical(msg.get("content"))})
            continue

        if role == "tool":
            call_id = str(msg.get("tool_call_id") or "")
            name = pending_tool_names.pop(call_id, None) or "unknown"
            content = msg.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text"
                )
            messages.append({"role": "user", "content": f"[Tool result: {name}]\n{content}"})
            continue

        # "user" (and any unrecognized role — forwarded as-is, matching the
        # other providers' tolerant handling of unexpected roles).
        messages.append({"role": role or "user", "content": _message_content_to_canonical(msg.get("content"))})

    tools: list[dict] = []
    for tool in body.get("tools", []) or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") or {}
        name = tool.get("name") or fn.get("name")
        if not name:
            continue
        tools.append({
            "name": name,
            "description": fn.get("description") or tool.get("description") or "",
            "parameters": fn.get("parameters") or tool.get("parameters") or {"type": "object", "properties": {}},
        })

    system_prompt = "\n\n".join(system_parts) if system_parts else None
    return messages, tools, system_prompt


# ── Response: canonical -> OpenAI chat completions ───────────────────────────


def _finish_reason_for(stop_reason: Optional[str], has_tool_calls: bool) -> str:
    if has_tool_calls:
        return "tool_calls"
    reason = (stop_reason or "").strip().lower()
    if reason in _TRUNCATION_STOP_REASONS:
        return "length"
    if reason in _BLOCK_STOP_REASONS:
        return "content_filter"
    return "stop"


def _usage_to_openai(usage: object) -> Optional[dict]:
    if not isinstance(usage, dict) or not usage:
        return None
    prompt_tokens = int(usage.get("input") or 0)
    completion_tokens = int(usage.get("output") or 0)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _canonical_calls(resp: dict) -> list[dict]:
    """Expand a ProviderResponse tool_call (single or ``"batch"``) into a flat
    ``[{"tool", "args"}, ...]`` list — one entry for a normal call, several
    for a batch."""
    if resp.get("tool") == "batch":
        calls = (resp.get("args") or {}).get("calls") or []
        return [c for c in calls if isinstance(c, dict) and c.get("tool")]
    return [{"tool": resp.get("tool"), "args": resp.get("args") or {}}]


def _format_tool_calls(calls: list[dict]) -> list[dict]:
    return [
        {
            "id": f"call_{uuid.uuid4().hex[:12]}",
            "type": "function",
            "function": {
                "name": call.get("tool", "unknown"),
                "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False),
            },
        }
        for call in calls
    ]


def canonical_to_chat_response(resp: dict, model: str, request_id: str) -> dict:
    """BaseProvider ProviderResponse -> OpenAI /v1/chat/completions JSON."""
    is_tool_call = isinstance(resp, dict) and resp.get("type") == "tool_call"
    message: dict = {"role": "assistant"}

    if is_tool_call:
        tool_calls = _format_tool_calls(_canonical_calls(resp))
        message["content"] = resp.get("content") or None
        message["tool_calls"] = tool_calls
    else:
        message["content"] = resp.get("content", "") if isinstance(resp, dict) else str(resp)

    finish_reason = _finish_reason_for(resp.get("stop_reason") if isinstance(resp, dict) else None, is_tool_call)

    reply: dict = {
        "id": request_id,
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    usage = _usage_to_openai(resp.get("usage") if isinstance(resp, dict) else None)
    if usage is not None:
        reply["usage"] = usage
    return reply


def stream_chat_response(resp: dict, model: str, request_id: str) -> Iterator[str]:
    """BaseProvider ProviderResponse -> OpenAI chat completions SSE frames.

    Frame order: role delta -> content delta (text) or one tool_calls delta
    per call (tool_call) -> finish chunk -> ``data: [DONE]``. Each tool call
    is sent as ONE delta carrying its full name+arguments rather than
    character-by-character streaming — codex only needs the frame sequence
    to be well-formed, not token-granular.
    """
    is_tool_call = isinstance(resp, dict) and resp.get("type") == "tool_call"

    def _chunk(delta: dict, finish_reason: Optional[str] = None) -> str:
        payload = {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def _gen() -> Iterator[str]:
        yield _chunk({"role": "assistant"})
        if is_tool_call:
            for idx, call in enumerate(_format_tool_calls(_canonical_calls(resp))):
                yield _chunk({"tool_calls": [{
                    "index": idx,
                    "id": call["id"],
                    "type": "function",
                    "function": call["function"],
                }]})
            finish_reason = "tool_calls"
        else:
            content = resp.get("content", "") if isinstance(resp, dict) else str(resp)
            if content:
                yield _chunk({"content": content})
            finish_reason = _finish_reason_for(
                resp.get("stop_reason") if isinstance(resp, dict) else None, False,
            )
        yield _chunk({}, finish_reason=finish_reason)
        yield "data: [DONE]\n\n"

    return _gen()

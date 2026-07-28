"""Real-time HTTP SSE streaming for Codex Responses API endpoint (/v1/responses).

Provides sub-second token streaming directly into Codex Desktop GUI / CLI,
transmitting reasoning and narration deltas live as tokens arrive over
WebSocket from the browser extension.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
import re
from typing import TYPE_CHECKING, Any, Optional

from aiohttp import web

if TYPE_CHECKING:
    from codex_engine.engine import _CodexProxy

logger = logging.getLogger("kim.codex_bridge.streaming")


def _clean_reasoning_stream(full_so_far: str) -> str:
    if not full_so_far:
        return ""
    text = re.sub(r"\[END_OF_RESPONSE(?:_[A-Za-z0-9-]+)?\]", "", full_so_far, flags=re.IGNORECASE)
    text = re.sub(r"\bKIM_[a-f0-9]{8}\b", "", text)
    text = re.sub(r"#\s*Codex Agent[\s\S]*?(?=(?:\b(?:Hi|Hello|I'll|I will|Let's|Looking|Checking|Running|Updating|Here|Done)\b|```|\Z))", "", text, flags=re.IGNORECASE)
    text = re.sub(r"This document defines exactly how the Codex agent[\s\S]*?(?=(?:\b(?:Hi|Hello|I'll|I will|Let's|Looking|Checking|Running|Updating|Here|Done)\b|```|\Z))", "", text, flags=re.IGNORECASE)
    text = re.sub(r"## Single Command Turn-by-Turn Protocol[\s\S]*?(?=(?:\b(?:Hi|Hello|I'll|I will|Let's|Looking|Checking|Running|Updating|Here|Done)\b|```|\Z))", "", text, flags=re.IGNORECASE)
    text = re.sub(r"in your response to cite this file[^\n]*\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"Make sure to include\s+(?:filecite[^]+|turn\d+file\d+|\s*)*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"filecite[^]+", "", text)
    text = re.sub(r"\bturn\d+file\d+\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*Make sure to include\s*$", "", text, flags=re.IGNORECASE | re.MULTILINE).strip()
    if text.startswith("{"):
        m = re.search(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)', text)
        if m:
            text = m.group(1).replace('\\"', '"').replace('\\n', '\n')
    return text.strip()


async def stream_responses_http(
    proxy: _CodexProxy,
    request: web.Request,
    body: dict,
    input_items: list,
    prompt: str,
    clear_chat: bool,
    is_first_relay: bool,
    handoff: Optional[str],
    relay_num: int,
) -> web.StreamResponse:
    """Stream a /v1/responses request live via SSE as tokens arrive from the provider."""
    stream_res = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await stream_res.prepare(request)

    resp_id = f"resp_{uuid.uuid4().hex[:16]}"
    reasoning_id = f"item_{uuid.uuid4().hex[:12]}"
    msg_item_id = f"item_{uuid.uuid4().hex[:12]}"

    in_progress = {
        "id": resp_id,
        "object": "response",
        "status": "in_progress",
        "output": [],
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    }

    # 1. Send response.created
    await stream_res.write(
        f"data: {json.dumps({'type': 'response.created', 'response': in_progress})}\n\n".encode("utf-8")
    )

    # 2. Send response.output_item.added for reasoning (index 0) and message (index 1)
    reasoning_item = {
        "id": reasoning_id,
        "type": "reasoning",
        "reasoning_text": "",
        "summary": [{"type": "summary_text", "text": ""}],
        "status": "in_progress",
    }
    await stream_res.write(
        f"data: {json.dumps({'type': 'response.output_item.added', 'output_index': 0, 'item': reasoning_item})}\n\n".encode("utf-8")
    )

    msg_item_added = {
        "id": msg_item_id,
        "type": "message",
        "role": "assistant",
        "content": [],
        "status": "in_progress",
    }
    await stream_res.write(
        f"data: {json.dumps({'type': 'response.output_item.added', 'output_index': 1, 'item': msg_item_added})}\n\n".encode("utf-8")
    )
    await stream_res.write(
        f"data: {json.dumps({'type': 'response.content_part.added', 'item_id': msg_item_id, 'output_index': 1, 'content_index': 0, 'part': {'type': 'output_text', 'text': ''}})}\n\n".encode("utf-8")
    )

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    accumulated_reasoning: list[str] = []
    last_emitted_len = 0
    is_streaming_done = False

    def _on_delta(delta: str) -> None:
        if delta:
            loop.call_soon_threadsafe(queue.put_nowait, delta)

    async def _heartbeat_worker():
        while not is_streaming_done:
            await asyncio.sleep(1.5)
            if not is_streaming_done:
                try:
                    await stream_res.write(b": ping\n\n")
                    await stream_res.drain()
                except Exception:
                    break

    async def _stream_worker():
        nonlocal last_emitted_len
        while True:
            chunk = await queue.get()
            if chunk is None:
                queue.task_done()
                break
            accumulated_reasoning.append(chunk)
            full_so_far = "".join(accumulated_reasoning)
            clean_full = _clean_reasoning_stream(full_so_far)
            if len(clean_full) > last_emitted_len:
                new_delta = clean_full[last_emitted_len:]
                last_emitted_len = len(clean_full)
                ev1 = {
                    "type": "response.reasoning_summary.text.delta",
                    "item_id": reasoning_id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": new_delta,
                }
                ev2 = {
                    "type": "response.reasoning.text.delta",
                    "item_id": reasoning_id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": new_delta,
                }
                ev3 = {
                    "type": "response.output_text.delta",
                    "item_id": msg_item_id,
                    "output_index": 1,
                    "content_index": 0,
                    "delta": new_delta,
                }
                try:
                    await stream_res.write(f"data: {json.dumps(ev1)}\n\n".encode("utf-8"))
                    await stream_res.write(f"data: {json.dumps(ev2)}\n\n".encode("utf-8"))
                    await stream_res.write(f"data: {json.dumps(ev3)}\n\n".encode("utf-8"))
                    await stream_res.drain()
                except Exception:
                    pass
            queue.task_done()

    worker_task = asyncio.create_task(_stream_worker())
    heartbeat_task = asyncio.create_task(_heartbeat_worker())

    # 3. Call provider complete with on_delta callback
    extra_kwargs = {"handoff": handoff} if handoff else {}
    from codex_engine.engine import (
        _is_repeat_of_previous,
        _make_responses_text_reply,
        _provider_response_to_responses_api,
        _surface_relay_reasoning,
        _system_prompt_for,
        _tool_command_signature,
        _humanize_single,
        _signature_subcommands,
    )

    try:
        response = await proxy._provider.complete(
            messages=[{"role": "user", "content": prompt}],
            tools=body.get("tools", []),
            system=_system_prompt_for(proxy._provider_name),
            clear_chat=clear_chat,
            on_delta=_on_delta,
            **extra_kwargs,
        )
    finally:
        is_streaming_done = True
        heartbeat_task.cancel()

    # Signal worker to finish draining deltas
    await queue.put(None)
    await worker_task

    # 4. Post-process response (contract nudge, thread state, reasoning log)
    response = await proxy._nudge_contract_retry(response, relay_num)
    proxy._note_relay_result(
        is_first_relay=is_first_relay,
        cleared_chat=clear_chat,
        consumed_handoff=handoff,
        response=response,
    )

    _surface_relay_reasoning(response, relay_num)
    responses_reply = _provider_response_to_responses_api(
        response, relay_num, request_tools=body.get("tools"),
        metrics=proxy._thread_state.setdefault("repairs", {}),
    )

    cmds = _tool_command_signature(responses_reply)
    if _is_repeat_of_previous(cmds, proxy._last_tool_commands):
        proxy._repeat_count = getattr(proxy, "_repeat_count", 0) + 1
        if proxy._repeat_count >= 2:
            logger.info(f"[relay #{relay_num}] Repeated tool call twice — ending turn (loop guard)")
            subs = sorted(_signature_subcommands(cmds))
            if subs:
                did = " and ".join(_humanize_single(s).lower() for s in subs)
                done_text = f"Done — {did} already ran; nothing left to do."
            else:
                done_text = "Done — that command already ran; nothing left to do."
            responses_reply = _make_responses_text_reply(resp_id, done_text)
            cmds = None
    else:
        proxy._repeat_count = 0
    proxy._last_tool_commands = cmds
    proxy._last_proxy_response = responses_reply

    # 5. Complete reasoning item 0
    if not hasattr(proxy, "_accumulated_thinking_lines") or proxy._accumulated_thinking_lines is None:
        proxy._accumulated_thinking_lines = []

    curr_reasoning = _clean_reasoning_stream("".join(accumulated_reasoning))
    if not curr_reasoning:
        for item in responses_reply.get("output") or []:
            if isinstance(item, dict) and item.get("type") == "reasoning":
                curr_reasoning = str(item.get("reasoning_text") or "")
                if curr_reasoning:
                    break
    if curr_reasoning and curr_reasoning not in ("Thinking...", "Done."):
        bullet_line = f"• {curr_reasoning}"
        if bullet_line not in proxy._accumulated_thinking_lines:
            proxy._accumulated_thinking_lines.append(bullet_line)

    from codex_engine.engine import _build_summary_items
    full_reasoning = "\n".join(proxy._accumulated_thinking_lines) if proxy._accumulated_thinking_lines else (curr_reasoning or "Thinking...")
    summary_items = _build_summary_items(full_reasoning)

    has_message_item = any(
        isinstance(it, dict) and it.get("type") == "message"
        for it in (responses_reply.get("output") or [])
    )

    # Ensure responses_reply reasoning items reflect accumulated thinking without duplicating message text
    for item in responses_reply.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "reasoning":
            if not has_message_item:
                item["reasoning_text"] = full_reasoning
            else:
                item.pop("reasoning_text", None)
            item["summary"] = summary_items

    ev_reasoning_done = {
        "type": "response.reasoning.text.done",
        "item_id": reasoning_id,
        "output_index": 0,
        "content_index": 0,
        "text": full_reasoning if not has_message_item else "",
    }
    await stream_res.write(f"data: {json.dumps(ev_reasoning_done)}\n\n".encode("utf-8"))

    item_dict = {
        "id": reasoning_id,
        "type": "reasoning",
        "summary": summary_items,
        "status": "completed",
    }
    if not has_message_item:
        item_dict["reasoning_text"] = full_reasoning

    reasoning_item_completed = {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": item_dict,
    }
    await stream_res.write(f"data: {json.dumps(reasoning_item_completed)}\n\n".encode("utf-8"))

    final_text = curr_reasoning or "".join(accumulated_reasoning)
    for item in responses_reply.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "message":
            for block in (item.get("content") or []):
                if isinstance(block, dict) and block.get("type") == "output_text":
                    t = str(block.get("text") or "")
                    if t:
                        final_text = t
                        break

    part_dict = {"type": "output_text", "text": final_text}
    await stream_res.write(
        f"data: {json.dumps({'type': 'response.content_part.done', 'item_id': msg_item_id, 'output_index': 1, 'content_index': 0, 'part': part_dict})}\n\n".encode("utf-8")
    )
    await stream_res.write(
        f"data: {json.dumps({'type': 'response.output_item.done', 'output_index': 1, 'item': {'id': msg_item_id, 'type': 'message', 'role': 'assistant', 'status': 'completed', 'content': [part_dict]}})}\n\n".encode("utf-8")
    )

    # Ensure responses_reply output contains message item
    has_msg = False
    for item in (responses_reply.get("output") or []):
        if isinstance(item, dict) and item.get("type") == "message":
            has_msg = True
            if not item.get("content"):
                item["content"] = [part_dict]
    if not has_msg:
        responses_reply.setdefault("output", []).append({
            "id": msg_item_id,
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [part_dict],
        })

    # 6. Stream remaining tool call output items
    out_idx = 2
    for item in responses_reply.get("output") or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type", "")
        if itype in ("reasoning", "message"):
            continue
        item_id = str(item.get("id") or f"item_{uuid.uuid4().hex[:12]}")
        item_dict = {**item, "id": item_id}

        if itype in ("function_call", "custom_tool_call"):
            arguments = str(item.get("arguments") or item.get("input") or "{}")
            await stream_res.write(
                f"data: {json.dumps({'type': 'response.output_item.added', 'output_index': out_idx, 'item': {**item_dict, 'arguments': '', 'status': 'in_progress'}})}\n\n".encode("utf-8")
            )
            await stream_res.write(
                f"data: {json.dumps({'type': 'response.function_call_arguments.delta', 'item_id': item_id, 'output_index': out_idx, 'delta': arguments})}\n\n".encode("utf-8")
            )
            await stream_res.write(
                f"data: {json.dumps({'type': 'response.function_call_arguments.done', 'item_id': item_id, 'output_index': out_idx, 'arguments': arguments})}\n\n".encode("utf-8")
            )
            await stream_res.write(
                f"data: {json.dumps({'type': 'response.output_item.done', 'output_index': out_idx, 'item': {**item_dict, 'status': 'completed'}})}\n\n".encode("utf-8")
            )
        else:
            await stream_res.write(
                f"data: {json.dumps({'type': 'response.output_item.added', 'output_index': out_idx, 'item': item_dict})}\n\n".encode("utf-8")
            )
            await stream_res.write(
                f"data: {json.dumps({'type': 'response.output_item.done', 'output_index': out_idx, 'item': item_dict})}\n\n".encode("utf-8")
            )
        out_idx += 1

    # 7. Send final response.completed and [DONE] sentinel
    await stream_res.write(
        f"data: {json.dumps({'type': 'response.completed', 'response': responses_reply})}\n\n".encode("utf-8")
    )
    await stream_res.write(b"data: [DONE]\n\n")
    await stream_res.write_eof()
    return stream_res

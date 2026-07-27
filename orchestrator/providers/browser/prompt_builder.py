"""
Prompt formatting for the browser provider.

Builds the text prompt injected into browser-based chat UIs. Handles:
- System prompt + tool list (first message only)
- History recap for resumed sessions
- Data URI extraction into attachments
- Completion hash generation
"""

import json
import logging
import os
import platform
import re
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

# Internal status/thinking narration that must never be replayed into the next
# browser prompt recap (#39). Covers every phrasing the frontend's
# speakAsKimNarration and the orchestrator's emit_status produce:
#   "Kim is thinking…", "Kim is still thinking… (3s)", "Kim is working",
#   "Kim is working on it…", "Kim is still working on it".
# Only applied to single-line assistant content (see build_history_recap), so a
# real one-line answer that happens to start "Kim is …" is still preserved
# unless it exactly matches this status shape.
_STATUS_RECAP_RE = re.compile(
    r"^kim is (?:still )?(?:thinking|working)(?: on it)?"
    r"[.…]*(?:\s*\(\d+s\))?$",
    re.IGNORECASE,
)

_DATA_URI_PREFIX = "data:"
_DATA_URI_BASE64_MARKER = ";base64,"
# M1: a real data-URI mime type ("image/png", "application/pdf") — one token,
# no whitespace. Without this check, a stray "data:" substring in prose (e.g.
# "metadata: 2026") pairs with a LATER real ";base64," and swallows all the
# prompt text in between as a bogus "mime type".
_DATA_URI_MIME_RE = re.compile(r"[a-z0-9.+-]+/[a-z0-9.+-]+\Z")
_DATA_URI_PARAM_RE = re.compile(r"[a-z0-9!#$&^_.+-]+=[^;\s,]+\Z")


def ext_for_mime(mime_type: str) -> str:
    mime_type = (mime_type or "").lower().strip()
    ext_map = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/webp": "webp",
        "image/gif": "gif",
        "image/svg+xml": "svg",
        "application/pdf": "pdf",
        "text/plain": "txt",
        "text/markdown": "md",
        "application/json": "json",
        "application/zip": "zip",
        "application/octet-stream": "bin",
    }
    if mime_type in ext_map:
        return ext_map[mime_type]
    if "/" in mime_type:
        tail = mime_type.split("/")[-1].split("+")[0].strip()
        if tail:
            return tail
    return "bin"


def append_attachment(
    attachments_out: list[dict],
    mime_type: str,
    data_b64: str,
    name: Optional[str] = None,
) -> None:
    if not data_b64:
        return
    clean_mime = mime_type.strip().lower() if mime_type else "application/octet-stream"
    if "/" not in clean_mime:
        clean_mime = "application/octet-stream"
    idx = len(attachments_out) + 1
    ext = ext_for_mime(clean_mime)
    default_name = (
        f"screenshot_{idx}.{ext}"
        if clean_mime.startswith("image/")
        else f"attachment_{idx}.{ext}"
    )
    attachments_out.append(
        {
            "name": (name or default_name).strip() or default_name,
            "mime_type": clean_mime,
            "data_base64": data_b64,
        }
    )


def strip_data_uris(text: str, attachments_out: list[dict]) -> str:
    """Extract inline ``data:<mime>;base64,...`` URIs into attachments."""
    out_parts: list[str] = []
    i = 0
    prefix = _DATA_URI_PREFIX
    marker = _DATA_URI_BASE64_MARKER

    while True:
        start = text.find(prefix, i)
        if start == -1:
            out_parts.append(text[i:])
            break

        marker_pos = text.find(marker, start)
        if marker_pos == -1:
            out_parts.append(text[i:])
            break

        media_header = text[start + len(prefix):marker_pos].strip().lower()
        header_parts = [part.strip() for part in media_header.split(";")]
        mime_type = header_parts[0] if header_parts else ""
        valid_header = bool(
            _DATA_URI_MIME_RE.fullmatch(mime_type)
            and all(_DATA_URI_PARAM_RE.fullmatch(part) for part in header_parts[1:])
        )
        payload_start = marker_pos + len(marker)
        end = payload_start
        while end < len(text) and text[end] not in " \t\n\r\"'<>)],;":
            end += 1

        payload = text[payload_start:end]
        if payload and valid_header:
            out_parts.append(text[i:start])
            append_attachment(attachments_out, mime_type, payload)
            if mime_type.startswith("image/"):
                out_parts.append("[Screenshot attached]")
            else:
                out_parts.append(f"[Attachment: {mime_type}]")
            i = end
        else:
            out_parts.append(text[i:start + len(prefix)])
            i = start + len(prefix)

    return "".join(out_parts)


def build_history_recap(
    prior_messages: list[dict],
    *,
    max_recap: int = 2000,
    max_item_chars: int = 400,
) -> str:
    """Compact recap of prior turns, used on first send of a resumed session."""
    lines: list[str] = []
    for msg in prior_messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if isinstance(content, list):
            text_parts = [
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            content = " ".join(p for p in text_parts if p)

        content = str(content).strip()
        if not content:
            continue
        if content.startswith("[Tool result:"):
            continue

        if role == "user":
            if content.startswith("Task: "):
                content = content[6:].strip()
            if not content:
                continue
            if len(content) > max_item_chars:
                content = content[:max_item_chars] + "…"
            lines.append(f"User: {content}")
        elif role == "assistant":
            if "\n" not in content and _STATUS_RECAP_RE.match(content):
                continue
            stripped = content.lstrip()
            if stripped.startswith("{") and '"tool"' in stripped:
                try:
                    if isinstance(json.loads(stripped), dict):
                        continue
                except Exception:
                    pass
            if len(content) > max_item_chars:
                content = content[:max_item_chars] + "…"
            lines.append(f"Kim: {content}")

    if not lines:
        return ""

    recap = "\n".join(lines)
    if len(recap) > max_recap:
        recap = "…\n" + recap[-max_recap:]
    return recap


def transport_marker_instruction(completion_hash: str) -> str:
    return (
        f"IMPORTANT: Always append the exact string {completion_hash} "
        "at the very end of your entire response. Do not append any other "
        "END_OF_RESPONSE marker or KIM_* token."
    )


def format_prompt(
    messages: list[dict],
    tools: list[dict],
    system: str,
    *,
    sent_system_prompt: bool,
    max_inject_chars: int,
    use_webview_bridge: bool,
    handoff_summary: Optional[str] = None,
    preferred_site: Optional[str] = None,
) -> tuple[str, list[dict], str, bool]:
    """
    Stateful prompt formatter for browser-based chat UIs.

    ``handoff_summary`` is the model-written compact artifact from a previous
    thread that this fresh thread continues. It is injected once, on the same
    first send as the system prompt, and replaces nothing — the (short) recap
    of the verbatim message tail still follows it.

    Returns:
        (prompt_text, attachments, completion_hash, new_sent_system_prompt)
    """
    attachments: list[dict] = []

    last_text = ""
    if messages:
        last_msg = messages[-1]
        content = last_msg["content"]

        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                item_type = item.get("type", "")
                if item_type == "text" and item.get("text"):
                    cleaned = strip_data_uris(item["text"], attachments)
                    text_parts.append(cleaned)
                elif item_type == "image" and item.get("data"):
                    append_attachment(
                        attachments,
                        str(item.get("media_type") or "image/png"),
                        str(item["data"]),
                        str(item.get("name") or "").strip() or None,
                    )
                    if use_webview_bridge:
                        text_parts.append(
                            "[Screenshot attached. If it is not visible and the task is not "
                            "a visual-inspection task, continue using observe_ui and other tools.]"
                        )
                    else:
                        text_parts.append("[Screenshot attached]")
                elif item_type in {"file", "document", "attachment"} and item.get("data"):
                    file_name = str(item.get("name") or item.get("filename") or "").strip() or None
                    mime_type = str(
                        item.get("media_type")
                        or item.get("mime_type")
                        or "application/octet-stream"
                    )
                    append_attachment(
                        attachments,
                        mime_type,
                        str(item.get("data") or ""),
                        file_name,
                    )
                    if file_name:
                        text_parts.append(f"[Attachment: {file_name}]")
                    else:
                        text_parts.append("[Attachment attached]")
            last_text = "\n".join(text_parts)
        else:
            last_text = strip_data_uris(str(content), attachments)

def _compress_image_b64(img_path: str, max_dim: int = 1000, quality: int = 75) -> tuple[str, str]:
    try:
        from PIL import Image
        import io, base64
        with Image.open(img_path) as img:
            img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            return base64.b64encode(buf.getvalue()).decode("utf-8"), "image/jpeg"
    except Exception:
        import base64
        with open(img_path, "rb") as f:
            mime = "image/png"
            if img_path.lower().endswith((".jpg", ".jpeg")):
                mime = "image/jpeg"
            elif img_path.lower().endswith(".webp"):
                mime = "image/webp"
            return base64.b64encode(f.read()).decode("utf-8"), mime


    # Convert Codex Desktop <image path="..."> XML tags and ## file.png headers into real base64 attachments
    def _parse_image_xml_tag(match):
        img_path = match.group(1).strip()
        if os.path.isfile(img_path):
            try:
                b64, mime = _compress_image_b64(img_path)
                append_attachment(attachments, mime, b64, name=os.path.basename(img_path))
                return f"![{os.path.basename(img_path)}](data:{mime};base64,{b64})"
            except Exception:
                pass
        return ""

    def _parse_header_image_path(match):
        img_path = match.group(2).strip()
        if os.path.isfile(img_path) and img_path.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            try:
                b64, mime = _compress_image_b64(img_path)
                append_attachment(attachments, mime, b64, name=os.path.basename(img_path))
                return f"## {match.group(1)}: ![{os.path.basename(img_path)}](data:{mime};base64,{b64})"
            except Exception:
                pass
        return match.group(0)

    last_text = re.sub(r'##\s+([^\n]+\.(?:png|jpg|jpeg|webp|gif)):\s*(/(?:tmp|var|Users)/[^\s\n]+)', _parse_header_image_path, last_text, flags=re.IGNORECASE)
    last_text = re.sub(r'<image\b[^>]*\bpath="([^"]+)"[^>]*>', _parse_image_xml_tag, last_text)
    last_text = re.sub(r'</image>', '', last_text)

    last_text = last_text.strip()

    unique_id = uuid.uuid4().hex[:8]
    completion_hash = f"[END_OF_RESPONSE_{unique_id}]"
    system_lower = system.lower()
    is_codex_bridge = (
        "you are answering codex" in system_lower
        or "you are codex" in system_lower
        or "codex bridge json" in system_lower
        or "codex bridge terminal" in system_lower
        or "codex, a coding agent" in system_lower
        or "available codex tools" in system_lower
    )

    history_block = ""
    if not sent_system_prompt and len(messages) > 1:
        restored_thread = os.environ.get("KIM_BROWSER_RESTORE_STATUS", "").strip().lower() == "stored_thread"
        recap = build_history_recap(
            messages[:-1],
            max_recap=700 if restored_thread else 2000,
            max_item_chars=220 if restored_thread else 400,
        )
        if recap:
            if restored_thread:
                history_block = (
                    "[BRIEF PRIOR CONTEXT — browser thread was restored; this is only a refresher. "
                    "Do not re-execute previous actions.]\n"
                    f"{recap}\n"
                    "[END BRIEF PRIOR CONTEXT]\n\n"
                    "Now respond to the next user message:\n\n"
                )
            else:
                history_block = (
                    "[PRIOR CONVERSATION — for context only; do not re-execute, "
                    "just use as background.]\n"
                    f"{recap}\n"
                    "[END PRIOR CONVERSATION]\n\n"
                    "Now respond to the next user message:\n\n"
                )

    handoff_block = ""
    if not sent_system_prompt and handoff_summary and handoff_summary.strip():
        handoff_block = (
            "[HANDOFF FROM PREVIOUS CHAT — READ THIS FIRST]\n"
            "This chat continues an earlier conversation that was summarized to "
            "save space. The summary below IS our prior conversation: treat it as "
            "established context and answer any question about what we discussed "
            "or did from it. The operating instructions that follow are system "
            "rules, NOT part of our conversation. Do not re-execute actions the "
            "summary lists as already done.\n"
            f"{handoff_summary.strip()}\n"
            "[END HANDOFF]\n\n"
        )

    new_sent_system_prompt = sent_system_prompt
    if not sent_system_prompt:
        # Not every advertised tool carries a "name": codex 0.144.3 sends
        # {"type": "web_search"} and {"type": "tool_search", …} as bare typed
        # entries. Fall back to the type, and skip anything with neither —
        # a hard t["name"] here raised KeyError 'name' and took down every
        # relay the moment codex advertised its real tool list.
        compact_tools = [
            {
                "name": t.get("name") or t.get("type"),
                "description": t.get("description", ""),
                "args": list(
                    (t.get("parameters") or {}).get("properties", {}).keys()
                ),
            }
            for t in tools
            if isinstance(t, dict) and (t.get("name") or t.get("type"))
        ]
        tools_json = json.dumps(compact_tools, indent=2)

        _sys = platform.system()
        _home = os.path.expanduser("~")
        if _sys == "Darwin":
            _os_hint = f"You are running on macOS. Home is {_home}. Use 'open' to launch apps and POSIX paths."
        elif _sys == "Linux":
            _os_hint = f"You are running on Linux. Home is {_home}. Use 'xdg-open' to launch apps and POSIX paths."
        else:
            _os_hint = f"You are running on Windows. Home is {_home}. Use 'start' to launch apps and Windows paths."

        if is_codex_bridge:
            prompt = (
                # Handoff goes FIRST so a fresh chat anchors on the prior
                # conversation rather than the large [SYSTEM] block below it.
                f"{handoff_block}"
                f"[SYSTEM]\n{system}\n"
                f"{_os_hint}\n\n"
                f"[AVAILABLE TOOLS]\n{tools_json}\n\n"
                + transport_marker_instruction(completion_hash) + "\n\n"
                f"{history_block}"
                f"{last_text}"
            )
        else:
            prompt = (
                # Handoff goes FIRST so a fresh chat anchors on the prior
                # conversation rather than the large [SYSTEM] block below it.
                f"{handoff_block}"
                f"[SYSTEM]\n{system}\n"
                f"{_os_hint}\n\n"
                f"[AVAILABLE TOOLS]\n{tools_json}\n\n"
                "[INSTRUCTIONS]\n"
                "You are operating as Kim through local tools. The website/model name "
                "does not matter. Do not claim you cannot access the computer if "
                "a listed tool can do it.\n"
                "For normal UI work, prefer observe_ui and click_ui. Use screenshots "
                "only for visual-inspection tasks or when structured UI is insufficient.\n"
                "After editing a lintable code file, run lint_file on it to catch "
                "errors before moving on.\n"
                "If web_open returns AUTH_REQUIRED or AUTH_FAILED, the page content is "
                "not usable yet. If the current task only asked to open a site, respond "
                "TASK_COMPLETE saying it is open at the sign-in prompt. Do not log in "
                "unless the current task explicitly asks you to sign in or provides "
                "credentials. Never reuse credentials from recent context alone.\n"
                "When web_search returns action=provider_native_web_search, use this "
                "chat provider's built-in web search in the current conversation, cite "
                "source names and URLs, then continue the original task. Do not call "
                "web_search again for the same query.\n"
                "For long commands, background_start returns a job_id. Continue other "
                "useful work and use background_poll later; do not busy-loop polls. Use "
                "background_cancel only when the command is no longer wanted.\n"
                "When ask_user returns NEED_HELP text, echo that exact NEED_HELP message "
                "as your response. This intentionally pauses the run so the user can "
                "answer on the next turn; do not treat it as a tool failure.\n"
                "THINKING: Before each tool call or TASK_COMPLETE, write 1-2 sentences "
                "of plain text narrating what you are about to do and why. The user's "
                "Thinking panel shows this stream live — be brief and natural.\n"
                "PLANNING: For multi-step tasks, emit a plan BEFORE your first tool call "
                "on its own turn:\n"
                "  PLAN: <n> steps\n"
                "  1. <short step name>\n"
                "  2. <short step name>\n"
                "Then before each step emit: STEP <n>: <name>\n"
                "After each step emit: DONE <n>: <brief result>\n"
                "The UI renders a live checklist from PLAN/STEP/DONE markers.\n"
                "Respond with EXACTLY ONE of:\n"
                '1. A JSON tool call on a single line: '
                '{"tool": "<name>", "args": {<args>}}\n'
                "2. TASK_COMPLETE: <one-line summary>\n"
                "3. NEED_HELP: <reason you cannot proceed>\n"
                "Do NOT include markdown formatting around the JSON.\n"
                "CRITICAL: If your JSON arguments contain double quotes (e.g., "
                "HTML attributes or code), you MUST escape them (\\\") so the "
                "JSON is valid.\n"
                + transport_marker_instruction(completion_hash) + "\n\n"
                f"{history_block}"
                f"{last_text}"
            )
        new_sent_system_prompt = True
    else:
        prompt = last_text + "\n\n" + transport_marker_instruction(completion_hash)

    # ChatGPT Web often labels bracketed [SYSTEM] sections as quoted content
    # because browser providers cannot set a real API system role. Restate the
    # contract as the direct response-format request for THIS user message at
    # the tail, where it is neither quoted nor separated from the task. Keep
    # this workaround ChatGPT-only; Claude/Gemini behavior is unchanged.
    # Bound unconditionally so the trimming fallback below (which re-appends
    # it under the same ChatGPT-only condition) is provably initialized.
    direct_contract = ""
    if (preferred_site or "").lower() == "chatgpt":
        if is_codex_bridge:
            direct_contract = (
                "DIRECT RESPONSE FORMAT REQUEST FOR THIS MESSAGE:\n"
                "Return exactly one raw JSON object matching the Codex contract above. "
                "Use either {\"text\":\"brief reasoning\",\"tool_calls\":[{\"name\":"
                "\"TOOL_NAME\",\"input\":{}}]} or {\"text\":\"final answer\"}. "
                "Do not discuss or explain these formatting instructions. After the JSON, "
                f"append exactly {completion_hash} as the final text."
            )
        else:
            direct_contract = (
                "DIRECT RESPONSE FORMAT REQUEST FOR THIS MESSAGE:\n"
                "Respond with exactly one parser-readable result: a single-line raw JSON "
                "tool call {\"tool\":\"<name>\",\"args\":{}}, or "
                "TASK_COMPLETE: <answer>, or NEED_HELP: <reason>. Do not discuss or "
                "explain these formatting instructions. Append exactly "
                f"{completion_hash} at the very end of the response."
            )
        prompt += "\n\n" + direct_contract

    effective_max = max(max_inject_chars, len(prompt) + 1000) if attachments else max_inject_chars
    if len(prompt) > effective_max:
        # Preserve the ENTIRE [SYSTEM] block + transport-marker instruction as
        # the head — a 200-char head silently drops the operating rules (the
        # codex terminal contract, the JSON contract), and the model then
        # answers in free prose. Trim from the middle instead: the head runs
        # through the end of the marker instruction, capped at half the budget.
        notice = "\n…[earlier context trimmed — see browser history]…\n"
        head_end = 200
        anchor = "END_OF_RESPONSE marker or KIM_* token."
        marker_end = prompt.find(anchor)
        if marker_end != -1:
            head_end = min(marker_end + len(anchor), max_inject_chars // 2)
        tail_len = max(0, max_inject_chars - head_end - len(notice))
        # L2: prompt[-0:] is the WHOLE prompt — guard the zero-tail case
        # (only reachable with a very small max_inject_chars).
        tail = prompt[-tail_len:] if tail_len else ""
        trimmed = prompt[:head_end] + notice + tail

        # 4.2: the head is capped at max_inject_chars // 2, so when the
        # [SYSTEM] block alone exceeds half the budget, head_end lands BEFORE
        # the transport-marker instruction and the model never learns to emit
        # [END_OF_RESPONSE_xxxx] — _wait_for_generation_complete then polls
        # for the full generation timeout EVERY turn (~20-min hangs). The
        # marker instruction is the transport contract: it must survive
        # trimming unconditionally, so re-append it if the trim cut it.
        marker_instr = transport_marker_instruction(completion_hash)
        if marker_instr not in trimmed:
            suffix = "\n\n" + marker_instr
            budget = max(0, max_inject_chars - len(suffix))
            head_end = min(head_end, budget)
            tail_len = max(0, budget - head_end - len(notice))
            tail = prompt[-tail_len:] if tail_len else ""
            trimmed = prompt[:head_end] + notice + tail + suffix

        # A pathological tiny budget can otherwise preserve only transport
        # instructions and discard the user's task. The task and marker are
        # the irreducible payload, so in this case the limit is a soft cap.
        task_tail = next(
            (line.strip() for line in reversed(last_text.splitlines()) if line.strip()),
            "",
        )
        if task_tail and task_tail not in trimmed:
            # Below the size of the transport scaffolding itself, the limit is
            # already necessarily a soft cap. Preserve the complete user turn
            # in that pathological case rather than silently reducing a
            # multi-line request to only its final line.
            essential_task = (
                last_text
                if max_inject_chars < len(marker_instr) + len(notice)
                else task_tail
            )
            essential = essential_task + "\n\n" + marker_instr
            if (preferred_site or "").lower() == "chatgpt":
                essential += "\n\n" + direct_contract
            trimmed = essential
        prompt = trimmed

    return prompt, attachments, completion_hash, new_sent_system_prompt

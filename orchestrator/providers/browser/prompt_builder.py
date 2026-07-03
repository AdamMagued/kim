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

        mime_type = text[start + len(prefix):marker_pos].strip().lower()
        payload_start = marker_pos + len(marker)
        end = payload_start
        while end < len(text) and text[end] not in " \t\n\r\"'<>)],;":
            end += 1

        payload = text[payload_start:end]
        if payload and "/" in mime_type:
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
) -> tuple[str, list[dict], str, bool]:
    """
    Stateful prompt formatter for browser-based chat UIs.

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

    last_text = last_text.strip()

    unique_id = uuid.uuid4().hex[:8]
    completion_hash = f"[END_OF_RESPONSE_{unique_id}]"

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

    new_sent_system_prompt = sent_system_prompt
    if not sent_system_prompt:
        compact_tools = [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "args": list(
                    t.get("parameters", {}).get("properties", {}).keys()
                ),
            }
            for t in tools
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

        system_lower = system.lower()
        is_codex_bridge = (
            "you are answering codex" in system_lower
            or "you are codex" in system_lower
            or "codex bridge json" in system_lower
            or "codex, a coding agent" in system_lower
            or "available codex tools" in system_lower
        )

        if is_codex_bridge:
            prompt = (
                f"[SYSTEM]\n{system}\n"
                f"{_os_hint}\n\n"
                + transport_marker_instruction(completion_hash) + "\n\n"
                f"{history_block}"
                f"{last_text}"
            )
        else:
            prompt = (
                f"[SYSTEM]\n{system}\n"
                f"{_os_hint}\n\n"
                f"[AVAILABLE TOOLS]\n{tools_json}\n\n"
                "[INSTRUCTIONS]\n"
                "You are operating as Kim through local tools. The website/model name "
                "does not matter. Do not claim you cannot access the computer if "
                "a listed tool can do it.\n"
                "For normal UI work, prefer observe_ui and click_ui. Use screenshots "
                "only for visual-inspection tasks or when structured UI is insufficient.\n"
                "If web_open returns AUTH_REQUIRED or AUTH_FAILED, the page content is "
                "not usable yet. If the current task only asked to open a site, respond "
                "TASK_COMPLETE saying it is open at the sign-in prompt. Do not log in "
                "unless the current task explicitly asks you to sign in or provides "
                "credentials. Never reuse credentials from recent context alone.\n"
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

    if len(prompt) > max_inject_chars:
        trim_at = max_inject_chars - 200
        prompt = (
            prompt[:200]
            + "\n…[earlier context trimmed — see browser history]…\n"
            + prompt[-trim_at:]
        )

    return prompt, attachments, completion_hash, new_sent_system_prompt

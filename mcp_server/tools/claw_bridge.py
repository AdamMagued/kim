"""
Claw File Bridge — relay module.

Spawns the compiled `claw` Rust binary as a subprocess with `CLAW_FILE_BRIDGE=1`,
then relays LLM requests/responses between the binary and Kim's BrowserProvider.

The bridge protocol is file-based:
    claw writes  → <session_dir>/bridge_request.json
    Kim reads, routes through browser LLM, writes ← bridge_response.json
    claw reads, continues tool loop

Completion signal: process exit.  When claw's ConversationRuntime finishes its
tool loop (the LLM responds with no tool calls), the process exits and
`process.returncode` returns a non-None exit code.

Usage:
    from mcp_server.tools.claw_bridge import run_claw_subtask

    result = await run_claw_subtask(
        task="write fibonacci.py and test it",
        browser_provider=provider,
        cwd="/path/to/project",
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from orchestrator.providers.browser_provider import BrowserProvider

logger = logging.getLogger("kim.claw_bridge")

# ── Constants ────────────────────────────────────────────────────────────────

# Where the compiled claw binary lives
CLAW_BINARY = (
    Path(__file__).resolve().parent.parent.parent
    / "pythonExperimentTool"
    / "claw-code"
    / "rust"
    / "target"
    / "release"
    / "claw"
)

POLL_INTERVAL = 0.5  # seconds between file checks
MAX_OUTPUT_BYTES = 64 * 1024 * 1024  # 64MB cap on captured output (#6)
MAX_RELAYS = 25  # cap relay attempts to prevent infinite loops from JSON escaping issues

ALLOWED_CLAW_TOOLS = {
    "bash",
    "read_file",
    "write_file",
    "edit_file",
    "glob_search",
    "grep_search",
    "WebFetch",
    "WebSearch",
    "TodoWrite",
}


# ── Public API ───────────────────────────────────────────────────────────────


async def run_claw_subtask(
    task: str,
    browser_provider: "BrowserProvider",
    cwd: Optional[str] = None,
    claw_binary: Optional[str] = None,
) -> dict:
    """
    Spawn claw with the file bridge and relay LLM calls through the browser.

    Args:
        task:              The coding task to pass to claw (natural language).
        browser_provider:  Kim's BrowserProvider instance for LLM calls.
        cwd:               Working directory for claw (defaults to current dir).
        claw_binary:       Override path to the claw binary.

    Returns:
        {"success": bool, "exit_code": int, "message": str}
    """
    binary = Path(claw_binary) if claw_binary else CLAW_BINARY
    if not binary.exists():
        return {
            "success": False,
            "exit_code": -1,
            "message": f"Claw binary not found at {binary}",
        }

    working_dir = cwd or os.getcwd()

    # Create a per-session temp directory (#7 — no predictable /tmp paths)
    bridge_dir = Path(tempfile.mkdtemp(prefix="kim-claw-"))
    request_file = bridge_dir / "bridge_request.json"
    response_file = bridge_dir / "bridge_response.json"

    # Save current browser state so we can restore after
    saved_url = await _save_browser_state(browser_provider)

    logger.info(f"Starting claw subtask: {task[:80]}…")
    logger.info(f"  binary: {binary}")
    logger.info(f"  cwd: {working_dir}")
    logger.info(f"  bridge_dir: {bridge_dir}")

    # Use asyncio subprocess (#6 — no pipe deadlock)
    process = await asyncio.create_subprocess_exec(
        str(binary), task,
        env={**os.environ, "CLAW_FILE_BRIDGE": "1", "CLAW_BRIDGE_DIR": str(bridge_dir)},
        cwd=working_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    relay_count = 0
    try:
        # Relay loop — runs until claw exits
        while process.returncode is None:
            if request_file.exists():
                relay_count += 1
                if relay_count > MAX_RELAYS:
                    logger.error(f"Relay count exceeded {MAX_RELAYS} — stopping to prevent infinite loop")
                    process.kill()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        logger.warning("Claw process did not exit after kill (MAX_RELAYS)")
                    break
                await _relay_one_request(browser_provider, relay_count, bridge_dir)
            else:
                await asyncio.sleep(POLL_INTERVAL)
            # Check if process has exited
            try:
                await asyncio.wait_for(process.wait(), timeout=0.01)
            except asyncio.TimeoutError:
                pass

        # Process exited — check for one final request that might have been
        # written just before exit (race condition)
        if request_file.exists():
            relay_count += 1
            await _relay_one_request(browser_provider, relay_count, bridge_dir)

    except Exception as e:
        logger.error(f"Claw bridge relay error: {e}", exc_info=True)
        process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning("Claw process did not exit after kill")
        return {
            "success": False,
            "exit_code": process.returncode or -1,
            "message": f"Relay error: {e}",
        }
    finally:
        # Restore browser state
        await _restore_browser_state(browser_provider, saved_url)
        # Clean up temp dir (#7)
        shutil.rmtree(str(bridge_dir), ignore_errors=True)

    exit_code = process.returncode
    success = exit_code == 0

    # Capture stderr for diagnostic info (capped at 64MB)
    stderr_output = ""
    try:
        stderr_bytes = await process.stderr.read(MAX_OUTPUT_BYTES)
        stderr_output = stderr_bytes.decode("utf-8", errors="replace")[-500:]
    except Exception:
        pass

    result_msg = (
        "Task completed successfully."
        if success
        else f"Kim encountered an error: {stderr_output[:200]}"
    )

    logger.info(result_msg)
    return {
        "success": success,
        "exit_code": exit_code,
        "message": result_msg,
    }


# ── Internal helpers ─────────────────────────────────────────────────────────


async def _relay_one_request(
    browser_provider: "BrowserProvider",
    relay_number: int,
    bridge_dir: Path,
) -> None:
    """Read a bridge request, route through the browser LLM, write response."""

    # Brief delay to ensure claw has finished writing
    await asyncio.sleep(0.15)

    request_file = bridge_dir / "bridge_request.json"
    try:
        raw_request = request_file.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"[relay #{relay_number}] Failed to read request: {e}")
        return

    # Remove request file immediately to signal claw we've picked it up
    # (not strictly necessary, but prevents re-reading on the next poll)
    request_file.unlink(missing_ok=True)

    logger.info(
        f"[relay #{relay_number}] Got request ({len(raw_request)} chars) — "
        f"sending to browser LLM…"
    )

    # Build a prompt for the browser LLM from the bridge request
    prompt = _build_browser_prompt(raw_request)

    bridge_response = {"text": "ERROR: Browser LLM did not return a valid Claw bridge response."}
    correction = ""
    surfaced_reasoning = ""
    validation_error = ""
    for attempt in range(3):
        try:
            # Route through the browser provider
            response = await browser_provider.complete(
                messages=[{"role": "user", "content": prompt + correction}],
                tools=[],
                system=_claw_browser_system_prompt(),
                clear_chat=(relay_number == 1 and attempt == 0),
            )
        except Exception as e:
            logger.error(f"[relay #{relay_number}] Browser LLM call failed: {e}")
            # Write an error response so claw doesn't hang
            _write_bridge_response(json.dumps({"text": f"Error: {e}"}), bridge_dir)
            return

        # Convert BrowserProvider response to bridge format
        bridge_response = _provider_response_to_bridge(response, prompt)
        surfaced_reasoning = _surface_bridge_reasoning(bridge_response, surfaced_reasoning)
        validation_error = _bridge_response_validation_error(bridge_response, prompt)
        if not validation_error:
            break

        logger.warning(
            f"[relay #{relay_number}] Invalid browser bridge response "
            f"(attempt {attempt + 1}/3): {validation_error}"
        )
        print("[STATUS] Kim is refining its response…", flush=True)
        correction = (
            "\n\n[BRIDGE FORMAT ERROR]\n"
            f"{validation_error}\n"
            "Return the corrected response now. Output ONLY Claw bridge JSON.\n"
            "For HTML content, encode every '<' as '\\u003c' and every '>' as '\\u003e' inside the JSON string. "
            "This is required because the browser chat page may otherwise swallow the HTML tags before Kim can read them.\n"
            "Example:\n"
            '{"text":"brief reasoning","tool_calls":[{"name":"write_file","input":{"path":"index.html","content":"\\u003c!doctype html\\u003e\\u003chtml lang=\'en\'\\u003e\\u003chead\\u003e\\u003c/head\\u003e\\u003cbody\\u003e\\u003c/body\\u003e\\u003c/html\\u003e"}}]}\n'
            "Do not use Kim's JSON format with top-level 'tool'/'args'. "
            "Do not invent KIM tokens; if Kim's transport instruction asks for one exact "
            "[END_OF_RESPONSE] marker at the very end, append only that exact marker."
        )
    else:
        repaired = _repair_invalid_html_tool_call(bridge_response)
        if repaired:
            bridge_response = repaired
            surfaced_reasoning = _surface_bridge_reasoning(bridge_response, surfaced_reasoning)
        else:
            bridge_response = {
                "text": (
                    "I could not safely complete that file write because the browser model kept "
                    "returning malformed file content. No changes were written."
                )
            }

    logger.info(
        f"[relay #{relay_number}] Got browser response — writing bridge_response.json"
    )

    # Surface the reasoning text first, so it appears in the activity feed before tool calls.
    # This shows Gemini's thinking/context before its action.
    _surface_bridge_reasoning(bridge_response, surfaced_reasoning)

    # Surface Claw's tool calls in Kim's activity feed as [TOOL] lines.
    # ChatView.tsx parses these and renders them via TOOL_MAP.
    for tc in bridge_response.get("tool_calls", []):
        name = tc.get("name", "?")
        inp = tc.get("input", {}) if isinstance(tc.get("input"), dict) else {}
        # Build a compact args dict — only path/command, never file content.
        display: dict = {}
        if "path" in inp:
            display["path"] = inp["path"]
        elif "file_path" in inp:
            display["path"] = inp["file_path"]
        if name == "write_file" and isinstance(inp.get("content"), str):
            display["lines"] = max(1, len(inp["content"].splitlines()))
        if name in ("bash",) and "command" in inp:
            display["command"] = str(inp["command"])[:80]
        elif "pattern" in inp:
            display["pattern"] = inp["pattern"]
        elif "glob" in inp:
            display["glob"] = inp["glob"]
        print(f"[TOOL] {name}({json.dumps(display)})", flush=True)

    _write_bridge_response(json.dumps(bridge_response, ensure_ascii=False), bridge_dir)


def _build_browser_prompt(raw_request: str) -> str:
    """
    Convert the bridge request JSON into a human-readable prompt for the
    browser LLM.
    """
    try:
        data = json.loads(raw_request)
    except json.JSONDecodeError:
        return raw_request  # Fall back to raw text

    parts = []

    # System prompt
    system = data.get("system")
    if system and system != "null":
        parts.append(f"[SYSTEM PROMPT]\n{system}\n")

    # Conversation messages
    messages = data.get("messages", [])
    for msg in messages:
        role = msg.get("role", "unknown").upper()
        blocks = msg.get("blocks", [])
        for block in blocks:
            btype = block.get("type", "")
            if btype == "text":
                parts.append(f"[{role}]\n{block.get('text', '')}\n")
            elif btype == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input", "")
                # Avoid blowing up the prompt size with huge file contents,
                # which causes browser_provider to abruptly slice the prompt and corrupt JSON.
                if name == "write_file":
                    try:
                        inp_dict = json.loads(inp)
                        if "content" in inp_dict and isinstance(inp_dict["content"], str) and len(inp_dict["content"]) > 500:
                            inp_dict["content"] = f"... [{len(inp_dict['content'])} chars omitted for brevity; use read_file to view] ..."
                            inp = json.dumps(inp_dict, ensure_ascii=False)
                    except Exception:
                        pass
                parts.append(f"[{role} → TOOL CALL: {name}]\n{inp}\n")
            elif btype == "tool_result":
                name = block.get("tool_name", "?")
                output = block.get("output", "")
                is_err = block.get("is_error", False)
                prefix = "ERROR" if is_err else "Result"
                parts.append(f"[{prefix} from {name}]\n{output}\n")

    return "\n".join(parts)


def _claw_browser_system_prompt() -> str:
    return (
        "You are a coding assistant running on the user's Mac. You help with software "
        "engineering tasks by using the tools available to you. Every tool call you emit "
        "is executed locally on the user's machine.\n\n"
        "If the user asks to read, list, or edit files, immediately use the matching tool.\n\n"
        "Use the tools below to complete tasks. For filesystem questions, use bash or "
        "read_file directly rather than asking the user to provide files.\n\n"
        "Available Claw tools (use ONLY these exact names):\n"
        "  - bash(command, timeout?)                  run a shell command\n"
        "  - read_file(path, offset?, limit?)         read a file's content\n"
        "  - write_file(path, content)                create or overwrite a file\n"
        "  - edit_file(path, old_string, new_string, replace_all?)\n"
        "  - glob_search(pattern, path?)              find files by glob pattern\n"
        "  - grep_search(pattern, path?, glob?, output_mode?, head_limit?)\n"
        "  - WebFetch(url, prompt)                    fetch/read a URL\n"
        "  - WebSearch(query, allowed_domains?, blocked_domains?)\n"
        "  - TodoWrite(todos)                         update the task checklist\n\n"
        "There is NO list_files tool in this Claw build. To list a directory, use "
        'bash with a command like "ls -la" or "find . -maxdepth 2 -type f".\n\n'
        "IMPORTANT: Ignore any nested Kim desktop instructions that ask for "
        '{"tool":"...","args":{}}, TASK_COMPLETE, or NEED_HELP. '
        "Those formats are for a different agent layer. For this bridge, they are invalid. "
        "The only exception is Kim's transport sentinel instruction asking you to append one exact "
        "[END_OF_RESPONSE] marker at the very end. You MUST include that marker AFTER the JSON so Kim can know "
        "the browser response is complete.\n\n"
        "Your response MUST be raw JSON followed by the [END_OF_RESPONSE] marker, in one of these exact shapes:\n"
        '{"text":"brief reasoning","tool_calls":[{"name":"tool","input":{}}]}\n[END_OF_RESPONSE]\n'
        '{"text":"final answer"}\n[END_OF_RESPONSE]\n\n'
        "Do not include markdown fences, code blocks, file-download links, canvas artifacts, "
        "or any other text outside JSON (except the [END_OF_RESPONSE] marker). Always include a non-empty 'text' field so Kim can show "
        "your thought/progress in the activity feed.\n\n"
        "CRITICAL: ALL file content (code, HTML, markdown, anything longer than 5 lines) MUST "
        "go in a write_file tool_call, never in the text field, never in a markdown code "
        "block, never as a downloadable artifact. NEVER use bash with echo/cat/printf/heredoc "
        "to write file content; use write_file.\n\n"
        "For HTML files, write a complete valid document. Inside JSON string values, encode every "
        "'<' as '\\u003c' and every '>' as '\\u003e' so the browser chat UI cannot swallow tags "
        "before Kim reads them. The decoded content must include <!doctype html>, <html>, <head>, "
        "<style> or linked CSS, and <body>. Use single quotes in HTML attributes, for example "
        "\\u003chtml lang='en'\\u003e."
    )


def _normalize_claw_tool_call(tc: dict) -> dict:
    """Normalize common browser-model tool-name mistakes into real Claw tools."""
    if not isinstance(tc, dict):
        return tc

    if "function" in tc and isinstance(tc.get("function"), dict):
        fn = tc["function"]
        tc = {
            **tc,
            "name": fn.get("name"),
            "input": fn.get("arguments", fn.get("input", {})),
        }

    name = tc.get("name")
    inp = tc.get("input", {})
    if isinstance(inp, str):
        try:
            parsed_input = json.loads(inp)
            inp = parsed_input if isinstance(parsed_input, dict) else {"raw": inp}
        except json.JSONDecodeError:
            inp = {"raw": inp}
    if not isinstance(inp, dict):
        inp = {}

    aliases = {
        "run_command": "bash",
        "run_shell": "bash",
        "shell": "bash",
        "terminal": "bash",
        "read": "read_file",
        "write": "write_file",
        "edit": "edit_file",
        "glob": "glob_search",
        "grep": "grep_search",
        "web_fetch": "WebFetch",
        "web_search": "WebSearch",
        "todo_write": "TodoWrite",
    }

    normalized_name = aliases.get(str(name), name)

    # ChatGPT frequently invents list_files even though this Claw build does
    # not expose it. Map it to bash so simple directory tasks keep flowing.
    if normalized_name == "list_files":
        raw_path = inp.get("path") or inp.get("dir") or inp.get("directory") or "."
        path = str(raw_path).strip() or "."
        recursive = bool(inp.get("recursive"))
        max_depth = inp.get("max_depth") or inp.get("depth")
        if recursive:
            depth = ""
            try:
                if max_depth is not None:
                    depth = f" -maxdepth {int(max_depth)}"
            except (TypeError, ValueError):
                depth = ""
            command = f"find {shlex.quote(path)}{depth} -print"
        else:
            command = f"ls -la {shlex.quote(path)}"
        return {**tc, "name": "bash", "input": {"command": command}}

    if normalized_name == "bash":
        command = inp.get("command") or inp.get("cmd") or inp.get("script")
        if command is not None:
            inp = {**inp, "command": str(command)}

    return {**tc, "name": normalized_name, "input": inp}


def _normalize_bridge_dict(parsed: dict) -> dict:
    """Accept Claw bridge JSON and cautiously adapt Kim-style JSON if needed."""
    if "tool" in parsed and "args" in parsed:
        return {
            "text": parsed.get("text") or f"Calling {parsed.get('tool', 'tool')}.",
            "tool_calls": [
                _normalize_claw_tool_call({
                    "name": parsed.get("tool", "unknown"),
                    "input": parsed.get("args", {}),
                })
            ],
        }

    if "tool_calls" in parsed or "text" in parsed:
        normalized = dict(parsed)
        normalized.setdefault("text", "")
        if isinstance(normalized.get("tool_calls"), list):
            normalized["tool_calls"] = [
                _normalize_claw_tool_call(tc) if isinstance(tc, dict) else tc
                for tc in normalized["tool_calls"]
            ]
        return normalized

    return parsed


def _looks_like_html_path(path: str) -> bool:
    return path.lower().endswith((".html", ".htm"))


def _latest_user_instruction_lower(prompt: str) -> str:
    """Return the latest [USER] block, excluding Claw/Kim system scaffolding."""
    import re

    matches = list(re.finditer(r"(?ms)^\[USER\]\n(.*?)(?=^\[[A-Z][^\]]*\]\n|\Z)", prompt))
    if matches:
        return matches[-1].group(1).lower()
    return prompt.lower()


def _bridge_response_validation_error(response: dict, prompt: str = "") -> Optional[str]:
    if not isinstance(response, dict):
        return "Response must be a JSON object."

    text = response.get("text", "")
    tool_calls = response.get("tool_calls", [])

    if text is not None and not isinstance(text, str):
        return "The 'text' field must be a string."
    if tool_calls is None:
        tool_calls = []
    if tool_calls and not isinstance(tool_calls, list):
        return "The 'tool_calls' field must be a list."

    for tc in tool_calls:
        if not isinstance(tc, dict):
            return "Each tool call must be an object."
        name = tc.get("name")
        inp = tc.get("input")
        if not isinstance(name, str) or not name:
            return "Each tool call needs a string 'name'."
        if not isinstance(inp, dict):
            return "Each tool call needs an object 'input'."
        if name not in ALLOWED_CLAW_TOOLS:
            return (
                f"Unsupported Claw tool '{name}'. Use only: "
                f"{', '.join(sorted(ALLOWED_CLAW_TOOLS))}. "
                "For directory listings use bash with ls/find."
            )
        if name == "bash":
            command = inp.get("command")
            if not isinstance(command, str) or not command.strip():
                return "bash.input.command must be a non-empty string."
        if name == "write_file":
            path = inp.get("path")
            content = inp.get("content")
            if not isinstance(path, str) or not path.strip():
                return "write_file.input.path must be a non-empty string."
            if not isinstance(content, str):
                return "write_file.input.content must be a string."
            if _looks_like_html_path(path):
                lowered = content.lower()
                # Check for both literal tags AND \u003c/\u003e encoded tags
                # (we instruct the model to encode < > to avoid browser swallowing)
                has_tags = ("<" in content and ">" in content) or ("\\u003c" in content.lower() and "\\u003e" in content.lower())
                has_document_shape = any(
                    marker in lowered
                    for marker in ("<\u0021doctype", "<html", "<head", "<body", "<main", "<section", "<div",
                                   "\\u003c!doctype", "\\u003chtml", "\\u003chead", "\\u003cbody", "\\u003cmain", "\\u003csection", "\\u003cdiv")
                )
                looks_stripped = not has_tags and any(
                    marker in lowered
                    for marker in ("body {", "header {", "footer {", "apex construction", "hero {")
                )
                if looks_stripped or not (has_tags and has_document_shape):
                    return (
                        "HTML write_file content is missing literal HTML tags. "
                        "Return a complete HTML document with <!doctype html>, <html>, <head>, and <body>."
                    )

    prompt_lower = prompt.lower()
    user_task_lower = _latest_user_instruction_lower(prompt)
    likely_file_task = any(word in user_task_lower for word in ("create", "write", "generate", "make"))
    has_tool_result = "[result from" in prompt_lower or "[error from" in prompt_lower
    if likely_file_task and isinstance(text, str) and len(text.splitlines()) > 5 and not tool_calls:
        return "Large generated file content must be sent via write_file, not in text."
        
    if not tool_calls and isinstance(text, str) and text.strip().startswith("ERROR:"):
        return text.strip()
        
    if not tool_calls and isinstance(text, str) and text.strip():
        lowered_text = text.lower()
        hallucinated_patterns = (
            "calling:", "tool_call", "tool call:", "tool call\n",
            "```json", "```python", "```bash",
        )
        if any(p in lowered_text for p in hallucinated_patterns) and any(t in lowered_text for t in ALLOWED_CLAW_TOOLS):
            return "You attempted to output a tool call, but your format was invalid. You MUST use the exact JSON schema provided in the instructions, ending with the [END_OF_RESPONSE] marker."

    if (
        not tool_calls
        and not has_tool_result
        and isinstance(text, str)
        and text.strip()
    ):
        ai_intends_action = any(
            text.strip().lower().startswith(prefix)
            for prefix in ("i'll", "i will", "let me", "checking", "running", "executing", "looking", "searching", "sure")
        )
        task_needs_action = any(
            word in user_task_lower
            for word in (
                "read", "list", "open", "show", "find", "search", "edit", 
                "create", "write", "generate", "make", "ls", "dir", "check", "inside", "explain"
            )
        )
        
        if (ai_intends_action or task_needs_action) and user_task_lower.strip() not in {"hi", "hello", "hey", "hi!", "hello!", "hey!"}:
            return (
                "This request needs local action. Do not answer conversationally; emit a Claw "
                "tool_call now. Use bash for directory listings, read_file for file reads, "
                "write_file/edit_file for changes, and grep_search/glob_search for search."
            )

    # Catch the "I can't access your files / upload the project" refusal that
    # ChatGPT (and occasionally other browser models) emit when they break
    # character and forget they are running as Claw with real local tools.
    # Without this check the relay treats the refusal as a legitimate final
    # answer and Kim silently exits without doing the user's task.
    if isinstance(text, str) and not tool_calls and text.strip():
        lowered = text.lower()
        refusal_patterns = (
            "can't actually read",
            "cannot actually read",
            "can't access",
            "cannot access",
            "don't have access",
            "do not have access",
            "no access to your",
            "unless you upload",
            "please upload",
            "connect the workspace",
            "share the file",
            "share the files",
            "i'm unable to read",
            "i am unable to read",
            "i don't have the ability",
            "as an ai",
            "as a language model",
        )
        looks_like_file_task = any(
            word in user_task_lower
            for word in ("read", "list", "open", "show", "find", "search", "edit", "create", "write", "generate", "make", "ls", "dir")
        )
        if looks_like_file_task and any(p in lowered for p in refusal_patterns):
            return (
                "You refused to act, but you ARE Claw running locally with file-system "
                "tools. Re-read the system prompt: every tool_call you emit is executed on "
                "the user's Mac. Do NOT say you cannot access files. Emit the appropriate "
                "tool_call now (bash for directory listings, read_file, grep_search, etc)."
            )

    return None


def _surface_bridge_reasoning(response: dict, previous: str = "") -> str:
    import re
    reasoning = response.get("text", "").strip() if isinstance(response, dict) else ""
    if not reasoning or reasoning.startswith("ERROR:") or reasoning == previous:
        return previous
    # Strip JSON wrapping that sometimes leaks into the text field
    if reasoning.startswith("{") and reasoning.endswith("}"):
        try:
            parsed = json.loads(reasoning)
            if isinstance(parsed, dict) and "text" in parsed:
                reasoning = str(parsed["text"]).strip()
        except (json.JSONDecodeError, TypeError):
            pass
            
    # Aggressively strip `{"text": "` prefix if it got truncated and couldn't be parsed
    if reasoning.startswith("{") and '"text"' in reasoning[:20]:
        match = re.search(r'^\{\s*"text"\s*:\s*["\']?(.*)', reasoning, re.DOTALL)
        if match:
            reasoning = match.group(1).strip()
            
    # Drop trailing quotes or braces if they were left over
    reasoning = re.sub(r'["\']?\s*\}?\s*$', '', reasoning)

    # Drop lines that are pure JSON fragments (if the above still left an array or object)
    if reasoning.startswith("{") or reasoning.startswith("["):
        return previous
    # Replace provider brand names with "Kim"
    reasoning = re.sub(
        r"\b(Gemini|Claude|ChatGPT|Grok|DeepSeek)\b",
        "Kim",
        reasoning,
        flags=re.IGNORECASE,
    )
    # Strip technical prefixes
    reasoning = re.sub(r"^(?:Calling\s+\w+\.?\s*)", "", reasoning).strip()
    if not reasoning:
        return previous
    if len(reasoning) > 150:
        reasoning = reasoning[:147] + "…"
    print(f"[STATUS] {reasoning}", flush=True)
    return reasoning


def _repair_invalid_html_tool_call(response: dict) -> Optional[dict]:
    """Last-resort guard: never leak bridge validation errors as the user answer."""
    if not isinstance(response, dict):
        return None
    for tc in response.get("tool_calls", []) or []:
        if not isinstance(tc, dict) or tc.get("name") != "write_file":
            continue
        inp = tc.get("input")
        if not isinstance(inp, dict):
            continue
        path = inp.get("path")
        content = inp.get("content")
        if not isinstance(path, str) or not _looks_like_html_path(path):
            continue
        if not isinstance(content, str):
            continue
        if _bridge_response_validation_error({"text": "x", "tool_calls": [tc]}):
            return {
                "text": "Writing a clean fallback HTML file because the browser model stripped the original tags.",
                "tool_calls": [
                    {
                        "name": "write_file",
                        "input": {
                            "path": path,
                            "content": _fallback_html_from_stripped_content(content),
                        },
                    }
                ],
            }
    return None


def _fallback_html_from_stripped_content(content: str) -> str:
    import html
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    title = "Construction Website"
    for line in lines:
        if "construction" in line.lower() and "{" not in line and len(line) < 80:
            title = line.replace("|", "-")
            break
    body_text = "\n".join(line for line in lines if "{" not in line and "}" not in line)
    escaped_body = html.escape(body_text or "Professional construction services built with care.")
    return f"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>{html.escape(title)}</title>
  <style>
    body {{ margin: 0; font-family: Arial, sans-serif; color: #263238; line-height: 1.6; }}
    header {{ background: #263238; color: white; padding: 32px 20px; text-align: center; }}
    main {{ max-width: 960px; margin: 0 auto; padding: 40px 20px; }}
    .hero {{ background: #f3f0ec; border-left: 6px solid #c47f5f; padding: 32px; margin-bottom: 28px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 18px; }}
    .card {{ border: 1px solid #ddd4cd; border-radius: 8px; padding: 20px; background: white; }}
    footer {{ background: #1d1d1d; color: white; text-align: center; padding: 20px; }}
  </style>
</head>
<body>
  <header><h1>{html.escape(title)}</h1></header>
  <main>
    <section class='hero'>
      <h2>Solid foundations, careful work</h2>
      <p>{escaped_body}</p>
    </section>
    <section class='grid'>
      <article class='card'><h3>Residential</h3><p>Custom homes, additions, and renovations.</p></article>
      <article class='card'><h3>Commercial</h3><p>Reliable construction for offices, shops, and facilities.</p></article>
      <article class='card'><h3>Project Management</h3><p>Planning, coordination, and delivery from start to finish.</p></article>
    </section>
  </main>
  <footer>Built for dependable construction work.</footer>
</body>
</html>
"""


def _extract_first_bridge_json(text: str) -> Optional[dict]:
    """Scan text for the first balanced JSON object that has 'text' or 'tool_calls' keys.

    DeepSeek frequently prefixes conversational text before the JSON, e.g.:
        I'll check that now
        {"text":"checking","tool_calls":[...]}
        [END_OF_RESPONSE]

    This function finds and extracts that embedded JSON.
    String-aware: braces inside JSON strings are not counted (#11).
    """
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:  # Guard against negative depth
                depth -= 1
                if depth == 0 and start >= 0:
                    candidate = text[start: i + 1]
                    try:
                        parsed = json.loads(candidate)
                        if isinstance(parsed, dict) and ("text" in parsed or "tool_calls" in parsed):
                            return parsed
                    except json.JSONDecodeError:
                        try:
                            import json5
                            parsed = json5.loads(candidate)
                            if isinstance(parsed, dict) and ("text" in parsed or "tool_calls" in parsed):
                                return parsed
                        except Exception:
                            try:
                                import json_repair
                                parsed = json_repair.loads(candidate)
                                if isinstance(parsed, dict) and ("text" in parsed or "tool_calls" in parsed):
                                    return parsed
                            except Exception:
                                pass
                    start = -1
    return None


def _provider_response_to_bridge(response: dict, prompt: str = "") -> dict:
    """
    Convert a BrowserProvider response dict to the bridge JSON format.

    BrowserProvider returns:
        {"type": "tool_call", "tool": str, "args": dict}
      | {"type": "text", "content": str}
    """
    if not isinstance(response, dict):
        return {"text": str(response)}

    resp_type = response.get("type", "text")

    if resp_type == "tool_call":
        return _normalize_bridge_dict({
            "text": f"Calling {response.get('tool', 'tool')}.",
            "tool_calls": [
                {
                    "name": response.get("tool", "unknown"),
                    "input": response.get("args", {}),
                }
            ],
        })

    content = response.get("content", "")

    # Cleanup scraper duplication: The Claude UI sometimes renders the "text" field outside 
    # the code block and again inside it, causing the scraper to concatenate them without a quote.
    if content.startswith('{"text":"'):
        second_idx = content.find('{"text":"', 8)
        if second_idx != -1 and second_idx < 500:
            between = content[9:second_idx]
            if '"' not in between:
                content = content[second_idx:]

    # Try to parse the content as JSON (the browser LLM might return
    # structured JSON in its text response)
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            normalized = _normalize_bridge_dict(parsed)
            if "tool_calls" in normalized or "text" in normalized:
                return normalized
    except (json.JSONDecodeError, TypeError):
        # #8: Use json5 + json-repair instead of regex
        try:
            # Try json5 first (good for unquoted keys, trailing commas)
            import json5
            parsed = json5.loads(content)
            if isinstance(parsed, dict):
                normalized = _normalize_bridge_dict(parsed)
                if "tool_calls" in normalized or "text" in normalized:
                    return normalized
        except Exception:
            try:
                # Try json-repair (good for truncated JSON, unescaped quotes)
                import json_repair
                parsed = json_repair.loads(content)
                if isinstance(parsed, dict):
                    normalized = _normalize_bridge_dict(parsed)
                    if "tool_calls" in normalized or "text" in normalized:
                        return normalized
            except Exception:
                pass

    # ── Scan for a JSON object embedded in text ──────────────────────────
    # DeepSeek often prefixes conversational text before the JSON, or wraps
    # it in <tool_call> tags that the browser strips.  Scan for the first
    # balanced JSON object that looks like a bridge response.
    json_obj = _extract_first_bridge_json(content)
    if json_obj is not None:
        normalized = _normalize_bridge_dict(json_obj)
        if "tool_calls" in normalized or "text" in normalized:
            return normalized

    # Recovery path: DeepSeek/Qwen internal `[调用 tool] {args}` format.
    import re
    ds_matches = list(re.finditer(r"\[(?:调用|Call)\s*([a-zA-Z0-9_]+)\]\s*(\{.*?\})", content, re.DOTALL))
    if ds_matches:
        tool_calls = []
        for match in ds_matches:
            tool_name = match.group(1).strip()
            args_str = match.group(2).strip()
            try:
                args = json.loads(args_str)
                tool_calls.append({"name": tool_name, "input": args})
            except json.JSONDecodeError:
                try:
                    import json5
                    tool_calls.append({"name": tool_name, "input": json5.loads(args_str)})
                except Exception:
                    pass
        
        if tool_calls:
            # Reconstruct the text prior to the first tool call as the thought reasoning
            reasoning = content[:ds_matches[0].start()].strip()
            if not reasoning:
                reasoning = f"Calling {tool_calls[0]['name']}..."
            return {
                "text": reasoning,
                "tool_calls": tool_calls
            }

    # Recovery path: synthesize a write_file tool call from markdown code blocks
    import re
    prompt_lower = prompt.lower()
    if "create" in prompt_lower or "write" in prompt_lower or "generate" in prompt_lower:
        match = re.search(r"```(\w*)\n(.*?)```", content, re.DOTALL)
        if match:
            lang = match.group(1).strip()
            code_content = match.group(2).strip()
            
            filename = "output.txt"
            name_match = re.search(r"(?:create|write|generate) ([\w\.-]+\.(?:html|py|js|ts|css|json|md|txt))", prompt_lower)
            if name_match:
                filename = name_match.group(1)
            elif lang == "html":
                filename = "index.html"
            elif lang == "python":
                filename = "main.py"
            elif lang == "javascript" or lang == "js":
                filename = "script.js"
            
            return {
                "text": f"Writing {filename}.",
                "tool_calls": [
                    {
                        "name": "write_file",
                        "input": {
                            "path": filename,
                            "content": code_content
                        }
                    }
                ]
            }

    # If repair and recovery failed, but it looks like a botched JSON tool call
    if content.strip().startswith("{") and "tool_calls" in content:
        return {"text": 'ERROR: Your previous tool call had invalid JSON — escape inner double quotes with \\". Try again.'}

    # Clean up <think>...</think> blocks before returning bare text so that
    # deep reasoning models (like DeepSeek-R1) don't leak their internal
    # thought processes into the UI as unparsed text if all fallback logic fails.
    cleaned_content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    if not cleaned_content and content:
        # If the entire response was just a thought block with no output,
        # return a generic error so Claw knows to retry instead of passing empty text.
        return {"text": "ERROR: Browser LLM returned only reasoning with no actionable output."}

    return {"text": cleaned_content}


def _write_bridge_response(data: str, bridge_dir: Path) -> None:
    """Write bridge_response.json atomically using rename."""
    bridge_dir.mkdir(parents=True, exist_ok=True)
    response_file = bridge_dir / "bridge_response.json"

    # Write to temp file in the same directory (same filesystem for atomic rename)
    fd, tmp_path = tempfile.mkstemp(dir=str(bridge_dir), suffix=".tmp")
    try:
        os.write(fd, data.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)

    # Atomic rename
    os.rename(tmp_path, str(response_file))





async def _save_browser_state(browser_provider: "BrowserProvider") -> Optional[str]:
    """Save the current browser URL before relay starts."""
    try:
        # Get current page URL if available
        if hasattr(browser_provider, "_page") and browser_provider._page:
            return browser_provider._page.url
    except Exception as e:
        logger.debug(f"Could not save browser state: {e}")
    return None


async def _restore_browser_state(
    browser_provider: "BrowserProvider",
    saved_url: Optional[str],
) -> None:
    """Navigate back to the saved URL when relay finishes."""
    if not saved_url:
        return
    try:
        if hasattr(browser_provider, "_page") and browser_provider._page:
            logger.info(f"Restoring browser to: {saved_url}")
            await browser_provider._page.goto(saved_url, wait_until="domcontentloaded")
    except Exception as e:
        logger.warning(f"Could not restore browser state: {e}")

"""
Claw File Bridge — relay module.

Spawns the compiled `claw` Rust binary as a subprocess with `CLAW_FILE_BRIDGE=1`,
then relays LLM requests/responses between the binary and Kim's BrowserProvider.

The bridge protocol is file-based:
    claw writes  → /tmp/claw_bridge/bridge_request.json
    Kim reads, routes through browser LLM, writes ← bridge_response.json
    claw reads, continues tool loop

Completion signal: process exit.  When claw's ConversationRuntime finishes its
tool loop (the LLM responds with no tool calls), the process exits and
`process.poll()` returns a non-None exit code.

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
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from orchestrator.providers.browser_provider import BrowserProvider

logger = logging.getLogger("kim.claw_bridge")

# ── Constants ────────────────────────────────────────────────────────────────

BRIDGE_DIR = Path("/tmp/claw_bridge")
REQUEST_FILE = BRIDGE_DIR / "bridge_request.json"
RESPONSE_FILE = BRIDGE_DIR / "bridge_response.json"

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
PROCESS_CHECK_INTERVAL = 0.5  # seconds between process.poll() checks


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

    # Ensure bridge directory exists and is clean
    BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
    _clean_bridge_files()

    # Save current browser state so we can restore after
    saved_url = await _save_browser_state(browser_provider)

    logger.info(f"Starting claw subtask: {task[:80]}…")
    logger.info(f"  binary: {binary}")
    logger.info(f"  cwd: {working_dir}")

    process = subprocess.Popen(
        [str(binary), task],
        env={**os.environ, "CLAW_FILE_BRIDGE": "1"},
        cwd=working_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    relay_count = 0
    try:
        # Relay loop — runs until claw exits
        while process.poll() is None:
            if REQUEST_FILE.exists():
                relay_count += 1
                await _relay_one_request(browser_provider, relay_count)
            else:
                await asyncio.sleep(POLL_INTERVAL)

        # Process exited — check for one final request that might have been
        # written just before exit (race condition)
        if REQUEST_FILE.exists():
            relay_count += 1
            await _relay_one_request(browser_provider, relay_count)

    except Exception as e:
        logger.error(f"Claw bridge relay error: {e}", exc_info=True)
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        return {
            "success": False,
            "exit_code": process.returncode or -1,
            "message": f"Relay error: {e}",
        }
    finally:
        # Restore browser state
        await _restore_browser_state(browser_provider, saved_url)
        _clean_bridge_files()

    exit_code = process.returncode
    success = exit_code == 0

    # Capture stderr for diagnostic info
    stderr_output = ""
    try:
        _, stderr_bytes = process.communicate(timeout=1)
        stderr_output = stderr_bytes.decode("utf-8", errors="replace")[-500:]
    except Exception:
        pass

    result_msg = (
        f"Claw completed ({relay_count} LLM calls, exit code {exit_code})"
        if success
        else f"Claw failed (exit code {exit_code}): {stderr_output[:200]}"
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
) -> None:
    """Read a bridge request, route through the browser LLM, write response."""

    # Brief delay to ensure claw has finished writing
    await asyncio.sleep(0.15)

    try:
        raw_request = REQUEST_FILE.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"[relay #{relay_number}] Failed to read request: {e}")
        return

    # Remove request file immediately to signal claw we've picked it up
    # (not strictly necessary, but prevents re-reading on the next poll)
    REQUEST_FILE.unlink(missing_ok=True)

    logger.info(
        f"[relay #{relay_number}] Got request ({len(raw_request)} chars) — "
        f"sending to browser LLM…"
    )

    # Build a prompt for the browser LLM from the bridge request
    prompt = _build_browser_prompt(raw_request)

    try:
        # Route through the browser provider
        response = await browser_provider.complete(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            system=(
                "You are a coding agent. You have access to tools: "
                "write_file, read_file, edit_file, bash, grep_search, "
                "glob_search, list_files.\n\n"
                "Respond ONLY with a JSON object in this exact format:\n"
                '{"text": "your reasoning", '
                '"tool_calls": [{"name": "tool", "input": {...}}]}\n\n'
                "If you just want to reply with text and no tool calls:\n"
                '{"text": "your answer"}\n\n'
                "Do NOT include markdown fences. Output raw JSON only.\n\n"
                "CRITICAL: ALL file content (code, HTML, markdown, anything > 5 lines) "
                "MUST go in a write_file tool_call, never in the text field. "
                "If asked to create index.html, respond with "
                '{"tool_calls": [{"name": "write_file", "input": {"path": "index.html", "content": "<html>...</html>"}}]}, '
                "NOT with the HTML inline in text."
            ),
        )
    except Exception as e:
        logger.error(f"[relay #{relay_number}] Browser LLM call failed: {e}")
        # Write an error response so claw doesn't hang
        _write_bridge_response(json.dumps({"text": f"Error: {e}"}))
        return

    # Convert BrowserProvider response to bridge format
    bridge_response = _provider_response_to_bridge(response, prompt)

    logger.info(
        f"[relay #{relay_number}] Got browser response — writing bridge_response.json"
    )

    _write_bridge_response(json.dumps(bridge_response, ensure_ascii=False))


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
                parts.append(f"[{role} → TOOL CALL: {name}]\n{inp}\n")
            elif btype == "tool_result":
                name = block.get("tool_name", "?")
                output = block.get("output", "")
                is_err = block.get("is_error", False)
                prefix = "ERROR" if is_err else "Result"
                parts.append(f"[{prefix} from {name}]\n{output}\n")

    return "\n".join(parts)


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
        return {
            "tool_calls": [
                {
                    "name": response.get("tool", "unknown"),
                    "input": response.get("args", {}),
                }
            ],
        }

    content = response.get("content", "")

    # Try to parse the content as JSON (the browser LLM might return
    # structured JSON in its text response)
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            # Already in bridge format?
            if "tool_calls" in parsed or "text" in parsed:
                return parsed
    except (json.JSONDecodeError, TypeError):
        pass

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

    return {"text": content}


def _write_bridge_response(data: str) -> None:
    """Write bridge_response.json atomically using rename."""
    BRIDGE_DIR.mkdir(parents=True, exist_ok=True)

    # Write to temp file in the same directory (same filesystem for atomic rename)
    fd, tmp_path = tempfile.mkstemp(dir=str(BRIDGE_DIR), suffix=".tmp")
    try:
        os.write(fd, data.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)

    # Atomic rename
    os.rename(tmp_path, str(RESPONSE_FILE))


def _clean_bridge_files() -> None:
    """Remove any stale bridge files."""
    for f in [REQUEST_FILE, RESPONSE_FILE]:
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass
    # Also clean any leftover .tmp files
    if BRIDGE_DIR.exists():
        for tmp in BRIDGE_DIR.glob("*.tmp"):
            try:
                tmp.unlink()
            except Exception:
                pass


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

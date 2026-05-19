"""
Codex Bridge — relay module.

Spawns an OpenAI Codex CLI subprocess and routes its LLM calls through
Kim's BrowserProvider via a local HTTP proxy server.

Unlike the old file-bridge approach (polling JSON files on disk), the
Codex bridge runs a lightweight local HTTP server that speaks the OpenAI
Responses API format.  Codex is configured at launch to point its
`base_url` at this proxy, so all model traffic flows through Kim's
BrowserProvider transparently.

Architecture:
    1. Start a local aiohttp server on a random port (e.g. 127.0.0.1:PORT)
    2. Generate a temporary ~/.codex/config.toml pointing base_url at our proxy
    3. Spawn `codex <task>` as a subprocess
    4. Proxy intercepts /v1/responses, routes through BrowserProvider.complete()
    5. Returns OpenAI Responses-format JSON back to Codex
    6. When Codex exits, tear down the proxy

Usage:
    from mcp_server.tools.codex_bridge import run_codex_subtask

    result = await run_codex_subtask(
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
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from orchestrator.providers.browser_provider import BrowserProvider

logger = logging.getLogger("kim.codex_bridge")

# ── Constants ────────────────────────────────────────────────────────────────

# Where the codex binary lives — users can override via CODEX_BIN env var
CODEX_BINARY = "codex"  # Assumes it's on PATH (installed via npm or brew)

MAX_OUTPUT_BYTES = 64 * 1024 * 1024  # 64MB cap on captured output
MAX_RELAYS = 50  # cap relay attempts to prevent infinite loops

ALLOWED_CODEX_TOOLS = {
    "shell",
    "read_file",
    "write_file",
    "edit_file",
    "list_dir",
    "grep_search",
    "glob_search",
    "web_fetch",
    "web_search",
}

# ── Public API ───────────────────────────────────────────────────────────────


async def run_codex_subtask(
    task: str,
    browser_provider: "BrowserProvider",
    cwd: Optional[str] = None,
    codex_binary: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """
    Spawn Codex with a local proxy and relay LLM calls through the browser.

    Args:
        task:              The coding task to pass to Codex (natural language).
        browser_provider:  Kim's BrowserProvider instance for LLM calls.
        cwd:               Working directory for Codex (defaults to current dir).
        codex_binary:      Override path to the codex binary.
        model:             Model name to pass to Codex (optional).

    Returns:
        {"success": bool, "exit_code": int, "message": str}
    """
    binary = codex_binary or os.environ.get("CODEX_BIN", "").strip() or CODEX_BINARY

    # Verify the binary exists on PATH or at the specified path
    binary_path = shutil.which(binary) if not os.path.isabs(binary) else binary
    if not binary_path or not os.path.exists(binary_path):
        return {
            "success": False,
            "exit_code": -1,
            "message": f"Codex binary not found: {binary}. Install with: npm i -g @openai/codex",
        }

    working_dir = cwd or os.getcwd()

    logger.info(f"Starting Codex subtask: {task[:80]}…")
    logger.info(f"  binary: {binary_path}")
    logger.info(f"  cwd: {working_dir}")

    # Start the local proxy server — Codex's LLM calls go here instead of OpenAI.
    proxy = _CodexProxy(browser_provider)
    proxy_port = await proxy.start()

    logger.info(f"  proxy: http://127.0.0.1:{proxy_port}")

    # Write a temp Codex config pointing model_provider at our proxy.
    config_dir = Path(tempfile.mkdtemp(prefix="kim-codex-config-"))
    config_file = config_dir / "config.toml"
    _write_codex_config(config_file, proxy_port, model)

    process: Optional[asyncio.subprocess.Process] = None  # type: ignore[name-defined]
    try:
        env = {
            **os.environ,
            # Tell Codex where to find its config (our temp dir with proxy config).
            "CODEX_HOME": str(config_dir),
            # Dummy keys so Codex skips its account-auth flow.
            "CODEX_API_KEY": "kim-proxy-key",
            "OPENAI_API_KEY": "kim-proxy-key",
            # Also set base_url so even fallback paths hit our proxy.
            "OPENAI_BASE_URL": f"http://127.0.0.1:{proxy_port}/v1",
        }

        # Use the new `exec --json` subcommand — the old --full-auto --quiet
        # interface is not supported by @openai/codex v0.x.
        cmd = [
            str(binary_path),
            "exec", "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--oss",
            "-C", working_dir,
            task,
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Stream JSONL lines to stdout in real-time so Tauri picks them up.
        stderr_lines: list[str] = []

        async def _stream_stdout() -> None:
            assert process and process.stdout
            async for raw in process.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    print(line, flush=True)

        async def _drain_stderr() -> None:
            assert process and process.stderr
            async for raw in process.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    stderr_lines.append(line)
                    logger.debug("codex stderr: %s", line)

        try:
            await asyncio.wait_for(
                asyncio.gather(_stream_stdout(), _drain_stderr()),
                timeout=600,
            )
        except asyncio.TimeoutError:
            logger.error("Codex subprocess timed out after 600s")
            try:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=5)
            except Exception:
                pass
            return {
                "success": False,
                "exit_code": -1,
                "message": "Codex task timed out after 10 minutes.",
            }

        exit_code = await process.wait()
        success = exit_code == 0
        stderr_text = "\n".join(stderr_lines[-10:])  # last 10 lines for diagnostics

        result_msg = (
            "Task completed successfully."
            if success
            else f"Codex exited with code {exit_code}: {stderr_text[:300]}"
        )

    except Exception as e:
        logger.error(f"Codex bridge error: {e}", exc_info=True)
        return {
            "success": False,
            "exit_code": -1,
            "message": f"Codex bridge error: {e}",
        }
    finally:
        await proxy.stop()
        shutil.rmtree(str(config_dir), ignore_errors=True)

    logger.info(result_msg[:200])
    return {
        "success": success,
        "exit_code": exit_code,
        "message": result_msg,
        "final_answer": final_answer,
        "answer_emitted": bool(final_answer),
    }


# ── Codex config generation ─────────────────────────────────────────────────


def _write_codex_config(config_path: Path, proxy_port: int, model: Optional[str] = None) -> None:
    """Write a minimal Codex config.toml pointing to our local proxy."""
    model_line = f'model = "{model}"' if model else ""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(f"""\
# Auto-generated by Kim — routes Codex through Kim's browser proxy
model_provider = "kim-proxy"
{model_line}

[model_providers.kim-proxy]
name = "Kim Proxy"
base_url = "http://127.0.0.1:{proxy_port}/v1"
wire_api = "responses"
env_key = "CODEX_API_KEY"
""", encoding="utf-8")
    logger.info(f"Wrote Codex config: {config_path}")


# ── Local HTTP Proxy Server ──────────────────────────────────────────────────


class _CodexProxy:
    """Minimal HTTP server that translates Codex Responses API calls
    into BrowserProvider.complete() calls."""

    def __init__(self, browser_provider: "BrowserProvider"):
        self._provider = browser_provider
        self._server = None
        self._runner = None
        self._port = 0
        self._relay_count = 0

    async def start(self) -> int:
        """Start the proxy server and return the port it's listening on."""
        try:
            from aiohttp import web
        except ImportError:
            raise RuntimeError(
                "aiohttp is required for the Codex bridge proxy. "
                "Install with: pip install aiohttp"
            )

        app = web.Application()
        app.router.add_post("/v1/responses", self._handle_responses)
        # Catch-all health check
        app.router.add_get("/v1/models", self._handle_models)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        # Extract the actual port
        self._port = site._server.sockets[0].getsockname()[1]
        logger.info(f"Codex proxy started on port {self._port}")
        return self._port

    async def stop(self):
        """Stop the proxy server."""
        if self._runner:
            try:
                await self._runner.cleanup()
            except Exception as e:
                logger.debug(f"Proxy cleanup error: {e}")

    async def _handle_models(self, request):
        """Return a dummy models list so Codex doesn't error."""
        from aiohttp import web
        return web.json_response({
            "object": "list",
            "data": [
                {
                    "id": "kim-proxy-model",
                    "object": "model",
                    "created": 0,
                    "owned_by": "kim",
                }
            ],
        })

    async def _handle_responses(self, request):
        """Handle POST /v1/responses — the Codex Responses API endpoint."""
        from aiohttp import web

        self._relay_count += 1
        relay_num = self._relay_count

        if relay_num > MAX_RELAYS:
            logger.error(f"Relay count exceeded {MAX_RELAYS}")
            return web.json_response(
                {"error": {"message": "Too many relay attempts"}},
                status=429,
            )

        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"error": {"message": "Invalid JSON body"}},
                status=400,
            )

        logger.info(f"[relay #{relay_num}] Codex request received")

        # Extract the prompt from Codex's Responses API request
        prompt = _extract_prompt_from_responses_request(body)

        try:
            # Route through browser provider
            response = await self._provider.complete(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                system=_codex_browser_system_prompt(),
                clear_chat=True,
            )
        except Exception as e:
            logger.error(f"[relay #{relay_num}] Browser LLM call failed: {e}")
            return web.json_response(
                {"error": {"message": f"LLM call failed: {e}"}},
                status=502,
            )

        # Convert BrowserProvider response to Responses API format
        responses_reply = _provider_response_to_responses_api(response, relay_num)

        # Surface reasoning in Kim's activity feed
        _surface_relay_reasoning(response, relay_num)

        return web.json_response(responses_reply)


# ── Request/Response translation ─────────────────────────────────────────────


def _extract_prompt_from_responses_request(body: dict) -> str:
    """Extract a human-readable prompt from a Codex Responses API request."""
    parts = []

    # System instructions
    instructions = body.get("instructions")
    if instructions:
        parts.append(f"[SYSTEM PROMPT]\n{instructions}\n")

    # Input messages
    input_items = body.get("input")
    if isinstance(input_items, str):
        parts.append(f"[USER]\n{input_items}\n")
    elif isinstance(input_items, list):
        for item in input_items:
            if isinstance(item, str):
                parts.append(f"[USER]\n{item}\n")
            elif isinstance(item, dict):
                role = item.get("role", "user").upper()
                content = item.get("content", "")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            btype = block.get("type", "")
                            if btype in ("input_text", "text"):
                                parts.append(f"[{role}]\n{block.get('text', '')}\n")
                            elif btype == "output_text":
                                parts.append(f"[ASSISTANT]\n{block.get('text', '')}\n")
                elif isinstance(content, str):
                    parts.append(f"[{role}]\n{content}\n")

    return "\n".join(parts) if parts else str(body)


def _codex_browser_system_prompt() -> str:
    return (
        "You are a coding assistant. The conversation below contains a [SYSTEM PROMPT] "
        "section from Codex that defines the available tools — use those exact tool names.\n\n"
        "CRITICAL: Your entire response MUST be raw JSON only. No markdown, no prose, "
        "no code fences. Use exactly one of these two shapes:\n\n"
        '  Tool call:    {"text": "brief reasoning", "tool_calls": [{"name": "TOOL_NAME", "input": {...}}]}\n'
        '  Final answer: {"text": "your answer"}\n\n'
        "Rules:\n"
        "- Use ONLY the tool names defined in the [SYSTEM PROMPT] from Codex.\n"
        "- For a tool turn: include tool_calls with at least one entry.\n"
        "- For a final answer: omit tool_calls entirely.\n"
        "- File content always goes in a tool call — never embed it in the text field.\n"
        "- Do NOT output anything outside the JSON object.\n"
    )


def _provider_response_to_responses_api(response: dict, relay_num: int) -> dict:
    """Convert a BrowserProvider response to OpenAI Responses API format."""
    resp_id = f"resp_{uuid.uuid4().hex[:16]}"

    if not isinstance(response, dict):
        return _make_responses_text_reply(resp_id, str(response))

    content = response.get("content", "")
    if isinstance(content, str):
        # Try to parse as bridge JSON
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                text = parsed.get("text", "")
                tool_calls = parsed.get("tool_calls", [])
                if tool_calls:
                    return _make_responses_tool_reply(resp_id, text, tool_calls)
                return _make_responses_text_reply(resp_id, text or content)
        except (json.JSONDecodeError, TypeError):
            pass
        return _make_responses_text_reply(resp_id, content)

    return _make_responses_text_reply(resp_id, str(content))


def _make_responses_text_reply(resp_id: str, text: str) -> dict:
    """Build a Responses API reply with just text output."""
    return {
        "id": resp_id,
        "object": "response",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": text}
                ],
            }
        ],
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        },
    }


def _make_responses_tool_reply(resp_id: str, text: str, tool_calls: list) -> dict:
    """Build a Responses API reply with tool calls."""
    output_items = []

    # Add reasoning text if present
    if text:
        output_items.append({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        })

    # Add tool calls
    for tc in tool_calls:
        if isinstance(tc, dict):
            output_items.append({
                "type": "function_call",
                "name": tc.get("name", "unknown"),
                "arguments": json.dumps(tc.get("input", {})),
                "call_id": f"call_{uuid.uuid4().hex[:12]}",
            })

    return {
        "id": resp_id,
        "object": "response",
        "status": "completed",
        "output": output_items,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        },
    }


# ── Output parsing & surfacing ───────────────────────────────────────────────


def _surface_codex_output(stdout_text: str) -> None:
    """Parse Codex's stdout and surface tool calls in Kim's activity feed."""
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Surface tool execution lines
        if "Running:" in line or "Executing:" in line:
            print(f"[TOOL] {line}", flush=True)
        elif line.startswith("✓") or line.startswith("✗"):
            print(f"[STATUS] {line}", flush=True)


def _extract_final_answer(stdout_text: str) -> Optional[str]:
    """Extract the final conversational answer from Codex output."""
    lines = stdout_text.strip().splitlines()
    # The final answer is typically the last non-empty lines of output
    answer_lines = []
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            if answer_lines:
                break
            continue
        # Skip tool execution lines
        if any(marker in stripped for marker in ("Running:", "Executing:", "✓", "✗", ">>>", "---")):
            break
        answer_lines.append(stripped)

    if answer_lines:
        answer_lines.reverse()
        return "\n".join(answer_lines)
    return None


def _surface_relay_reasoning(response: dict, relay_num: int) -> None:
    """Surface non-final reasoning from the browser LLM in Kim's activity feed."""
    if not isinstance(response, dict):
        return
    content = response.get("content", "")
    if not isinstance(content, str) or not content.strip():
        return
    # Clean up provider brand names
    display = re.sub(
        r"\b(Gemini|Claude|ChatGPT|Grok|DeepSeek)\b",
        "Kim",
        content[:150],
        flags=re.IGNORECASE,
    )
    if display and not display.startswith("{"):
        print(f"[STATUS] {display}", flush=True)


def _emit_bridge_answer(answer: str) -> None:
    """Emit a full final answer for the React UI as a single parseable line."""
    cleaned = answer.strip()
    if cleaned:
        print(f"[ANSWER] {json.dumps(cleaned, ensure_ascii=False)}", flush=True)

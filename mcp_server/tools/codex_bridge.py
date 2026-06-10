"""
Codex Bridge — relay module.

NOTE: The Tauri subprocess spawn now uses orchestrator/codex_bridge_service.py
(Phase 7). This module remains the canonical home for _CodexProxy and
run_codex_subtask, which are imported by codex_bridge_service. It will be
deleted once the new service has been verified end-to-end in production.

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

Auto-compaction (claw-style two-pass):
    When estimated token count exceeds the provider threshold, the proxy
    summarizes older messages via the browser LLM (first pass), then applies
    priority-based line selection to keep the summary under a character budget
    (second pass — adapted from claw's summary_compression.rs).

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

CODEX_BINARY = "codex"

MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_RELAYS = 50

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

# Compaction constants (adapted from claw compact.rs)
COMPACT_KEEP_ITEMS = 20      # Keep last N items uncompressed
COMPACT_MIN_ITEMS_TO_REMOVE = 5  # Don't compact if fewer than this many items would be removed

# Per-provider context thresholds (tokens) before compaction triggers
_COMPACT_THRESHOLDS: dict[str, int] = {
    "claude": 180_000,
    "chatgpt": 100_000,
    "gemini": 800_000,
    "grok": 100_000,
    "deepseek": 60_000,
}
_DEFAULT_COMPACT_THRESHOLD = 100_000


# ── Public API ───────────────────────────────────────────────────────────────


async def run_codex_subtask(
    task: str,
    browser_provider: "BrowserProvider",
    cwd: Optional[str] = None,
    codex_binary: Optional[str] = None,
    model: Optional[str] = None,
    provider_name: Optional[str] = None,
) -> dict:
    """
    Spawn Codex with a local proxy and relay LLM calls through the browser.

    Args:
        task:              The coding task to pass to Codex (natural language).
        browser_provider:  Kim's BrowserProvider instance for LLM calls.
        cwd:               Working directory for Codex (defaults to current dir).
        codex_binary:      Override path to the codex binary.
        model:             Model name to pass to Codex (optional).
        provider_name:     Provider identifier string (e.g. 'browser:gemini') for
                           selecting the correct compaction threshold.

    Returns:
        {"success": bool, "exit_code": int, "message": str}
    """
    binary = codex_binary or os.environ.get("CODEX_BIN", "").strip() or CODEX_BINARY

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

    # Reset so system prompt is injected fresh at the start of each Codex session.
    if hasattr(browser_provider, '_sent_system_prompt'):
        browser_provider._sent_system_prompt = False

    proxy = _CodexProxy(browser_provider, provider_name=provider_name or "")
    proxy_port = await proxy.start()

    logger.info(f"  proxy: http://127.0.0.1:{proxy_port}")

    config_dir = Path(tempfile.mkdtemp(prefix="kim-codex-config-"))
    config_file = config_dir / "config.toml"
    _write_codex_config(config_file, proxy_port, model)

    process: Optional[asyncio.subprocess.Process] = None  # type: ignore[name-defined]
    try:
        env = {
            **os.environ,
            "CODEX_HOME": str(config_dir),
            "CODEX_API_KEY": "kim-proxy-key",
            "OPENAI_API_KEY": "kim-proxy-key",
            "OPENAI_BASE_URL": f"http://127.0.0.1:{proxy_port}/v1",
        }

        cmd = [
            str(binary_path),
            "exec", "--json",
            "--dangerously-bypass-approvals-and-sandbox",
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

        stderr_lines: list[str] = []

        async def _stream_stdout() -> None:
            assert process and process.stdout
            async for raw in process.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    print(line, flush=True)

        async def _drain_stderr() -> None:
            assert process and process.stderr
            await _drain_stderr_to(process.stderr, stderr_lines)

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
        stderr_text = "\n".join(stderr_lines[-10:])

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
        # Kill the subprocess on every non-normal exit (exception, cancellation,
        # BrokenPipeError from stdout, etc.).  The timeout path already kills
        # explicitly; this guard covers all other early-exit paths so Codex
        # never becomes an orphan process.
        if process is not None and process.returncode is None:
            try:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=5)
            except Exception:
                pass
        await proxy.stop()
        shutil.rmtree(str(config_dir), ignore_errors=True)

    logger.info(result_msg[:200])
    return {
        "success": success,
        "exit_code": exit_code,
        "message": result_msg,
    }


# ── Codex config generation ─────────────────────────────────────────────────


def _write_codex_config(config_path: Path, proxy_port: int, model: Optional[str] = None) -> None:
    """Write a minimal Codex config.toml pointing to our local proxy."""
    model_line = f'model = "{model}"' if model else 'model = "kim-proxy-model"'
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


# ── Compaction helpers ───────────────────────────────────────────────────────


def _estimate_tokens(items: list) -> int:
    """Rough token estimate: ~4 chars per token."""
    total = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        for field in ("content", "output", "arguments", "text"):
            val = item.get(field, "")
            if isinstance(val, str):
                total += len(val) // 4
            elif isinstance(val, list):
                for block in val:
                    if isinstance(block, dict):
                        total += len(block.get("text", "")) // 4
    return total


def _get_compact_threshold(provider_name: str) -> int:
    name = provider_name.lower()
    for key, threshold in _COMPACT_THRESHOLDS.items():
        if key in name:
            return threshold
    return _DEFAULT_COMPACT_THRESHOLD


def _is_compaction_summary(item: object) -> bool:
    """Return True if this item is a Kim compaction summary block."""
    if not isinstance(item, dict):
        return False
    content = item.get("content", "")
    if isinstance(content, str):
        return "[CONTEXT SUMMARY" in content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and "[CONTEXT SUMMARY" in block.get("text", ""):
                return True
    return False


def _fix_tool_boundary(items: list, keep_from: int) -> int:
    """Walk keep_from forward past any leading function_call_output items
    to avoid orphaning a tool result without its paired tool call."""
    idx = keep_from
    while idx < len(items):
        item = items[idx]
        if isinstance(item, dict) and item.get("type") == "function_call_output":
            idx += 1
        else:
            break
    return idx


def _compress_summary(summary_text: str, max_chars: int = 1200, max_lines: int = 24, max_line_chars: int = 160) -> str:
    """Second-pass compression (adapted from claw summary_compression.rs).

    Deduplicates, truncates long lines, and selects within a char/line budget
    using a priority ordering that keeps core fields over noise.
    """
    inner = re.sub(r"</?summary>", "", summary_text).strip()

    seen: set[str] = set()
    lines: list[str] = []
    for raw_line in inner.splitlines():
        normalized = " ".join(raw_line.split())[:max_line_chars]
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        lines.append(normalized)

    def _priority(line: str) -> int:
        lower = line.lower()
        if any(lower.startswith(k) for k in (
            "scope:", "current work:", "tools used:", "key files:",
            "pending:", "recent user requests:", "key decisions:",
        )):
            return 0
        if any(lower.startswith(k) for k in (
            "timeline:", "previously compacted:", "newly compacted:",
        )):
            return 1
        if line.startswith(("- ", "• ", "* ", "  -")):
            return 2
        return 3

    lines.sort(key=_priority)

    selected: list[str] = []
    total_chars = 0
    for line in lines:
        if len(selected) >= max_lines:
            break
        if total_chars + len(line) + 1 > max_chars:
            break
        selected.append(line)
        total_chars += len(line) + 1

    omitted = len(lines) - len(selected)
    body = "\n".join(selected)
    if omitted > 0:
        body += f"\n[{omitted} additional context lines omitted]"

    return f"<summary>\n{body}\n</summary>"


def _merge_compact_summaries(existing: str, new_summary: str) -> str:
    """Merge existing and new compaction summaries (claw merge_compact_summaries pattern)."""
    existing_inner = re.sub(r"</?summary>", "", existing).strip()
    new_inner = re.sub(r"</?summary>", "", new_summary).strip()
    return (
        f"<summary>\nPreviously compacted context:\n{existing_inner}\n\n"
        f"Newly compacted context:\n{new_inner}\n</summary>"
    )


async def _summarize_messages(items: list, provider: "BrowserProvider") -> str:
    """Call the browser LLM to produce a <summary> XML block for the given items."""
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role = item.get("role", "")
        itype = item.get("type", "")
        content = item.get("content", "")

        if itype == "function_call":
            name = item.get("name", "unknown")
            args = str(item.get("arguments", ""))[:400]
            parts.append(f"[TOOL CALL: {name}]\n{args}")
        elif itype == "function_call_output":
            output = item.get("output", "")
            if isinstance(output, list):
                output = " ".join(str(o) for o in output)
            parts.append(f"[TOOL RESULT]\n{str(output)[:400]}")
        elif role == "user":
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") in ("input_text", "text"):
                        parts.append(f"[USER]\n{block.get('text', '')[:800]}")
            elif isinstance(content, str) and not _is_compaction_summary(item):
                parts.append(f"[USER]\n{content[:800]}")
        elif role == "assistant":
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                        parts.append(f"[ASSISTANT]\n{block.get('text', '')[:800]}")
            elif isinstance(content, str):
                parts.append(f"[ASSISTANT]\n{content[:800]}")

    transcript = "\n\n".join(parts)

    prompt = (
        "Summarize the following coding agent conversation for context compaction. "
        "Output ONLY this XML block:\n\n"
        "<summary>\n"
        "Scope: [N tool calls, M user turns]\n"
        "Tools used: [comma-separated tool names]\n"
        "Recent user requests: [brief list]\n"
        "Key files touched: [files created/modified/read]\n"
        "Current work: [what was in progress at the end]\n"
        "Key decisions: [important findings or decisions]\n"
        "Timeline: [brief chronological summary]\n"
        "</summary>\n\n"
        f"TRANSCRIPT (truncated to 8000 chars):\n{transcript[:8000]}"
    )

    try:
        response = await provider.complete(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            system="You are a precise context summarizer. Output only the requested <summary>...</summary> XML block. No other text.",
            clear_chat=True,
        )
        raw = response.get("content", "") if isinstance(response, dict) else str(response)
        match = re.search(r"<summary>(.*?)</summary>", raw, re.DOTALL)
        if match:
            return f"<summary>{match.group(1)}</summary>"
        return f"<summary>{raw[:1000]}</summary>"
    except Exception as exc:
        logger.warning(f"Summarization LLM call failed: {exc}")
        return f"<summary>Context compacted. {len(items)} messages summarized.</summary>"


# ── Local HTTP Proxy Server ──────────────────────────────────────────────────


class _CodexProxy:
    """Minimal HTTP server that translates Codex Responses API calls
    into BrowserProvider.complete() calls, with auto-compaction."""

    def __init__(self, browser_provider: "BrowserProvider", provider_name: str = ""):
        self._provider = browser_provider
        self._provider_name = provider_name
        self._server = None
        self._runner = None
        self._port = 0
        self._relay_count = 0
        self._last_sent_count = 0  # how many input_items were forwarded to the browser last relay
        self._last_proxy_response: Optional[dict] = None  # last Responses API reply sent to Codex
        # Cache: hash(json(prefix_items)) → summary_item dict
        # Avoids re-summarizing the same prefix on every Codex turn.
        self._compaction_cache: dict[int, dict] = {}

    async def start(self) -> int:
        try:
            from aiohttp import web
        except ImportError:
            raise RuntimeError(
                "aiohttp is required for the Codex bridge proxy. "
                "Install with: pip install aiohttp"
            )

        app = web.Application()
        app.router.add_post("/v1/responses", self._handle_responses)
        app.router.add_post("/v1/chat/completions", self._handle_chat_completions)
        app.router.add_get("/v1/models", self._handle_models)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self._port = site._server.sockets[0].getsockname()[1]
        logger.info(f"Codex proxy started on port {self._port}")
        return self._port

    async def stop(self):
        if self._runner:
            try:
                await self._runner.cleanup()
            except Exception as e:
                logger.debug(f"Proxy cleanup error: {e}")

    async def _handle_models(self, request):
        from aiohttp import web
        return web.json_response({
            "object": "list",
            "data": [{"id": "kim-proxy-model", "object": "model", "created": 0, "owned_by": "kim"}],
        })

    async def _handle_responses(self, request):
        """Handle POST /v1/responses — the Codex Responses API endpoint."""
        from aiohttp import web

        self._relay_count += 1
        relay_num = self._relay_count

        if relay_num > MAX_RELAYS:
            logger.error(f"Relay count exceeded {MAX_RELAYS}")
            return web.json_response(
                {"error": {"message": "Too many relay attempts"}}, status=429,
            )

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": {"message": "Invalid JSON body"}}, status=400)

        stream = bool(body.get("stream", False))
        logger.info(f"[relay #{relay_num}] Codex request received")

        # ── Auto-compaction ──────────────────────────────────────────────────
        input_items = body.get("input", [])
        compacted = False
        if isinstance(input_items, list) and input_items:
            threshold = _get_compact_threshold(self._provider_name)
            estimated = _estimate_tokens(input_items)
            logger.debug(f"[relay #{relay_num}] ~{estimated} estimated tokens (threshold {threshold})")

            if estimated > threshold:
                input_items = await self._apply_compaction(input_items, relay_num)
                body = {**body, "input": input_items}
                compacted = True
        # ────────────────────────────────────────────────────────────────────

        is_first_relay = self._last_sent_count == 0 or compacted

        if is_first_relay:
            # First relay or post-compaction: send full context and start a fresh browser chat.
            prompt = _extract_prompt_from_responses_request(body)
            clear_chat = True
            # Ensure system prompt is re-injected when the new browser chat opens.
            if hasattr(self._provider, '_sent_system_prompt'):
                self._provider._sent_system_prompt = False
            self._last_sent_count = len(input_items) if isinstance(input_items, list) else 0
            logger.info(f"[relay #{relay_num}] First relay — sending full context ({self._last_sent_count} items)")
        else:
            # Subsequent relay: send only new user-side items since the last relay.
            delta_items = input_items[self._last_sent_count:] if isinstance(input_items, list) else []

            # If Codex is only sending "Continue." keepalive messages (no tool results),
            # return the cached last response directly — no need to call the browser again.
            # This breaks the Continue. loop that occurs when Codex keeps asking for more
            # after a plain-text (non-tool-call) answer.
            if delta_items and _is_continue_only_delta(delta_items) and self._last_proxy_response is not None:
                logger.info(f"[relay #{relay_num}] Continue.-only delta — returning cached response")
                return _sse_or_json(stream, self._last_proxy_response)

            if delta_items:
                prompt = _extract_delta_prompt(delta_items)
                self._last_sent_count = len(input_items)
                logger.info(f"[relay #{relay_num}] Delta relay — {len(delta_items)} new items")
            else:
                # No new items at all — return cached response or terminal stub
                if self._last_proxy_response is not None:
                    logger.info(f"[relay #{relay_num}] Empty delta — returning cached response")
                    return _sse_or_json(stream, self._last_proxy_response)
                prompt = "Continue."
                logger.info(f"[relay #{relay_num}] Empty delta (no cache) — sending 'Continue.'")
            clear_chat = False

        try:
            response = await self._provider.complete(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                system=_codex_browser_system_prompt(),
                clear_chat=clear_chat,
            )
        except Exception as e:
            logger.error(f"[relay #{relay_num}] Browser LLM call failed: {e}")
            return web.json_response(
                {"error": {"message": f"LLM call failed: {e}"}}, status=502,
            )

        responses_reply = _provider_response_to_responses_api(response, relay_num)
        self._last_proxy_response = responses_reply
        _surface_relay_reasoning(response, relay_num)

        return _sse_or_json(stream, responses_reply)

    async def _handle_chat_completions(self, request):
        """Handle POST /v1/chat/completions — standard OpenAI chat format."""
        from aiohttp import web

        self._relay_count += 1
        relay_num = self._relay_count

        if relay_num > MAX_RELAYS:
            return web.json_response({"error": {"message": "Too many relay attempts"}}, status=429)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": {"message": "Invalid JSON body"}}, status=400)

        logger.info(f"[relay #{relay_num}] Chat completions request received")

        # ── Auto-compaction ──────────────────────────────────────────────────
        messages = body.get("messages", [])
        if isinstance(messages, list) and messages:
            threshold = _get_compact_threshold(self._provider_name)
            estimated = _estimate_tokens(messages)
            logger.debug(f"[relay #{relay_num}] ~{estimated} estimated tokens (threshold {threshold})")
            if estimated > threshold:
                messages = await self._apply_compaction_chat(messages, relay_num)
                body = {**body, "messages": messages}
        # ────────────────────────────────────────────────────────────────────

        prompt = _extract_prompt_from_chat_request(body)

        try:
            response = await self._provider.complete(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                system=_codex_browser_system_prompt(),
                clear_chat=True,
            )
        except Exception as e:
            logger.error(f"[relay #{relay_num}] Browser LLM call failed: {e}")
            return web.json_response({"error": {"message": f"LLM call failed: {e}"}}, status=502)

        reply = _provider_response_to_chat_completions(response, relay_num)
        _surface_relay_reasoning(response, relay_num)

        return web.json_response(reply)

    async def _apply_compaction_chat(self, messages: list, relay_num: int) -> list:
        """Compaction for chat completions format (role/content messages)."""
        existing_summary: Optional[str] = None
        summary_start = 0

        # Check if first non-system message is a compaction summary
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            if msg.get("role") == "system":
                continue
            content = msg.get("content", "")
            if isinstance(content, str) and "[CONTEXT SUMMARY" in content:
                existing_summary = content
                summary_start = i + 1
            break

        system_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "system"]
        non_system = messages[summary_start:] if summary_start else [
            m for m in messages if not (isinstance(m, dict) and m.get("role") == "system")
        ]

        keep_from_raw = max(0, len(non_system) - COMPACT_KEEP_ITEMS)
        if keep_from_raw < COMPACT_MIN_ITEMS_TO_REMOVE:
            return messages

        to_summarize = non_system[:keep_from_raw]
        to_keep = non_system[keep_from_raw:]

        try:
            prefix_key = hash(json.dumps(to_summarize, default=str, sort_keys=True))
        except Exception:
            prefix_key = hash(str(to_summarize))

        if prefix_key in self._compaction_cache:
            logger.info(f"[relay #{relay_num}] [compaction] Cache hit — reusing summary")
            cached = self._compaction_cache[prefix_key]
            return system_msgs + [cached] + list(to_keep)

        logger.info(f"[relay #{relay_num}] [compaction] Summarizing {len(to_summarize)} messages, keeping {len(to_keep)}")
        print(f"[STATUS] Compacting context — summarizing {len(to_summarize)} messages…", flush=True)

        # Convert chat messages to codex-bridge item format for the summarizer
        items_for_summary = [
            {"role": m.get("role", "user"), "content": m.get("content", "")}
            for m in to_summarize if isinstance(m, dict)
        ]
        new_summary = await _summarize_messages(items_for_summary, self._provider)

        if existing_summary:
            merged = _merge_compact_summaries(existing_summary, new_summary)
        else:
            merged = new_summary

        compressed = _compress_summary(merged)

        summary_msg = {
            "role": "user",
            "content": f"[CONTEXT SUMMARY — previous conversation compacted]\n{compressed}",
        }

        self._compaction_cache[prefix_key] = summary_msg
        print(f"[STATUS] Context compacted — summary is {len(compressed)} chars", flush=True)

        return system_msgs + [summary_msg] + list(to_keep)

    async def _apply_compaction(self, items: list, relay_num: int) -> list:
        """Run claw-style two-pass compaction, with prefix-hash caching."""
        # Separate existing summary (if any) from the rest
        existing_summary: Optional[str] = None
        summary_start = 0
        if _is_compaction_summary(items[0]):
            first_content = items[0].get("content", "")
            if isinstance(first_content, list):
                for block in first_content:
                    if isinstance(block, dict):
                        existing_summary = block.get("text", "")
                        break
            elif isinstance(first_content, str):
                existing_summary = first_content
            summary_start = 1

        effective = items[summary_start:]

        # Determine split point
        keep_from_raw = max(0, len(effective) - COMPACT_KEEP_ITEMS)
        keep_from = _fix_tool_boundary(effective, keep_from_raw)

        if len(effective[:keep_from]) < COMPACT_MIN_ITEMS_TO_REMOVE:
            logger.debug("[compaction] Not enough items to compact, skipping")
            return items

        to_summarize = effective[:keep_from]
        to_keep = effective[keep_from:]

        # Cache key: hash of the prefix that would be summarized
        try:
            prefix_key = hash(json.dumps(to_summarize, default=str, sort_keys=True))
        except Exception:
            prefix_key = hash(str(to_summarize))

        if prefix_key in self._compaction_cache:
            logger.info(f"[relay #{relay_num}] [compaction] Cache hit — reusing summary")
            cached_summary_item = self._compaction_cache[prefix_key]
            return [cached_summary_item] + list(to_keep)

        # First pass: LLM summarization
        logger.info(f"[relay #{relay_num}] [compaction] Summarizing {len(to_summarize)} items, keeping {len(to_keep)}")
        print(f"[STATUS] Compacting context — summarizing {len(to_summarize)} messages…", flush=True)

        new_summary = await _summarize_messages(to_summarize, self._provider)

        # Merge with existing summary if we had one
        if existing_summary:
            merged = _merge_compact_summaries(existing_summary, new_summary)
        else:
            merged = new_summary

        # Second pass: compress
        compressed = _compress_summary(merged)

        summary_item: dict = {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": f"[CONTEXT SUMMARY — previous conversation compacted]\n{compressed}",
                }
            ],
        }

        self._compaction_cache[prefix_key] = summary_item

        print(f"[STATUS] Context compacted — summary is {len(compressed)} chars", flush=True)
        logger.info(f"[compaction] Done. Summary: {len(compressed)} chars")

        return [summary_item] + list(to_keep)


# ── Request/Response translation ─────────────────────────────────────────────


def _is_continue_only_delta(items: list) -> bool:
    """Return True if the delta contains only Codex 'Continue.' keepalive user messages.

    Codex appends 'Continue.' (optionally followed by a completion-token instruction)
    to prompt the model to keep going.  When the last browser response was a plain
    text answer (no tool calls), these keepalive messages should not reach the browser.
    """
    has_user = False
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call_output":
            return False  # Real tool result present — not Continue.-only
        role = item.get("role", "")
        if role == "user":
            has_user = True
            content = item.get("content", "")
            text = ""
            if isinstance(content, str):
                text = content.strip()
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") in ("input_text", "text"):
                        text += block.get("text", "")
                text = text.strip()
            # Allow "Continue." possibly followed by the append-marker instruction
            if not text.startswith("Continue"):
                return False
    return has_user


def _sse_or_json(stream: bool, payload: dict):
    """Return payload as SSE stream or plain JSON depending on the stream flag."""
    from aiohttp import web
    return _make_sse_response(payload) if stream else web.json_response(payload)


def _make_sse_response(responses_reply: dict):
    """Wrap a Responses API dict as a minimal SSE stream for Codex streaming requests."""
    from aiohttp import web
    in_progress = dict(responses_reply)
    in_progress["status"] = "in_progress"
    lines = [
        f"data: {json.dumps({'type': 'response.created', 'response': in_progress})}\n\n",
        f"data: {json.dumps({'type': 'response.completed', 'response': responses_reply})}\n\n",
        "data: [DONE]\n\n",
    ]
    return web.Response(
        body="".join(lines).encode(),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _extract_delta_prompt(items: list) -> str:
    """Build a prompt string from only new (delta) Responses API items.

    Skips assistant echoes and function_call items — the browser chat already
    has those. Only forwards what the browser hasn't seen: user messages and
    tool results.
    """
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        itype = item.get("type", "")
        role = item.get("role", "")
        content = item.get("content", "")

        if itype == "function_call_output":
            output = item.get("output", "")
            if isinstance(output, list):
                output = " ".join(str(o) for o in output)
            parts.append(f"[TOOL RESULT]\n{str(output)[:2000]}")
        elif role == "user":
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") in ("input_text", "text"):
                        parts.append(f"[USER]\n{block.get('text', '')}")
            elif isinstance(content, str):
                parts.append(f"[USER]\n{content}")
        # assistant and function_call items are already in the browser chat — skip them
    return "\n\n".join(parts)


def _extract_prompt_from_responses_request(body: dict) -> str:
    """Extract a human-readable prompt from a Codex Responses API request."""
    parts = []

    instructions = body.get("instructions")
    if instructions:
        parts.append(f"[SYSTEM PROMPT]\n{instructions}\n")

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


def _extract_prompt_from_chat_request(body: dict) -> str:
    """Convert a chat completions request body into a single prompt string."""
    parts = []
    for msg in body.get("messages", []):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "user").upper()
        content = msg.get("content", "")
        if isinstance(content, str):
            if role == "SYSTEM":
                parts.append(f"[SYSTEM PROMPT]\n{content}\n")
            else:
                parts.append(f"[{role}]\n{content}\n")
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(f"[{role}]\n{block.get('text', '')}\n")
    return "\n".join(parts) if parts else str(body)


def _provider_response_to_chat_completions(response: dict, relay_num: int) -> dict:
    """Convert a BrowserProvider response to OpenAI chat completions format."""
    resp_id = f"chatcmpl_{uuid.uuid4().hex[:16]}"

    if not isinstance(response, dict):
        return _make_chat_text_reply(resp_id, str(response))

    content = response.get("content", "")
    if isinstance(content, str):
        parsed = None
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            try:
                import json_repair  # type: ignore[import-not-found]
                parsed = json_repair.repair_json(content, return_objects=True)
            except Exception:
                pass

        if isinstance(parsed, dict):
            text = parsed.get("text", "")
            tool_calls = parsed.get("tool_calls", [])
            if tool_calls:
                return _make_chat_tool_reply(resp_id, text, tool_calls)
            return _make_chat_text_reply(resp_id, text or content)

        return _make_chat_text_reply(resp_id, content)

    return _make_chat_text_reply(resp_id, str(content))


def _make_chat_text_reply(resp_id: str, text: str) -> dict:
    return {
        "id": resp_id,
        "object": "chat.completion",
        "created": 0,
        "model": "kim-proxy-model",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _make_chat_tool_reply(resp_id: str, text: str, tool_calls: list) -> dict:
    formatted_calls = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            formatted_calls.append({
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {
                    "name": tc.get("name", "unknown"),
                    "arguments": json.dumps(tc.get("input", {})),
                },
            })
    return {
        "id": resp_id,
        "object": "chat.completion",
        "created": 0,
        "model": "kim-proxy-model",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": text or None,
                "tool_calls": formatted_calls,
            },
            "finish_reason": "tool_calls" if formatted_calls else "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _codex_browser_system_prompt() -> str:
    return (
        "You are Kim, a coding assistant (codex bridge json mode). "
        "The conversation below contains a [SYSTEM PROMPT] "
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
        # Try clean JSON first, then json_repair as fallback
        parsed = None
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            try:
                import json_repair  # type: ignore[import-not-found]
                parsed = json_repair.repair_json(content, return_objects=True)
            except Exception:
                pass

        if isinstance(parsed, dict):
            text = parsed.get("text", "")
            tool_calls = parsed.get("tool_calls", [])
            if tool_calls:
                return _make_responses_tool_reply(resp_id, text, tool_calls)
            return _make_responses_text_reply(resp_id, text or content)

        return _make_responses_text_reply(resp_id, content)

    return _make_responses_text_reply(resp_id, str(content))


def _make_responses_text_reply(resp_id: str, text: str) -> dict:
    return {
        "id": resp_id,
        "object": "response",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    }


def _make_responses_tool_reply(resp_id: str, text: str, tool_calls: list) -> dict:
    output_items = []

    if text:
        output_items.append({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        })

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
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    }


# ── Output parsing & surfacing ───────────────────────────────────────────────


async def _drain_stderr_to(
    stderr_stream: asyncio.StreamReader,
    stderr_lines: list,
    max_line_chars: int = 200,
) -> None:
    """Read all lines from stderr, accumulate them, and surface each one in real time.

    Lines are printed as ``[STATUS] codex: {line}`` (truncated to *max_line_chars*)
    so they appear in the user-visible activity feed rather than disappearing into a
    debug log.  Accumulated lines remain available for the non-zero-exit error message.
    Empty lines are skipped to avoid cluttering the UI with blank status entries.
    """
    async for raw in stderr_stream:
        line = raw.decode("utf-8", errors="replace").rstrip()
        if not line:
            continue
        stderr_lines.append(line)
        logger.debug("codex stderr: %s", line)
        print(f"[STATUS] codex: {line[:max_line_chars]}", flush=True)


def _surface_codex_output(stdout_text: str) -> None:
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if "Running:" in line or "Executing:" in line:
            print(f"[TOOL] {line}", flush=True)
        elif line.startswith("✓") or line.startswith("✗"):
            print(f"[STATUS] {line}", flush=True)


def _extract_final_answer(stdout_text: str) -> Optional[str]:
    lines = stdout_text.strip().splitlines()
    answer_lines = []
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            if answer_lines:
                break
            continue
        if any(marker in stripped for marker in ("Running:", "Executing:", "✓", "✗", ">>>", "---")):
            break
        answer_lines.append(stripped)

    if answer_lines:
        answer_lines.reverse()
        return "\n".join(answer_lines)
    return None


def _surface_relay_reasoning(response: dict, relay_num: int) -> None:
    if not isinstance(response, dict):
        return
    content = response.get("content", "")
    if not isinstance(content, str) or not content.strip():
        return
    display = re.sub(
        r"\b(Gemini|Claude|ChatGPT|Grok|DeepSeek)\b",
        "Kim",
        content[:150],
        flags=re.IGNORECASE,
    )
    if display and not display.startswith("{"):
        print(f"[STATUS] {display}", flush=True)


def _emit_bridge_answer(answer: str) -> None:
    cleaned = answer.strip()
    if cleaned:
        print(f"[ANSWER] {json.dumps(cleaned, ensure_ascii=False)}", flush=True)

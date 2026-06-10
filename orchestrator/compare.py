"""
Provider comparison harness — Tier 3f first slice.

Runs the same task sequentially through multiple Kim providers and returns a
structured comparison of their outputs.  Sequential execution is intentional
for the first slice: each run gets its own MCP server subprocess so there is
no shared session state, no concurrent stdio contention, and no recursive
tool-call risk.

Provider allowlist policy
-------------------------
The comparison runner does NOT enforce the scheduled-task allowlist (ollama/
browser only).  Comparisons are user-initiated, not automated, so the full
provider roster is available.  The caller is responsible for ensuring that any
API-key-bearing providers (claude, openai, gemini, etc.) have been authorised
by the user before invoking compare_providers.

Usage
-----
    from orchestrator.compare import compare_providers
    import asyncio, yaml

    with open("config.yaml") as f:
        config = yaml.safe_load(f)

    results = asyncio.run(compare_providers(
        task="What is 2 + 2?",
        providers=["ollama", "claude"],
        config=config,
    ))
    for r in results:
        print(r["provider"], r["success"], r["summary"])

First-slice scope
-----------------
- Sequential provider runs (parallel is slice 2)
- Structured result dicts, no UI
- No Tauri IPC command (slice 2)
- No LLM judge / winner selection (slice 3)
- Comparison results saved to kim_comparisons/ directory for later retrieval

_session_factory injection
--------------------------
Tests inject a fake session factory so no real MCP server is spawned.
Production callers leave _session_factory=None to get the real mcp_agent_context.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncContextManager, Callable, Optional

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 120.0

# Type alias: a factory that takes (config, provider_name) and returns an async
# context manager that yields a KimAgent-like object with .run(task) method.
SessionFactory = Callable[
    [dict, str],
    AsyncContextManager[Any],
]


def _default_session_factory(
    config: dict,
    provider_name: str,
) -> AsyncContextManager[Any]:
    from orchestrator.agent import mcp_agent_context
    return mcp_agent_context(config, provider_name=provider_name)


async def _run_one_provider(
    task: str,
    provider_name: str,
    config: dict,
    timeout: float,
    session_factory: SessionFactory,
) -> dict:
    """
    Run task against a single provider.  Returns a result dict:

        {
            "provider":          str,
            "success":           bool,
            "summary":           str | None,
            "termination":       str | None,
            "duration_seconds":  float,
            "error":             str | None,
        }

    The timeout bounds the entire provider run — factory startup (MCP server
    subprocess handshake) plus agent.run().  Previously only agent.run() was
    bounded, so a hung MCP server startup would freeze silently.
    """
    t0 = time.monotonic()
    result: dict = {
        "provider": provider_name,
        "success": False,
        "summary": None,
        "termination": None,
        "duration_seconds": 0.0,
        "error": None,
    }
    try:
        async def _run() -> dict:
            async with session_factory(config, provider_name) as agent:
                return await agent.run(task)

        run_result = await asyncio.wait_for(_run(), timeout=timeout)
        result["success"] = bool(run_result.get("success"))
        result["summary"] = run_result.get("summary") or None
        result["termination"] = run_result.get("termination") or None
    except asyncio.TimeoutError:
        result["error"] = f"timed out after {timeout:.0f}s"
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    finally:
        result["duration_seconds"] = round(time.monotonic() - t0, 3)

    return result


def _save_comparison(
    comparison: dict,
    output_dir: Path,
) -> Path:
    """Atomically write a comparison result JSON file.  Returns the path.

    Filename claiming is atomic: open() with exclusive-create ('x' / O_EXCL)
    claims the name in one syscall, so two concurrent processes always land on
    different suffixes (_2, _3, …) without a TOCTOU gap.  If writing fails after
    the file is claimed the incomplete file is deleted before re-raising.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = comparison["started_at"].replace(":", "-").replace("+", "")[:19]
    base = f"compare_{ts}"
    payload = json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    # Atomically claim a filename with O_EXCL.  On collision (concurrent process
    # or prior run with same timestamp) increment the suffix and retry.
    dest = output_dir / f"{base}.json"
    counter = 2
    while True:
        try:
            f = dest.open("x", encoding="utf-8")
            break
        except FileExistsError:
            dest = output_dir / f"{base}_{counter}.json"
            counter += 1

    try:
        f.write(payload)
    except Exception:
        try:
            f.close()
        finally:
            dest.unlink(missing_ok=True)
        raise
    else:
        f.close()

    return dest


async def compare_providers(
    task: str,
    providers: list[str],
    config: dict,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    save_dir: Optional[Path] = None,
    _session_factory: Optional[SessionFactory] = None,
) -> "tuple[list[dict], Optional[Path]]":
    """
    Run *task* sequentially through each provider in *providers*.

    Args:
        task:             Task text to run.
        providers:        List of provider name strings (e.g. ["claude", "ollama"]).
        config:           Kim config dict (from config.yaml).
        timeout_seconds:  Per-provider timeout.  Default: 120 s.
        save_dir:         Directory to write the comparison JSON.  Defaults to
                          kim_comparisons/ next to config.yaml.  Pass an explicit
                          Path to redirect; pass Path("/dev/null") to suppress.
        _session_factory: Injected factory for tests.  None → real mcp_agent_context.

    Returns:
        Tuple of (results, saved_path) where results is the list of per-provider
        result dicts (in input order) and saved_path is the Path of the written
        JSON file, or None if saving was suppressed or failed.
    """
    if not task or not task.strip():
        raise ValueError("task must not be empty")
    if not providers:
        raise ValueError("providers list must not be empty")
    providers = [str(p).strip() for p in providers]
    if any(not p for p in providers):
        raise ValueError("provider names must not be empty")
    seen: set[str] = set()
    duplicates: list[str] = []
    for provider in providers:
        key = provider.lower()
        if key in seen:
            duplicates.append(provider)
        seen.add(key)
    if duplicates:
        raise ValueError(f"duplicate providers are not allowed: {', '.join(duplicates)}")
    if len(providers) > 8:
        raise ValueError(f"compare_providers: at most 8 providers allowed, got {len(providers)}")
    if timeout_seconds < 1:
        raise ValueError(f"timeout_seconds must be >= 1, got {timeout_seconds}")

    factory = _session_factory or _default_session_factory

    started_at = datetime.now(timezone.utc).isoformat()
    results: list[dict] = []

    for provider_name in providers:
        logger.info("compare_providers: running %r with provider %r", task[:60], provider_name)
        result = await _run_one_provider(task, provider_name, config, timeout_seconds, factory)
        results.append(result)
        status = "ok" if result["success"] else f"failed ({result.get('error') or result.get('termination')})"
        logger.info(
            "compare_providers: provider=%r  success=%s  duration=%.1fs  %s",
            provider_name, result["success"], result["duration_seconds"], status,
        )

    comparison = {
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "task": task,
        "providers": list(providers),
        "timeout_seconds": timeout_seconds,
        "results": results,
    }

    # Persist comparison results
    if save_dir is None:
        # Default: kim_comparisons/ adjacent to the script root
        root = Path(__file__).resolve().parent.parent
        save_dir = root / "kim_comparisons"

    written_path: Optional[Path] = None
    if str(save_dir) not in ("/dev/null", ""):
        try:
            written_path = _save_comparison(comparison, save_dir)
            logger.info("compare_providers: saved to %s", written_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("compare_providers: could not save results: %s", exc)

    return results, written_path

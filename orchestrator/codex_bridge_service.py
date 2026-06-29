"""
Codex bridge service — lifecycle-managed launcher for browser-backed Codex runs.

Merges ``orchestrator/run_codex_bridge.py`` and the subprocess-launch logic from
``codex_engine/engine.py`` into a single module with:

- ``atexit`` handler: kills the Codex process on any exit path
- ``signal.SIGTERM`` handler: same cleanup (Tauri sends SIGTERM on cancel)
- ``tempfile.TemporaryDirectory`` context manager: automatic config cleanup
- Codex stderr forwarded to stdout as ``[STATUS] codex error: {line}``

Usage (invoked by Tauri subprocess.rs, not by humans):
    python -m orchestrator.codex_bridge_service \\
        --task "write fibonacci.py and test it" \\
        --cwd  /path/to/project \\
        --provider browser:gemini
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path
from typing import Optional

from codex_engine.engine import (
    CODEX_BINARY,
    _CodexProxy,
    _write_codex_config,
)
from orchestrator.providers.base import create_provider

# Repo root — used to locate the default ``config.yaml`` when ``--config`` is
# omitted (see ``_load_config`` below). Import resolution for ``codex_engine.*``,
# ``orchestrator.*`` and ``mcp_server.*`` comes from ``PYTHONPATH=kim_root`` set by
# the Tauri launcher (``subprocess.rs``), so no ``sys.path`` manipulation is needed.
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent

# Named constant for the placeholder API key used in local proxy auth (#53).
# The proxy validates a per-run cryptographically random bearer token; this
# value is only a human-readable label, not a real secret.
_LOCAL_PROXY_KEY = "kim-proxy-key"

logger = logging.getLogger("kim.codex_bridge_service")

# ── Module-level cleanup state ────────────────────────────────────────────────

_active_process: Optional[asyncio.subprocess.Process] = None
_active_proxy: Optional[_CodexProxy] = None


def _cleanup_sync() -> None:
    """Kill the active Codex process. Called from atexit and SIGTERM handler."""
    proc = _active_process
    if proc is not None:
        try:
            proc.kill()
        except Exception:
            pass
    # The aiohttp proxy lives in the same process — it shuts down with us.


atexit.register(_cleanup_sync)


def _install_sigterm_handler() -> None:
    def _handler(sig: int, _frame: object) -> None:
        _cleanup_sync()
        sys.exit(128 + sig)

    try:
        signal.signal(signal.SIGTERM, _handler)
    except (OSError, ValueError):
        pass  # SIGTERM not available on all platforms (e.g. Windows, nested loops)


_install_sigterm_handler()


# ── Config loading ─────────────────────────────────────────────────────────────


def _load_config(path: Optional[str]) -> dict:
    cfg_path = Path(path) if path else _REPO / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        return {}
    try:
        loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        return loaded if isinstance(loaded, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# ── CLI ────────────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run a Codex task via Kim's browser provider.")
    p.add_argument("--task", required=True, help="Coding task for Codex.")
    p.add_argument("--cwd", required=True, help="Working directory for Codex.")
    p.add_argument(
        "--provider",
        default=os.environ.get("KIM_PROVIDER", "browser"),
        help="Browser provider, e.g. 'browser:gemini'.",
    )
    p.add_argument("--model", default=None, help="Model name to pass to Codex.")
    p.add_argument("--config", default=None, help="Path to config.yaml.")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


# ── Main async logic ──────────────────────────────────────────────────────────


def _status(message: str) -> None:
    """Print a typed status line to stdout for the Rust IPC parser."""
    print(
        json.dumps({"type": "status", "message": message}, separators=(",", ":"), ensure_ascii=False),
        flush=True,
    )


async def _request_hitl_approval(task: str) -> bool:
    """Emit a HITL approval request event and block on stdin for the user's decision (#2).

    The Rust supervisor reads our stdout, sees the event, shows a confirmation
    dialog in the UI, then writes {"type": "hitl_approve", "approved": bool} to
    our stdin.  We block here (with a 120 s timeout) until that decision arrives.

    Returns True if approved, False if denied or timed out.
    """
    import asyncio

    event = {
        "type": "hitl_approval_request",
        "tool": "codex_bridge",
        "risk": "high",
        "reason": "Codex can execute arbitrary shell commands in your project directory.",
        "preview": task[:200],
    }
    print(json.dumps(event, separators=(",", ":"), ensure_ascii=False), flush=True)

    loop = asyncio.get_running_loop()
    try:
        line: str = await asyncio.wait_for(
            loop.run_in_executor(None, sys.stdin.readline),
            timeout=120.0,
        )
        data = json.loads(line.strip())
        return bool(data.get("approved", False))
    except Exception as exc:  # noqa: BLE001
        logger.warning("HITL stdin read failed (%s) — denying Codex launch", exc)
        return False


async def _run_async(args: argparse.Namespace) -> int:
    global _active_process, _active_proxy  # noqa: PLW0603

    if not args.provider.lower().startswith("browser"):
        print(
            f"[ERROR] codex_bridge_service requires a browser provider; got {args.provider!r}.",
            file=sys.stderr,
        )
        return 2

    config = _load_config(args.config)
    config["provider"] = args.provider
    os.environ["PROJECT_ROOT"] = args.cwd

    # Gate: when running under Tauri, require explicit HITL approval before
    # spawning Codex.  Codex can run arbitrary shell commands, so this is a
    # HIGH-risk operation that must never silently auto-execute (#2).
    if os.environ.get("KIM_TAURI_MODE") == "1":
        approved = await _request_hitl_approval(args.task)
        if not approved:
            _status("Codex launch denied by user. No code was executed.")
            print("[FAILED] Codex launch denied by user.", flush=True)
            return 1

    _status(f"✓ Using Codex via browser bridge ({args.provider})")

    provider = create_provider(args.provider, config)
    # Reset per-session state so the new task gets a fresh system prompt injection.
    if hasattr(provider, "_sent_system_prompt"):
        provider._sent_system_prompt = False  # type: ignore[attr-defined]

    # Resolve the Codex binary (Tauri may have set CODEX_BIN).
    binary = os.environ.get("CODEX_BIN", "").strip() or CODEX_BINARY
    binary_path = shutil.which(binary) if not os.path.isabs(binary) else binary
    if not binary_path or not os.path.exists(binary_path):
        print(f"[FAILED] Codex binary not found: {binary}.", flush=True)
        return 1

    _status(f"codex binary: {binary_path}")

    proxy = _CodexProxy(provider, provider_name=args.provider)
    _active_proxy = proxy
    proxy_port = await proxy.start()

    logger.info("Proxy started on port %d", proxy_port)

    try:
        with tempfile.TemporaryDirectory(prefix="kim-codex-config-") as config_dir:
            config_file = Path(config_dir) / "config.toml"
            _write_codex_config(config_file, proxy_port, args.model)

            # Minimal env — do not inherit full os.environ (parent secrets) (#1).
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", ""),
                "USER": os.environ.get("USER", ""),
                "TMPDIR": os.environ.get("TMPDIR", ""),
                "LANG": os.environ.get("LANG", ""),
                "CODEX_HOME": config_dir,
                # Per-run bearer token generated by the proxy (#47).
                "CODEX_API_KEY": proxy._bearer_token,
                "OPENAI_API_KEY": proxy._bearer_token,
                "OPENAI_BASE_URL": f"http://127.0.0.1:{proxy_port}/v1",
            }
            # On Windows the POSIX vars above are absent; forward the essentials
            # so the codex child process can locate system tools and temp storage.
            if sys.platform == "win32":
                _WIN_PASSTHROUGH = (
                    "SystemRoot",
                    "ComSpec",
                    "USERPROFILE",
                    "TEMP",
                    "TMP",
                    # Windows uses "Path" (mixed-case) in addition to "PATH".
                    "Path",
                )
                for _var in _WIN_PASSTHROUGH:
                    if _var in os.environ:
                        env[_var] = os.environ[_var]
            # --dangerously-bypass-approvals-and-sandbox requires explicit opt-in (#1).
            bypass_flag = os.environ.get("KIM_CODEX_BYPASS_SANDBOX", "").strip()
            cmd = [
                str(binary_path),
                "exec",
                "--json",
            ]
            if bypass_flag == "1":
                cmd.append("--dangerously-bypass-approvals-and-sandbox")
            cmd += ["-C", args.cwd, args.task]

            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _active_process = process

                async def _stream_stdout() -> None:
                    assert process.stdout
                    async for raw in process.stdout:
                        line = raw.decode("utf-8", errors="replace").rstrip()
                        if line:
                            print(line, flush=True)

                async def _drain_stderr() -> None:
                    assert process.stderr
                    async for raw in process.stderr:
                        line = raw.decode("utf-8", errors="replace").rstrip()
                        if line:
                            logger.debug("codex stderr: %s", line)
                            # Surface codex subprocess errors to the Kim activity feed.
                            _status(f"codex error: {line}")

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
                    print("[FAILED] Codex task timed out after 10 minutes.", flush=True)
                    return 1

                exit_code = await process.wait()
                _active_process = None

                if exit_code != 0:
                    print(f"[FAILED] Codex exited with code {exit_code}.", flush=True)

                return exit_code

            except Exception as e:
                logger.exception("Codex bridge service crashed")
                print(f"[FAILED] Codex bridge error: {e}", flush=True)
                return 1
            finally:
                _active_process = None

    finally:
        await proxy.stop()
        _active_proxy = None


# ── Entry point ────────────────────────────────────────────────────────────────


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        rc = asyncio.run(_run_async(args))
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()

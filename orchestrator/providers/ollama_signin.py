"""Ollama cloud sign-in: trigger `ollama signin` and wait for it to land, so a
cloud-mode turn that hits Ollama's "sign in required" error can pick the
sign-in up automatically instead of dying with a dead-end error the user has
to go fix in another terminal (see `OllamaProvider.complete()`'s
`except PermissionError` branch in orchestrator/providers/ollama.py, and the
"sign in"/"unauthorized"/"forbidden" detection in its `_stream_chat_inner`).

Why shell out to `ollama signin` instead of building an OAuth flow: Ollama
already owns that flow entirely — when NOT signed in, `ollama signin` prints
a device-code URL, opens the user's default browser, and blocks until the
OAuth callback lands (or it times out on its own). When ALREADY signed in it
prints "You are already signed in as user '<name>'" and returns immediately
(~0.5s, verified live against Ollama 0.32.0, which has no `whoami`
subcommand — see desktop/src-tauri/src/ollama.rs's `/api/me`-based fix for
the same underlying problem on the desktop side). We don't add a separate
"check first" step here: this module is only invoked AFTER a real cloud
request already failed with the sign-in-required signal, so launching
`ollama signin` (and its browser popup) is exactly the action the caller
wants at that point — never an unexpected side effect of a passive check.

The wait below is a genuine poll (bounded interval/timeout, cancellable, with
periodic progress logging) rather than a single fixed-length timer, because
`ollama signin`'s own internal timeout (if any) is undocumented and we must
not hang the request forever nor give up before a normal-speed human has a
chance to click through the browser flow.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

logger = logging.getLogger(__name__)

# "poll every ~2s, up to ~2min" — sane bounds per the task brief; adjustable
# by callers (kept as parameters, not hardcoded, so tests run fast).
SIGNIN_POLL_INTERVAL_S = 2.0
SIGNIN_TIMEOUT_S = 120.0


class OllamaSigninUnavailable(PermissionError):
    """The `ollama` CLI could not be launched at all (missing / broken)."""


class OllamaSigninTimeout(PermissionError):
    """Sign-in did not complete before the poll deadline."""


class OllamaSigninFailed(PermissionError):
    """`ollama signin` exited non-zero (declined, CLI error, ...)."""


async def _drain_stdout(proc: "asyncio.subprocess.Process") -> bytes:
    assert proc.stdout is not None
    return await proc.stdout.read()


async def trigger_signin_and_wait(
    poll_interval_s: float = SIGNIN_POLL_INTERVAL_S,
    timeout_s: float = SIGNIN_TIMEOUT_S,
) -> None:
    """Launch `ollama signin` and poll it to completion.

    Returns normally once the process exits 0 (covers both a fresh sign-in
    completed in the browser and the already-signed-in fast path). Raises a
    `PermissionError` subclass — safe for any existing `except
    PermissionError` handler upstream — on every other outcome: CLI missing,
    declined/failed sign-in, or the poll deadline passing first. Propagates
    `asyncio.CancelledError` (e.g. the caller's turn was interrupted)
    after killing the child so it never leaks a background process.
    """
    logger.warning(
        "OllamaProvider: cloud request needs sign-in — launching `ollama signin` "
        "(opens your browser). Complete it there; this turn resumes automatically "
        "(waiting up to %ds).",
        int(timeout_s),
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "ollama", "signin",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise OllamaSigninUnavailable(
            "Sign in to Ollama to use cloud models — the `ollama` CLI was not "
            "found on PATH. Install Ollama, then run `ollama signin` manually."
        ) from exc
    except OSError as exc:
        raise OllamaSigninUnavailable(f"Could not launch `ollama signin`: {exc}") from exc

    output_task = asyncio.create_task(_drain_stdout(proc))
    elapsed = 0.0
    try:
        while True:
            try:
                await asyncio.wait_for(proc.wait(), timeout=poll_interval_s)
                break
            except asyncio.TimeoutError:
                elapsed += poll_interval_s
                if elapsed >= timeout_s:
                    proc.kill()
                    with contextlib.suppress(Exception):
                        await proc.wait()
                    output_task.cancel()
                    raise OllamaSigninTimeout(
                        f"Ollama sign-in did not complete within {int(timeout_s)}s. "
                        "Finish signing in in your browser and try again, or run "
                        "`ollama signin` manually in another terminal."
                    )
                logger.info(
                    "OllamaProvider: still waiting for Ollama sign-in (%ds elapsed)...",
                    int(elapsed),
                )
    except asyncio.CancelledError:
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        output_task.cancel()
        raise

    output = ""
    with contextlib.suppress(Exception):
        raw = await asyncio.wait_for(output_task, timeout=2.0)
        output = raw.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        detail = f" {output}" if output else ""
        raise OllamaSigninFailed(f"Ollama sign-in did not complete.{detail}")

    logger.info("OllamaProvider: Ollama sign-in confirmed.%s", f" {output}" if output else "")

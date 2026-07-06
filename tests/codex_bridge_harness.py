"""Behavioral test harness for the live codex-bridge spawn path.

Drives ``orchestrator.codex_bridge_service._run_async`` end-to-end with a REAL
fake ``codex`` binary (a tiny Python script) that records the exact argv and
env dict it was spawned with. Tests assert on that observed behavior — never
on source text.

The only seams faked are the ones that would touch the network / a real
browser: ``create_provider`` and ``_CodexProxy`` (replaced by stubs), plus the
on-disk thread-state dir (redirected into the test tmpdir).

This is the K4 pattern from docs/ROADMAP_TO_10.md: spawn a fake binary and
assert the actual argv + env passed, pinning the hardened minimal-allowlist
env contract of codex_bridge_service (NOT ``**os.environ``).
"""

from __future__ import annotations

import json
import os
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

FAKE_BEARER_TOKEN = "test-bearer-token-0123456789"
FAKE_PROXY_PORT = 45871

# The hardened minimal env allowlist that codex_bridge_service passes to the
# codex subprocess on POSIX platforms. This is the single env contract — the
# old ``**os.environ`` spread (dead run_codex_subtask) was deleted in Phase 0.
EXPECTED_POSIX_ENV_KEYS = {
    "PATH",
    "HOME",
    "USER",
    "TMPDIR",
    "LANG",
    "CODEX_HOME",
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
}

# Vars some platforms inject into every child process — tolerated, not part
# of the contract.
PLATFORM_INJECTED_ENV_KEYS = {"__CF_USER_TEXT_ENCODING"}

# Env vars that must never influence a test unless a test sets them itself.
_SCRUBBED_VARS = (
    "KIM_CODEX_BYPASS_SANDBOX",
    "KIM_CODEX_SKIP_GIT_CHECK",
    "KIM_TAURI_MODE",
    "KIM_CLI_SESSION_ID",
    "KIM_PROVIDER",
    "CODEX_BIN",
)


def make_fake_codex_binary(
    dir_path: Path, *, sleep_s: float = 0.0, exit_code: int = 0
) -> Path:
    """Write an executable script that records its argv+env+pid, then exits."""
    dir_path.mkdir(parents=True, exist_ok=True)
    script = dir_path / "fake-codex"
    script.write_text(
        f"""#!{sys.executable}
import json, os, sys, time
base = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(base, "pid.txt"), "w") as f:
    f.write(str(os.getpid()))
with open(os.path.join(base, "capture.json"), "w") as f:
    json.dump({{"argv": sys.argv[1:], "env": dict(os.environ)}}, f)
time.sleep({sleep_s})
sys.exit({exit_code})
"""
    )
    script.chmod(0o755)
    return script


class FakeBrowserProvider:
    """Stub provider — the proxy is also stubbed, so complete() is never hit."""

    def __init__(self) -> None:
        self._sent_system_prompt = True

    async def complete(self, **kwargs):  # pragma: no cover - never called
        return {"type": "text", "content": "ok"}


async def run_bridge(
    tmp: Path,
    *,
    task: str = "write hello.py",
    provider_name: str = "browser:gemini",
    env: dict | None = None,
    git_repo: bool = True,
    sleep_s: float = 0.0,
    exit_code: int = 0,
    config_yaml: str | None = None,
    binary_override: str | None = None,
):
    """Run _run_async end-to-end against a real fake codex binary.

    Returns a namespace with:
        rc          — the exit code returned by _run_async
        capture     — {"argv": [...], "env": {...}} recorded by the fake
                      binary, or None if it was never spawned
        parent_env  — the (patched) parent environ dict after the run
        bin_dir     — directory holding the fake binary + capture files
        project     — the project cwd passed via --cwd
        proxy       — the stub _CodexProxy instance
    """
    from orchestrator import codex_bridge_service as svc
    import codex_engine.thread_state as ts

    project = tmp / "project"
    project.mkdir(exist_ok=True)
    if git_repo:
        (project / ".git").mkdir(exist_ok=True)

    bin_dir = tmp / "bin"
    binary = make_fake_codex_binary(bin_dir, sleep_s=sleep_s, exit_code=exit_code)

    config_path = tmp / "config.yaml"
    config_path.write_text(config_yaml or "browser_provider: {}\n")

    parent_env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_VARS}
    parent_env["CODEX_BIN"] = binary_override or str(binary)
    parent_env.update(env or {})

    fake_proxy = MagicMock()
    fake_proxy._bearer_token = FAKE_BEARER_TOKEN
    fake_proxy.start = AsyncMock(return_value=FAKE_PROXY_PORT)
    fake_proxy.stop = AsyncMock()

    args = Namespace(
        task=task,
        cwd=str(project),
        provider=provider_name,
        model=None,
        config=str(config_path),
        verbose=False,
    )

    with (
        patch.object(os, "environ", parent_env),
        patch.object(ts, "_STATE_DIR", tmp / "state"),
        patch.object(svc, "create_provider", return_value=FakeBrowserProvider()),
        patch.object(svc, "_CodexProxy", return_value=fake_proxy),
    ):
        rc = await svc._run_async(args)

    capture_file = bin_dir / "capture.json"
    capture = (
        json.loads(capture_file.read_text()) if capture_file.exists() else None
    )
    return SimpleNamespace(
        rc=rc,
        capture=capture,
        parent_env=parent_env,
        bin_dir=bin_dir,
        project=str(project),
        proxy=fake_proxy,
    )

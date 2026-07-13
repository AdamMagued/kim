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
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from cross_platform_helpers import write_python_executable

FAKE_BEARER_TOKEN = "test-bearer-token-0123456789"
FAKE_PROXY_PORT = 45871

# The hardened minimal env allowlist that codex_bridge_service passes to the
# codex subprocess on every platform. This is the single env contract — the
# old ``**os.environ`` spread (dead run_codex_subtask) was deleted in Phase 0.
EXPECTED_BASE_ENV_KEYS = {
    "PATH",
    "HOME",
    "USER",
    "TMPDIR",
    "LANG",
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
}

# Vars some platforms inject into every child process — tolerated, not part
# of the contract. macOS injects __CF_USER_TEXT_ENCODING and, on some
# shells/locales, LC_CTYPE into every spawned process regardless of the env
# dict passed to exec; neither reaches the child from Kim's own allowlist.
WINDOWS_PASSTHROUGH_ENV_KEYS = (
    "SystemRoot",
    "ComSpec",
    "USERPROFILE",
    "TEMP",
    "TMP",
    "PATHEXT",
)

PLATFORM_INJECTED_ENV_KEYS = {"__CF_USER_TEXT_ENCODING", "LC_CTYPE"}

# Env vars that must never influence a test unless a test sets them itself.
_SCRUBBED_VARS = (
    "KIM_CODEX_BYPASS_SANDBOX",
    "KIM_CODEX_SKIP_GIT_CHECK",
    "KIM_TAURI_MODE",
    "KIM_CLI_SESSION_ID",
    "KIM_PROVIDER",
    "CODEX_BIN",
    "CODEX_HOME",
    "KIM_RUN_ID",
    "KIM_SESSION_ID",
)

# The exec-path contract these harness tests pin. The service default is now
# the app-server transport, so exec must be selected explicitly.
_DEFAULT_CONFIG_YAML = "browser_provider: {}\ncodex_bridge:\n  transport: exec\n"


def make_fake_codex_binary(
    dir_path: Path, *, sleep_s: float = 0.0, exit_code: int = 0, spawn_child: bool = False
) -> Path:
    """Write an executable script that records its argv+env+pid, then exits.

    spawn_child=True additionally forks off a long-sleeping grandchild
    process (its pid recorded to child_pid.txt) and does NOT wait for it —
    mirroring a shell/tool subprocess codex exec itself launches. Used to
    prove a timeout kill reaps the whole process tree, not just the direct
    codex-exec PID (finding 3).
    """
    child_spawn_code = (
        """
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
with open(os.path.join(base, "child_pid.txt"), "w", encoding="utf-8") as f:
    f.write(str(child.pid))
"""
        if spawn_child
        else ""
    )
    source = f"""
import json
import os
import subprocess
import sys
import time

base = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(base, "pid.txt"), "w", encoding="utf-8") as f:
    f.write(str(os.getpid()))
with open(os.path.join(base, "capture.json"), "w", encoding="utf-8") as f:
    json.dump({{"argv": sys.argv[1:], "env": dict(os.environ)}}, f)
{child_spawn_code}
time.sleep({sleep_s})
sys.exit({exit_code})
"""
    return write_python_executable(dir_path, "fake-codex", source)


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
    spawn_child: bool = False,
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
    binary = make_fake_codex_binary(
        bin_dir, sleep_s=sleep_s, exit_code=exit_code, spawn_child=spawn_child
    )

    config_path = tmp / "config.yaml"
    config_path.write_text(config_yaml or _DEFAULT_CONFIG_YAML, encoding="utf-8")

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
        patch.dict(os.environ, parent_env, clear=True),
        patch.object(ts, "_STATE_DIR", tmp / "state"),
        patch.object(svc, "create_provider", return_value=FakeBrowserProvider()),
        patch.object(svc, "_CodexProxy", return_value=fake_proxy),
    ):
        rc = await svc._run_async(args)
        observed_parent_env = dict(os.environ)

    capture_file = bin_dir / "capture.json"
    capture = (
        json.loads(capture_file.read_text(encoding="utf-8")) if capture_file.exists() else None
    )
    return SimpleNamespace(
        rc=rc,
        capture=capture,
        parent_env=observed_parent_env,
        bin_dir=bin_dir,
        project=str(project),
        proxy=fake_proxy,
    )

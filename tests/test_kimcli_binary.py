"""Offline end-to-end smoke of the REAL kimcli binary (or a functional
codex stand-in), driven exactly the way ``kim tui`` drives it.

Unlike ``test_appserver_real_binary.py`` (app-server/JSON-RPC transport,
``kim tui``'s default), this file exercises the OTHER two real-binary
surfaces kimcli ships:

  a. ``kimcli --version`` branding string.
  b. ``kimcli update`` refusal (Kim owns the version pin; kimcli must never
     self-update — see docs/kimcli.md "Version-bump playbook" step 3).
  c. The legacy ``exec --json`` transport (``orchestrator.codex_bridge_
     service._run_exec_task``'s spawn shape) against a REAL
     ``codex_engine.standalone_proxy --provider fake`` subprocess — the
     full responses-passthrough loop, headless.
  d. The same loop with ``-c mcp_servers.kim.*`` attached (the ``kim tui``
     launcher's ``build_kimcli_argv`` shape, ``cli/src/commands/tui/
     argv.rs``), proving MCP attach doesn't break a turn and that kimcli
     itself reports the ``kim`` MCP server as configured.

Binary resolution (first match wins):
  1. ``KIMCLI_BIN`` env var (a path or a PATH-searchable name).
  2. The local debug build at ``/Volumes/AdamSSD/kimcli-build-target/debug/kimcli``.
  3. ``kimcli`` on PATH.
  4. ``codex`` on PATH — a FUNCTIONAL stand-in. kimcli is a branding-only
     fork of codex-cli 0.144.3 (see docs/kimcli.md); its wire protocol and
     config-override surface are byte-for-byte identical to upstream codex,
     so codex exercises the same (c)/(d) proxy-facing behavior. Branding-only
     assertions ((a), (b)) are skipped for this stand-in — see IS_KIMCLI.
  5. Otherwise: skip this whole module (no binary available, e.g. plain CI
     without the kimcli-e2e job's codex install step).

Each test spawns its own ``standalone_proxy --provider fake`` subprocess (the
same ``FakeProvider`` used by ``tests/test_standalone_proxy.py`` and the
``KIM_FAKE=1`` dev mode) and its own kimcli/codex ``exec --json`` subprocess,
both as process-group leaders so a timeout kills the whole tree (mirrors
``orchestrator/codex_bridge_service.py``'s own spawn + ``_kill_process_tree``
teardown), never just the direct PID.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_bridge_harness import _SCRUBBED_VARS  # noqa: E402

from orchestrator.process_kill import _kill_process_tree  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LOCAL_BUILD_BIN = Path("/Volumes/AdamSSD/kimcli-build-target/debug/kimcli")

_VERSION_RE = re.compile(r"kimcli \d+\.\d+\.\d+ \(rebranded codex-cli \d+\.\d+\.\d+\)")

# Generous but bounded — a hung child must never hang the suite.
_HANDSHAKE_TIMEOUT_S = 20.0
_TASK_TIMEOUT_S = 90.0
_SHORT_TIMEOUT_S = 20.0


def _resolve_binary() -> Optional[str]:
    """First match wins: KIMCLI_BIN env -> local debug build -> PATH kimcli
    -> PATH codex (functional stand-in) -> None (caller skips the module)."""
    env_bin = os.environ.get("KIMCLI_BIN", "").strip()
    if env_bin:
        resolved = shutil.which(env_bin)
        if not resolved and Path(env_bin).is_file() and os.access(env_bin, os.X_OK):
            resolved = env_bin
        if resolved:
            return resolved
    if _LOCAL_BUILD_BIN.is_file() and os.access(_LOCAL_BUILD_BIN, os.X_OK):
        return str(_LOCAL_BUILD_BIN)
    found = shutil.which("kimcli")
    if found:
        return found
    return shutil.which("codex")


def _version_output(binary: str) -> str:
    try:
        result = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=_SHORT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (result.stdout or "") + (result.stderr or "")


BIN = _resolve_binary()
_VERSION_OUTPUT = _version_output(BIN) if BIN else ""
# True only for an actual kimcli binary (branding string present) — a bare
# codex stand-in prints "codex-cli 0.144.3" instead and must skip (a)/(b).
IS_KIMCLI = bool(BIN) and bool(_VERSION_RE.search(_VERSION_OUTPUT))

if BIN is None:
    pytest.skip(
        "no kimcli/codex binary available (checked KIMCLI_BIN, "
        f"{_LOCAL_BUILD_BIN}, PATH kimcli, PATH codex)",
        allow_module_level=True,
    )


def _bin() -> str:
    """Narrows module-level ``BIN: Optional[str]`` to ``str`` for type
    checkers — the module-level skip above already guarantees this at
    runtime for every test that actually executes."""
    assert BIN is not None
    return BIN


# ── argv construction — replicates cli/src/commands/tui/argv.rs ─────────────


def _toml_quote(value: str) -> str:
    """Mirrors argv.rs's ``toml_quote``: escape for a TOML basic string."""
    out = ['"']
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _c_override(key: str, value: str) -> list[str]:
    return ["-c", f"{key}={_toml_quote(value)}"]


def _c_override_raw(key: str, raw_value: str) -> list[str]:
    return ["-c", f"{key}={raw_value}"]


def _proxy_route_overrides(port: int, model: str = "kim-proxy-model") -> list[str]:
    """``-c`` overrides routing kimcli at Kim's local proxy — same shape as
    argv.rs's ``proxy_route_overrides`` / codex_bridge_service.py's
    ``_exec_config_overrides``."""
    argv: list[str] = []
    argv += _c_override("model_provider", "kim-proxy")
    argv += _c_override("model", model)
    argv += _c_override("model_providers.kim-proxy.name", "Kim Proxy")
    argv += _c_override("model_providers.kim-proxy.base_url", f"http://127.0.0.1:{port}/v1")
    argv += _c_override("model_providers.kim-proxy.wire_api", "responses")
    argv += _c_override("model_providers.kim-proxy.env_key", "CODEX_API_KEY")
    return argv


def _mcp_kim_overrides(python: str, kim_root: str, target_cwd: str) -> list[str]:
    """``-c mcp_servers.kim.*`` overrides — same shape as argv.rs's
    ``mcp_kim_overrides``."""
    argv: list[str] = []
    argv += _c_override("mcp_servers.kim.command", python)
    argv += _c_override_raw("mcp_servers.kim.args", '["-m","mcp_server.server"]')
    argv += _c_override("mcp_servers.kim.cwd", kim_root)
    argv += _c_override_raw(
        "mcp_servers.kim.env",
        "{PROJECT_ROOT=" + _toml_quote(target_cwd) + ",KIM_ENABLED_TOOL_TIERS=" + _toml_quote("ui,browser") + "}",
    )
    argv += _c_override_raw("mcp_servers.kim.startup_timeout_sec", "30")
    argv += _c_override_raw("mcp_servers.kim.tool_timeout_sec", "120")
    return argv


# ── subprocess plumbing ──────────────────────────────────────────────────────


def _group_kwargs() -> dict:
    """Spawn as a process-group leader so a timeout kill reaps the whole
    tree (mirrors codex_bridge_service.py's own spawn)."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _spawn_proxy(*, provider: str = "fake") -> subprocess.Popen:
    """Real ``codex_engine.standalone_proxy`` subprocess (not stubbed) —
    same invocation shape as tests/test_standalone_proxy.py."""
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_VARS}
    env.pop("KIM_FAKE", None)  # never let ambient KIM_FAKE short-circuit --provider
    pythonpath = str(_REPO_ROOT)
    if env.get("PYTHONPATH"):
        pythonpath += os.pathsep + env["PYTHONPATH"]
    env["PYTHONPATH"] = pythonpath
    return subprocess.Popen(
        [sys.executable, "-u", "-m", "codex_engine.standalone_proxy", "--provider", provider],
        cwd=str(_REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        **_group_kwargs(),
    )


def _read_handshake(proc: subprocess.Popen, timeout: float = _HANDSHAKE_TIMEOUT_S) -> dict:
    assert proc.stdout is not None and proc.stderr is not None  # PIPE was requested
    stdout, stderr = proc.stdout, proc.stderr
    q: "queue.Queue" = queue.Queue()
    threading.Thread(target=lambda: q.put(stdout.readline()), daemon=True).start()
    try:
        line = q.get(timeout=timeout)
    except queue.Empty:
        line = None
    if not line:
        stderr_tail = stderr.read() if proc.poll() is not None else "<proxy still running>"
        raise AssertionError(
            f"standalone_proxy produced no handshake line within {timeout}s; stderr={stderr_tail!r}"
        )
    payload = json.loads(line)
    if payload.get("event") != "ready":
        raise AssertionError(f"standalone_proxy handshake failed: {payload}")
    return payload


def _cleanup_proc(proc: Optional[subprocess.Popen], timeout: float = 10.0) -> None:
    if proc is None:
        return
    if proc.poll() is None:
        _kill_process_tree(proc.pid)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
    for stream in (proc.stdout, proc.stderr):
        try:
            if stream:
                stream.close()
        except OSError:
            pass


def _kimcli_env(*, token: str, codex_home: str) -> dict:
    """Minimal env for the spawned kimcli/codex process — mirrors
    codex_bridge_service.py's ``_run_exec_task`` minimal-env contract (#1: no
    inherited parent secrets). ``CODEX_HOME`` is pinned to an isolated tmp
    dir so the test never reads/depends on a real ``~/.codex/config.toml``
    (this dev machine's has unrelated MCP servers configured, verified
    manually via ``kimcli mcp list``)."""
    return {
        "PATH": os.environ.get("PATH") or os.environ.get("Path", ""),
        "HOME": os.environ.get("HOME") or os.environ.get("USERPROFILE", ""),
        "USER": os.environ.get("USER") or os.environ.get("USERNAME", ""),
        "TMPDIR": os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP", ""),
        "LANG": os.environ.get("LANG", ""),
        "CODEX_API_KEY": token,
        "OPENAI_API_KEY": token,
        "CODEX_HOME": codex_home,
    }


def _run_subprocess(cmd: list[str], *, env: dict, cwd: str, timeout: float) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        **_group_kwargs(),
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc.pid)
        stdout, stderr = proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _agent_message_texts(jsonl_output: str) -> list[str]:
    """``exec --json`` output -> the text of every ``item.completed`` /
    ``agent_message`` event (the assistant-visible reply stream)."""
    texts: list[str] = []
    for line in jsonl_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict) or parsed.get("type") != "item.completed":
            continue
        item = parsed.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            texts.append(str(item.get("text", "")))
    return texts


# ── (a) version / branding ───────────────────────────────────────────────────


@unittest.skipUnless(IS_KIMCLI, "resolved binary is the codex stand-in, not kimcli — branding N/A")
class VersionBrandingTests(unittest.TestCase):
    def test_version_matches_rebrand_pattern(self):
        result = subprocess.run([_bin(), "--version"], capture_output=True, text=True, timeout=_SHORT_TIMEOUT_S)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout.strip(), _VERSION_RE, result.stdout)


# ── (b) update refusal ───────────────────────────────────────────────────────


@unittest.skipUnless(IS_KIMCLI, "codex stand-in legitimately self-updates — refusal is a kimcli-only behavior")
class UpdateRefusalTests(unittest.TestCase):
    def test_update_refuses_with_nonzero_exit(self):
        result = subprocess.run([_bin(), "update"], capture_output=True, text=True, timeout=_SHORT_TIMEOUT_S)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)


# ── (c) full loop via responses-passthrough ──────────────────────────────────


class ResponsesPassthroughExecTests(unittest.TestCase):
    """The legacy ``exec --json`` transport (codex_bridge_service.py's
    ``_run_exec_task`` spawn shape) against a REAL standalone_proxy, REAL
    kimcli/codex binary, only the LLM call itself stubbed (FakeProvider)."""

    def setUp(self):
        self.proxy: Optional[subprocess.Popen] = None
        # ignore_cleanup_errors: kimcli's plugin subsystem can write
        # transient files under CODEX_HOME/.tmp (e.g. a plugin-registry
        # probe) that outlive the exec call by a beat; never let that race
        # fail an otherwise-passing test at teardown.
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

    def tearDown(self):
        _cleanup_proc(self.proxy)
        self.tmp.cleanup()

    def test_full_turn_completes_with_canned_reply(self):
        self.proxy = _spawn_proxy(provider="fake")
        handshake = _read_handshake(self.proxy)
        port, token = handshake["port"], handshake["token"]

        project = Path(self.tmp.name) / "project"
        project.mkdir()
        codex_home = Path(self.tmp.name) / "codex_home"
        codex_home.mkdir()

        cmd = [
            _bin(), "exec", "--json",
            *_proxy_route_overrides(port),
            "--skip-git-repo-check",
            "-C", str(project),
            "say hello",
        ]
        result = _run_subprocess(
            cmd, env=_kimcli_env(token=token, codex_home=str(codex_home)),
            cwd=str(project), timeout=_TASK_TIMEOUT_S,
        )
        self.assertEqual(result.returncode, 0, result.stdout + "\n---stderr---\n" + result.stderr)
        texts = _agent_message_texts(result.stdout)
        self.assertTrue(texts, f"no agent_message events in output: {result.stdout!r}")
        # FakeProvider's default scripted reply (orchestrator/providers/fake.py):
        # first turn is a tool_call (take_screenshot, unsupported outside the
        # MCP-attached test below — kimcli reports it back as a tool error),
        # second turn is this fixed TASK_COMPLETE text — proving the full
        # request/response loop (proxy auth + translation + the real binary's
        # own turn/relay handling) round-tripped for real.
        self.assertTrue(
            any("Fake run complete" in t for t in texts),
            f"canned reply text not found in agent_message events: {texts!r}",
        )


# ── (d) MCP attach smoke ─────────────────────────────────────────────────────


class McpAttachSmokeTests(unittest.TestCase):
    """Same loop as (c), plus ``-c mcp_servers.kim.*`` wired at Kim's own
    ``mcp_server.server`` (argv.rs's ``mcp_kim_overrides`` shape). The
    canned FakeProvider can't itself call a Kim tool (its scripted replies
    are fixed), so the assertion is the strongest one actually observable
    offline: (1) ``kimcli mcp list`` reports ``kim`` as a configured,
    enabled MCP server for these exact overrides, and (2) attaching it does
    not break a real turn."""

    def setUp(self):
        self.proxy: Optional[subprocess.Popen] = None
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

    def tearDown(self):
        _cleanup_proc(self.proxy)
        self.tmp.cleanup()

    def _mcp_argv(self, target_cwd: str) -> list[str]:
        return _mcp_kim_overrides(sys.executable, str(_REPO_ROOT), target_cwd)

    def test_mcp_server_listed_as_kim_and_enabled(self):
        project = Path(self.tmp.name) / "project"
        project.mkdir()
        codex_home = Path(self.tmp.name) / "codex_home"
        codex_home.mkdir()

        cmd = [_bin(), "mcp", "list", *self._mcp_argv(str(project))]
        result = _run_subprocess(
            cmd,
            env=_kimcli_env(token="unused", codex_home=str(codex_home)),
            cwd=str(project),
            timeout=_SHORT_TIMEOUT_S,
        )
        self.assertEqual(result.returncode, 0, result.stdout + "\n---stderr---\n" + result.stderr)
        lines = [ln for ln in result.stdout.splitlines() if ln.strip().startswith("kim ")]
        self.assertTrue(lines, f"'kim' MCP server not listed: {result.stdout!r}")
        self.assertIn("enabled", lines[0], lines[0])

    def test_full_turn_completes_with_mcp_attached(self):
        self.proxy = _spawn_proxy(provider="fake")
        handshake = _read_handshake(self.proxy)
        port, token = handshake["port"], handshake["token"]

        project = Path(self.tmp.name) / "project"
        project.mkdir()
        codex_home = Path(self.tmp.name) / "codex_home"
        codex_home.mkdir()

        cmd = [
            _bin(), "exec", "--json",
            *_proxy_route_overrides(port),
            *self._mcp_argv(str(project)),
            "--skip-git-repo-check",
            "-C", str(project),
            "say hello with mcp attached",
        ]
        result = _run_subprocess(
            cmd, env=_kimcli_env(token=token, codex_home=str(codex_home)),
            cwd=str(project), timeout=_TASK_TIMEOUT_S,
        )
        self.assertEqual(result.returncode, 0, result.stdout + "\n---stderr---\n" + result.stderr)
        texts = _agent_message_texts(result.stdout)
        self.assertTrue(
            any("Fake run complete" in t for t in texts),
            f"canned reply text not found with MCP attached: {texts!r}",
        )


if __name__ == "__main__":
    unittest.main()

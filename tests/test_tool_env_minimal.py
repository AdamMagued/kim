"""
S4 behavioral tests: NO tool subprocess inherits the full parent environment.

These spawn REAL child processes through the live handlers and read the env
the child actually observed — the Phase 0 harness principle (assert on
recorded reality, not source text).
"""

from __future__ import annotations

import asyncio

import pytest

from mcp_server.os_utils import IS_WINDOWS, minimal_subprocess_env

_SECRETS = {
    "OPENAI_API_KEY": "sk-super-secret",
    "ANTHROPIC_API_KEY": "sk-ant-secret",
    "GITHUB_TOKEN": "ghp_secret",
    "AWS_SECRET_ACCESS_KEY": "aws-secret",
    "KIM_RANDOM_APP_VAR": "leaky",
}


@pytest.fixture
def planted_secrets(monkeypatch):
    for k, v in _SECRETS.items():
        monkeypatch.setenv(k, v)


def _assert_no_secrets(env_dump: str) -> None:
    for key, value in _SECRETS.items():
        assert key not in env_dump, f"{key} leaked into a tool subprocess"
        assert value not in env_dump, f"value of {key} leaked into a tool subprocess"


class TestMinimalEnvBuilder:
    def test_allowlist_only(self, planted_secrets):
        env = minimal_subprocess_env()
        for key in _SECRETS:
            assert key not in env
        assert "PATH" in env or "Path" in env

    def test_injection_vectors_never_present(self, monkeypatch):
        monkeypatch.setenv("LD_PRELOAD", "/evil.so")
        monkeypatch.setenv("PYTHONPATH", "/evil")
        monkeypatch.setenv("NODE_PATH", "/evil")
        env = minimal_subprocess_env()
        assert "LD_PRELOAD" not in env
        assert "PYTHONPATH" not in env
        assert "NODE_PATH" not in env

    def test_extra_keys_pass_through(self, planted_secrets):
        env = minimal_subprocess_env(extra_keys=("GITHUB_TOKEN",))
        assert env.get("GITHUB_TOKEN") == "ghp_secret"
        assert "OPENAI_API_KEY" not in env

    def test_overrides_apply(self):
        env = minimal_subprocess_env(overrides={"GH_PROMPT_DISABLED": "1"})
        assert env["GH_PROMPT_DISABLED"] == "1"


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX 'env' binary")
class TestShellChildEnvBehavioral:
    def test_run_command_sandbox_child_sees_no_secrets(self, planted_secrets):
        from mcp_server.tools.shell import handle_run_command
        out = asyncio.run(handle_run_command({"cmd": "env"}))
        assert "exit_code: 0" in out
        _assert_no_secrets(out)

    def test_run_command_non_sandbox_child_sees_no_secrets(
        self, planted_secrets, monkeypatch
    ):
        import mcp_server.tools.shell as shell
        monkeypatch.setattr(shell, "SHELL_SANDBOX_MODE", False)
        out = asyncio.run(shell.handle_run_command({"cmd": "env"}))
        assert "exit_code: 0" in out
        _assert_no_secrets(out)
        assert "PATH=" in out, "the allowlisted basics must still be present"

    def test_run_python_child_sees_no_secrets(self, planted_secrets):
        from mcp_server.tools.code import handle_run_python
        out = asyncio.run(handle_run_python({
            "code": "import os; print(sorted(os.environ))",
        }))
        _assert_no_secrets(out)

    def test_git_still_works_under_minimal_env(
        self, planted_secrets, tmp_path, monkeypatch
    ):
        """git spawns with the minimal env and must keep functioning
        (HOME for ~/.gitconfig and PATH are in the allowlist).

        Uses a freshly `git init`-ed tmp repo instead of os.getcwd(): the old
        version ran `git status` in whatever directory pytest happened to be
        launched from, and failed when the suite ran outside a git repo.
        """
        import subprocess

        import mcp_server.tools.git as git_mod

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        # Point the handler's project root at the tmp repo; validate_path is
        # scoped to the configured root, so bypass it for the tmp dir.
        monkeypatch.setattr(git_mod, "PROJECT_ROOT", repo)
        monkeypatch.setattr(git_mod, "validate_path", lambda p: p)

        out = asyncio.run(git_mod._run_git("status", "--short"))
        assert "exit_code: 0" in out


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX 'sh' binary")
class TestFullStackEnvThroughServer:
    def test_call_tool_run_command_env_dump_is_clean(
        self, planted_secrets, monkeypatch
    ):
        """The whole path: call_tool → policy(allow: env is read-only) →
        handler → real child. The child's env dump must be secret-free."""
        monkeypatch.delenv("KIM_HITL_RISK_THRESHOLD", raising=False)
        from mcp_server import server as srv
        result = asyncio.run(srv.call_tool("run_command", {"cmd": "env"}))
        text = result.content[0].text
        assert "exit_code: 0" in text
        _assert_no_secrets(text)

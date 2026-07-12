"""
F-K-8 regression pack: the git tool's path-validation gate had ZERO test
coverage. `_validate_git_paths` (mcp_server/tools/git.py) is what stops
git_diff / git_add / etc. from touching paths outside ALLOWED_PATHS and, via
config.validate_path, the secret-file sandbox that CLAUDE.md marks as a standing
constraint. Before this pack a refactor could silently drop the validate_path
call and every suite would stay green.

Two guarantees per handler:
  1. A path outside ALLOWED_PATHS / a secret file → PERMISSION_ERROR and git is
     NEVER spawned (asserted by patching _run_git and checking it wasn't called).
  2. A benign in-repo path passes the gate and reaches _run_git.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from mcp_server.config import PROJECT_ROOT
from mcp_server.tools import git as git_tool
from mcp_server.tools.git import (
    _validate_git_paths,
    handle_git_add,
    handle_git_checkout,
    handle_git_diff,
)


# ── _validate_git_paths unit coverage (lines 36-46, previously 0%) ────────────


def test_validate_git_paths_allows_in_repo_relative():
    assert _validate_git_paths(["README.md"], str(PROJECT_ROOT)) is None


def test_validate_git_paths_allows_multiple_in_repo():
    assert _validate_git_paths(["a.py", "sub/b.py"], str(PROJECT_ROOT)) is None


def test_validate_git_paths_skips_flag_tokens():
    # Flags and the '--' pathspec separator must be skipped, not validated.
    assert _validate_git_paths(["--cached", "--", "README.md"], str(PROJECT_ROOT)) is None


def test_validate_git_paths_skips_empty_token():
    assert _validate_git_paths([""], str(PROJECT_ROOT)) is None


def test_validate_git_paths_rejects_absolute_outside_repo():
    rejection = _validate_git_paths(["/etc/passwd"], str(PROJECT_ROOT))
    assert rejection is not None and "PERMISSION_ERROR" in rejection


def test_validate_git_paths_rejects_parent_traversal():
    rejection = _validate_git_paths(["../../../../etc/passwd"], str(PROJECT_ROOT))
    assert rejection is not None and "PERMISSION_ERROR" in rejection


def test_validate_git_paths_rejects_secret_file_in_repo():
    # A path INSIDE the repo but matching a secret-file glob (.env / id_rsa /
    # *.pem / credentials) must still be refused — the CLAUDE.md constraint.
    for secret in [".env", "id_rsa", "server.pem", "aws_credentials.json"]:
        rejection = _validate_git_paths([secret], str(PROJECT_ROOT))
        assert rejection is not None and "PERMISSION_ERROR" in rejection, (
            f"secret file {secret!r} should be refused"
        )


# ── Handler-level: rejection happens BEFORE git is spawned ────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_path", [
    "/etc/passwd",
    "../../../../etc/passwd",
    ".env",
    "id_rsa",
])
async def test_git_diff_rejects_bad_path_without_spawning(bad_path):
    with patch.object(git_tool, "_run_git", new=AsyncMock(return_value="")) as run:
        result = await handle_git_diff({"path": bad_path, "cwd": str(PROJECT_ROOT)})
    assert "PERMISSION_ERROR" in result
    run.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_path", [
    "/etc/passwd",
    "../../secret",
    ".env",
])
async def test_git_add_rejects_bad_path_without_spawning(bad_path):
    with patch.object(git_tool, "_run_git", new=AsyncMock(return_value="")) as run:
        result = await handle_git_add({"paths": bad_path, "cwd": str(PROJECT_ROOT)})
    assert "PERMISSION_ERROR" in result
    run.assert_not_called()


@pytest.mark.asyncio
async def test_git_add_rejects_one_bad_path_in_list_without_spawning():
    with patch.object(git_tool, "_run_git", new=AsyncMock(return_value="")) as run:
        result = await handle_git_add(
            {"paths": ["good.py", "/etc/passwd"], "cwd": str(PROJECT_ROOT)}
        )
    assert "PERMISSION_ERROR" in result
    run.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_target", [
    "/etc/passwd",
    "../escape",
    "-c",            # option-injection into checkout
    "\\\\server\\share",
])
async def test_git_checkout_rejects_escape_target_without_spawning(bad_target):
    with patch.object(git_tool, "_run_git", new=AsyncMock(return_value="")) as run:
        result = await handle_git_checkout({"target": bad_target, "cwd": str(PROJECT_ROOT)})
    assert "PERMISSION_ERROR" in result
    run.assert_not_called()


# ── Handler-level: benign paths pass the gate and reach _run_git ──────────────


@pytest.mark.asyncio
async def test_git_diff_benign_path_reaches_run_git():
    with patch.object(git_tool, "_run_git", new=AsyncMock(return_value="exit_code: 0")) as run:
        result = await handle_git_diff({"path": "README.md", "cwd": str(PROJECT_ROOT)})
    run.assert_awaited_once()
    # the '--' pathspec separator + the path are forwarded
    assert run.await_args.args[-2:] == ("--", "README.md")
    assert "exit_code: 0" in result


@pytest.mark.asyncio
async def test_git_add_benign_paths_reach_run_git():
    with patch.object(git_tool, "_run_git", new=AsyncMock(return_value="exit_code: 0")) as run:
        result = await handle_git_add({"paths": ["a.py", "b.py"], "cwd": str(PROJECT_ROOT)})
    run.assert_awaited_once()
    assert "a.py" in run.await_args.args and "b.py" in run.await_args.args
    assert "exit_code: 0" in result


@pytest.mark.asyncio
async def test_git_checkout_benign_branch_reaches_run_git():
    with patch.object(git_tool, "_run_git", new=AsyncMock(return_value="exit_code: 0")) as run:
        result = await handle_git_checkout({"target": "feature/x", "cwd": str(PROJECT_ROOT)})
    run.assert_awaited_once()
    assert "feature/x" in run.await_args.args
    assert "exit_code: 0" in result


@pytest.mark.asyncio
async def test_git_status_and_log_reach_run_git():
    from mcp_server.tools.git import handle_git_log, handle_git_status
    with patch.object(git_tool, "_run_git", new=AsyncMock(return_value="exit_code: 0")) as run:
        await handle_git_status({"cwd": str(PROJECT_ROOT)})
        await handle_git_log({"cwd": str(PROJECT_ROOT), "n": 5})
    assert run.await_count == 2

"""K6: approval-request preview builder (moved server-side with the K1 gate)."""

import pytest

from mcp_server import config
from mcp_server.policy import build_approval_preview

build = build_approval_preview


@pytest.fixture
def allowed_tmp(tmp_path):
    """Grant tmp_path as an allowed root: the write_file/edit_file preview
    branches re-apply validate_path (D3 hardening), so preview tests that
    read/write real files must run inside the sandbox."""
    orig = list(config.ALLOWED_PATHS)
    config.ALLOWED_PATHS.append(tmp_path.resolve())
    yield tmp_path
    config.ALLOWED_PATHS[:] = orig


def test_run_command_preview_is_the_command():
    assert build("run_command", {"command": "rm -rf build"}) == "rm -rf build"


def test_write_file_preview_is_unified_diff_for_new_file(allowed_tmp):
    out = build("write_file", {"path": str(allowed_tmp / "x.txt"), "content": "hello\nworld"})
    assert "+hello" in out
    assert "+world" in out


def test_write_file_preview_diffs_existing(allowed_tmp):
    f = allowed_tmp / "y.txt"
    f.write_text("a\nb\n")
    out = build("write_file", {"path": str(f), "content": "a\nB\n"})
    assert "-b" in out and "+B" in out


def test_write_file_diff_capped_at_40_lines(allowed_tmp):
    out = build("write_file", {"path": str(allowed_tmp / "big.txt"), "content": "\n".join(str(i) for i in range(200))})
    assert "diff truncated" in out
    assert len(out.splitlines()) <= 41


def test_web_preview_has_url_and_label():
    out = build("web_click", {"url": "https://ex.com", "label": "Login button"})
    assert "https://ex.com" in out
    assert "Login button" in out


def test_unknown_tool_empty_preview():
    assert build("read_file", {"path": "/x"}) == ""


# ── D3 hardening, write_file branch (mirrors the edit_file fixes) ──────────


SECRET = "OPENAI_API_KEY=sk-verysecret"


def test_write_file_preview_denied_for_secret_path(allowed_tmp):
    env_file = allowed_tmp / ".env"
    env_file.write_text(SECRET + "\n")
    out = build("write_file", {"path": str(env_file), "content": "OPENAI_API_KEY=other"})
    assert out == ""


def test_write_file_preview_denied_outside_allowed_paths(tmp_path):
    # tmp_path NOT granted as an allowed root here — preview must render nothing.
    f = tmp_path / "z.txt"
    f.write_text(SECRET + "\n")
    out = build("write_file", {"path": str(f), "content": "new"})
    assert out == ""


def test_write_file_preview_ignores_file_path_alias(allowed_tmp):
    secret_file = allowed_tmp / "notes.txt"
    secret_file.write_text(SECRET + "\n")
    # Empty declared path + real target smuggled in an alias arg the policy
    # path table doesn't cover: the alias must never be read.
    out = build("write_file", {
        "path": "",
        "file_path": str(secret_file),
        "content": "attacker-chosen",
    })
    assert "OPENAI_API_KEY" not in out

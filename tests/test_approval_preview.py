"""K6: approval-request preview builder."""

from orchestrator.agent import KimAgent

build = KimAgent._build_approval_preview


def test_run_command_preview_is_the_command():
    assert build("run_command", {"command": "rm -rf build"}) == "rm -rf build"


def test_write_file_preview_is_unified_diff_for_new_file(tmp_path):
    out = build("write_file", {"path": str(tmp_path / "x.txt"), "content": "hello\nworld"})
    assert "+hello" in out
    assert "+world" in out


def test_write_file_preview_diffs_existing(tmp_path):
    f = tmp_path / "y.txt"
    f.write_text("a\nb\n")
    out = build("write_file", {"path": str(f), "content": "a\nB\n"})
    assert "-b" in out and "+B" in out


def test_write_file_diff_capped_at_40_lines(tmp_path):
    out = build("write_file", {"path": str(tmp_path / "big.txt"), "content": "\n".join(str(i) for i in range(200))})
    assert "diff truncated" in out
    assert len(out.splitlines()) <= 41


def test_web_preview_has_url_and_label():
    out = build("web_click", {"url": "https://ex.com", "label": "Login button"})
    assert "https://ex.com" in out
    assert "Login button" in out


def test_unknown_tool_empty_preview():
    assert build("read_file", {"path": "/x"}) == ""

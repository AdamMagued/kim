"""Comprehensive unit test suite for all model call parsers, diff patch engines, and response salvagers.

Ensures every legal output shape emitted by LLMs (ChatGPT, Claude, Gemini, DeepSeek)
is cleanly parsed, extracted, sanitized, and executed without failures or truncation.
"""

import pytest
import re
import os
import tempfile
import json

from codex_engine.patch_engine import apply_git_patch, sanitize_patch
from codex_engine.engine import (
    _extract_shell_blocks,
    _extract_json_tool_fences,
    _extract_file_directive,
    _file_directive_tool_calls,
    _is_done_reply,
    _strip_done_marker,
    _provider_response_to_responses_api,
    _salvage_action_reply,
)
from codex_engine.responses_streaming import _clean_reasoning_stream


# ═════════════════════════════════════════════════════════════════════════════
# 1. apply_patch & Unified Diff Parsing Tests
# ═════════════════════════════════════════════════════════════════════════════

class TestPatchEngineParsers:

    def test_sanitize_patch_context_spacing_and_counts(self):
        raw_patch = """--- a/test.txt
+++ b/test.txt
@@ -1,3 +1,3 @@
context line 1
-old line
+new line
context line 2"""

        sanitized = sanitize_patch(raw_patch)
        assert "--- a/test.txt" in sanitized
        assert "+++ b/test.txt" in sanitized
        assert "@@ -1,3 +1,3 @@" in sanitized
        # Check context lines were prefixed with leading space
        assert " context line 1" in sanitized
        assert " context line 2" in sanitized

    def test_apply_git_patch_apply_patch_fence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Initialize git repo in tmpdir
            os.system(f"git -C '{tmpdir}' init -q && touch '{tmpdir}/test.txt' && git -C '{tmpdir}' add . && git -C '{tmpdir}' commit -m 'init' -q")

            patch_text = """Here is the fix for your bug:

```apply_patch
--- a/test.txt
+++ b/test.txt
@@ -0,0 +1,2 @@
+hello world
+second line
```
"""
            res = apply_git_patch(tmpdir, patch_text)
            assert res.get("success") is True, f"Failed to apply patch: {res}"
            with open(os.path.join(tmpdir, "test.txt"), "r") as f:
                content = f.read()
            assert "hello world" in content
            assert "second line" in content

    def test_apply_git_patch_begin_patch_format(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.system(f"git -C '{tmpdir}' init -q && touch '{tmpdir}/test.txt' && git -C '{tmpdir}' add . && git -C '{tmpdir}' commit -m 'init' -q")
            patch_text = """*** Begin Patch
*** Update File: test.txt
@@ -0,0 +1,2 @@
+hello world
+second line
*** End Patch"""
            res = apply_git_patch(tmpdir, patch_text)
            assert res.get("success") is True, f"Failed to apply patch: {res}"
            with open(os.path.join(tmpdir, "test.txt"), "r") as f:
                content = f.read()
            assert "hello world" in content

    def test_apply_git_patch_multifile_begin_patch_format(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.system(f"git -C '{tmpdir}' init -q && echo 'original 1' > '{tmpdir}/file1.txt' && echo 'to delete' > '{tmpdir}/file3.txt' && git -C '{tmpdir}' add . && git -C '{tmpdir}' commit -m 'init' -q")
            patch_text = """*** Begin Patch
*** Update File: file1.txt
@@ -1,1 +1,2 @@
 original 1
+added line
*** Add File: file2.txt
@@ -0,0 +1,1 @@
+new file 2 content
*** Delete File: file3.txt
@@ -1,1 +0,0 @@
-to delete
*** End Patch"""
            res = apply_git_patch(tmpdir, patch_text)
            assert res.get("success") is True, f"Failed to apply multi-file patch: {res}"
            with open(os.path.join(tmpdir, "file1.txt"), "r") as f:
                c1 = f.read()
            assert "added line" in c1
            with open(os.path.join(tmpdir, "file2.txt"), "r") as f:
                c2 = f.read()
            assert "new file 2 content" in c2
            assert not os.path.exists(os.path.join(tmpdir, "file3.txt"))

    def test_apply_git_patch_bare_hunk_header(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.system(f"git -C '{tmpdir}' init -q && touch '{tmpdir}/test.txt' && git -C '{tmpdir}' add . && git -C '{tmpdir}' commit -m 'init' -q")
            patch_text = """*** Begin Patch
*** Update File: test.txt
@@
+bare header addition
*** End Patch"""
            res = apply_git_patch(tmpdir, patch_text)
            assert res.get("success") is True, f"Failed to apply patch with bare @@ header: {res}"
            with open(os.path.join(tmpdir, "test.txt"), "r") as f:
                content = f.read()
            assert "bare header addition" in content

    def test_apply_git_patch_diff_fence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.system(f"git -C '{tmpdir}' init -q && echo 'foo' > '{tmpdir}/foo.txt' && git -C '{tmpdir}' add . && git -C '{tmpdir}' commit -m 'init' -q")

            patch_text = """```diff
--- a/foo.txt
+++ b/foo.txt
@@ -1,1 +1,1 @@
-foo
+bar
```"""
            res = apply_git_patch(tmpdir, patch_text)
            assert res.get("success") is True
            with open(os.path.join(tmpdir, "foo.txt"), "r") as f:
                assert f.read().strip() == "bar"

    def test_apply_git_patch_patch_fence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.system(f"git -C '{tmpdir}' init -q && echo 'line1' > '{tmpdir}/file.py' && git -C '{tmpdir}' add . && git -C '{tmpdir}' commit -m 'init' -q")

            patch_text = """```patch
--- a/file.py
+++ b/file.py
@@ -1,1 +1,2 @@
 line1
+line2
```
*** End Patch"""
            res = apply_git_patch(tmpdir, patch_text)
            assert res.get("success") is True
            with open(os.path.join(tmpdir, "file.py"), "r") as f:
                assert "line2" in f.read()


# ═════════════════════════════════════════════════════════════════════════════
# 2. Shell Command Block Extraction Tests (_extract_shell_blocks)
# ═════════════════════════════════════════════════════════════════════════════

class TestShellBlockExtraction:

    def test_extract_bash_fence(self):
        text = "I will check the workspace status.\n```bash\nnpm run test\n```"
        blocks = _extract_shell_blocks(text)
        assert blocks == ["npm run test"]

    def test_extract_sh_fence(self):
        text = "Running build script:\n```sh\nls -la && pwd\n```"
        blocks = _extract_shell_blocks(text)
        assert blocks == ["ls -la && pwd"]

    def test_extract_zsh_fence(self):
        text = "```zsh\npython3 -m pytest\n```"
        blocks = _extract_shell_blocks(text)
        assert blocks == ["python3 -m pytest"]

    def test_filter_no_op_commands(self):
        text = "```bash\n:\n```"
        assert _extract_shell_blocks(text) == []
        text2 = "```bash\ntrue\n```"
        assert _extract_shell_blocks(text2) == []

    def test_bare_safe_command_fallback(self):
        text = "I will verify directory.\npwd"
        blocks = _extract_shell_blocks(text)
        assert blocks == ["pwd"]

    def test_indented_multiline_python_command_extraction(self):
        text = "python3 -c 'import os; print(os.getcwd())'"
        blocks = _extract_shell_blocks(text)
        assert len(blocks) == 1
        assert "python3 -c" in blocks[0]


# ═════════════════════════════════════════════════════════════════════════════
# 3. JSON Tool Call Extraction Tests (_extract_json_tool_fences)
# ═════════════════════════════════════════════════════════════════════════════

class TestJsonToolFenceExtraction:

    def test_structured_tool_calls(self):
        text = """```json
{
  "tool_calls": [
    {"name": "exec", "input": {"cmd": "git status"}}
  ]
}
```"""
        calls = _extract_json_tool_fences(text)
        assert len(calls) == 1
        assert calls[0]["name"] == "exec"
        assert calls[0]["input"]["cmd"] == "git status"

    def test_single_tool_call(self):
        text = """```json
{"name": "exec", "input": {"cmd": "npm test"}}
```"""
        calls = _extract_json_tool_fences(text)
        assert len(calls) == 1
        assert calls[0]["name"] == "exec"

    def test_bare_exec_cmd_dict(self):
        text = """```json
{"cmd": "npm run build", "workdir": "apps/web"}
```"""
        calls = _extract_json_tool_fences(text)
        assert len(calls) == 1
        assert calls[0]["name"] == "exec"
        assert calls[0]["input"]["cmd"] == "npm run build"


# ═════════════════════════════════════════════════════════════════════════════
# 4. File Directive Extraction Tests (_extract_file_directive)
# ═════════════════════════════════════════════════════════════════════════════

class TestFileDirectiveExtraction:

    def test_save_this_as_directive(self):
        text = """Save this as index.html:

```html
<h1>Hello World</h1>
```

Then open it in your browser.
"""
        res = _extract_file_directive(text)
        assert res is not None
        fname, body, wants_open = res
        assert fname == "index.html"
        assert "<h1>Hello World</h1>" in body
        assert wants_open is True

        calls = _file_directive_tool_calls(fname, body, wants_open)
        assert len(calls) == 2
        assert calls[0]["name"] == "exec"
        assert "cat > index.html" in calls[0]["input"]["cmd"]
        assert "open index.html" in calls[1]["input"]["cmd"] or "start index.html" in calls[1]["input"]["cmd"] or "xdg-open index.html" in calls[1]["input"]["cmd"]

    def test_file_named_directive(self):
        text = """File named app.py:

```python
print("Hello")
```"""
        res = _extract_file_directive(text)
        assert res is not None
        fname, body, wants_open = res
        assert fname == "app.py"
        assert body == 'print("Hello")'
        assert wants_open is False


# ═════════════════════════════════════════════════════════════════════════════
# 5. DONE Signal & Summary Line Tests
# ═════════════════════════════════════════════════════════════════════════════

class TestDoneSignalParser:

    def test_done_signal_detection(self):
        text = "All tests passed and build verified.\nDONE"
        assert _is_done_reply(text) is True
        assert _strip_done_marker(text) == "All tests passed and build verified."

    def test_done_lowercase_detection(self):
        text = "Done."
        assert _is_done_reply(text) is True
        assert _strip_done_marker(text) == "Done."

    def test_done_rejected_when_file_write_present(self):
        text = "cat > index.html << 'EOF'\nhi\nEOF\nDONE"
        assert _is_done_reply(text) is False


# ═════════════════════════════════════════════════════════════════════════════
# 6. Stream Reasoning Cleaner Tests (_clean_reasoning_stream)
# ═════════════════════════════════════════════════════════════════════════════

class TestStreamReasoningCleaner:

    def test_strips_codex_agent_system_prompt(self):
        raw = """• # Codex Agent — Tool & Output Format Reference

  This document defines exactly how the Codex agent must format commands...
  Hi Adam — I’ll confirm the workspace is ready.

  ```bash
  pwd
  ```"""
        cleaned = _clean_reasoning_stream(raw)
        assert "# Codex Agent" not in cleaned
        assert "This document defines" not in cleaned
        assert "Hi Adam" in cleaned
        assert "```bash" in cleaned
        assert "pwd" in cleaned

    def test_preserves_markdown_code_blocks(self):
        raw = """Here is the code change:

```apply_patch
--- a/test.ts
+++ b/test.ts
@@ -1,1 +1,1 @@
-const a = 1;
+const a = 2;
```"""
        cleaned = _clean_reasoning_stream(raw)
        assert "```apply_patch" in cleaned
        assert "const a = 2;" in cleaned

    def test_strips_filecite_headers(self):
        raw = "in your response to cite this file, or to surface it as a link.\nHi there!"
        cleaned = _clean_reasoning_stream(raw)
        assert "in your response to cite this file" not in cleaned
        assert cleaned == "Hi there!"


# ═════════════════════════════════════════════════════════════════════════════
# 7. Responses API Contract Translation Integration Test
# ═════════════════════════════════════════════════════════════════════════════

class TestProviderResponseTranslation:

    def test_translate_apply_patch_response(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.system(f"git -C '{tmpdir}' init -q && echo 'old' > '{tmpdir}/f.txt' && git -C '{tmpdir}' add . && git -C '{tmpdir}' commit -m 'init' -q")

            # Change cwd temporarily to tmpdir
            orig_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                provider_resp = {
                    "type": "text",
                    "content": """Updating server port.

```apply_patch
--- a/f.txt
+++ b/f.txt
@@ -1,1 +1,1 @@
-old
+new
```"""
                }
                reply = _provider_response_to_responses_api(provider_resp, 1)
                assert reply["object"] == "response"
                assert reply["status"] == "completed"
                assert len(reply["output"]) > 0
                with open(os.path.join(tmpdir, "f.txt"), "r") as f:
                    assert f.read().strip() == "new"
            finally:
                os.chdir(orig_cwd)

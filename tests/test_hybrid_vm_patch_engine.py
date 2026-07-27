import os
import tempfile
import pytest
from codex_engine.router import classify_task_mode
from codex_engine.patch_engine import create_workspace_zip, apply_git_patch


def test_classify_task_mode():
    assert classify_task_mode("refactor the auth page and write vitest suite") == "PATCH"
    assert classify_task_mode("fix bug in test-health-check.ts") == "PATCH"
    assert classify_task_mode("open google chrome and check system uptime") == "LOCAL"
    assert classify_task_mode("brew install ffmpeg and screencapture desktop") == "LOCAL"


def test_create_workspace_zip(tmp_path):
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "index.js").write_text("console.log('hello');")

    zip_path = create_workspace_zip(str(tmp_path))
    assert os.path.exists(zip_path)
    assert os.path.getsize(zip_path) > 0
    os.remove(zip_path)


def test_apply_git_patch(tmp_path):
    # Initialize a dummy git repo
    import subprocess
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), capture_output=True)

    file_a = tmp_path / "hello.txt"
    file_a.write_text("Hello World\n")
    subprocess.run(["git", "add", "hello.txt"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(tmp_path), capture_output=True)

    patch_text = """```diff
diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-Hello World
+Hello Hybrid Engine
```"""

    res = apply_git_patch(str(tmp_path), patch_text)
    assert res["success"] is True
    assert file_a.read_text().strip() == "Hello Hybrid Engine"

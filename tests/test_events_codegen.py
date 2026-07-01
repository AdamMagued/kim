"""Contract tests for the schema-generated Python IPC encoders."""

from __future__ import annotations

import json

from orchestrator import events_gen


def test_generated_emitters_preserve_wire_shapes(capsys):
    events_gen.emit_status("working")
    events_gen.emit_tool("read_file", {})
    events_gen.emit_answer("done")
    events_gen.emit_diff("main.py", 2, 1)

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines == [
        {"type": "status", "message": "working"},
        {"type": "tool", "name": "read_file", "args": {}},
        {"type": "answer", "text": "done"},
        {"type": "diff", "path": "main.py", "added": 2, "removed": 1},
    ]


def test_generated_legacy_manifest_covers_text_protocol():
    expected = {
        "[STATUS]",
        "[PLAN]",
        "[STEP]",
        "[DONE]",
        "[CONTEXT]",
        "[STATS]",
        "[TOOL]",
        "[ANSWER]",
        "[DIFF]",
        "[SUCCESS]",
        "[FAILED]",
        "[ERROR]",
        "TASK_COMPLETE:",
        "NEED_HELP:",
    }
    assert expected == set(events_gen.LEGACY_LOG_TAGS)

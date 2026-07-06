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


def test_generated_named_tag_constants_match_manifest():
    """K5: every legacy tag has a generated LOG_TAG_<NAME> constant whose value
    is the tag string — the constants emitters must use instead of literals."""
    for tag in events_gen.LEGACY_LOG_TAGS:
        name = "LOG_TAG_" + tag.strip("[]:").replace("[", "").replace("]", "")
        assert getattr(events_gen, name) == tag


def test_codex_emitters_use_generated_tag_constants():
    """The codex engine + bridge service source the bracket vocabulary from
    events_gen (no re-typed '[STATUS] ' style print literals)."""
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    engine = (repo / "codex_engine" / "engine.py").read_text()
    bridge = (repo / "orchestrator" / "codex_bridge_service.py").read_text()
    assert "LOG_TAG_STATUS" in engine and "LOG_TAG_ANSWER" in engine
    assert "LOG_TAG_FAILED" in bridge and "LOG_TAG_TASK_COMPLETE" in bridge
    for src, fname in [(engine, "engine.py"), (bridge, "codex_bridge_service.py")]:
        for literal in ('print("[STATUS]', 'print(f"[STATUS]', 'print("[FAILED]', 'print(f"[FAILED]'):
            assert literal not in src, f"{fname} re-types a bracket literal: {literal}"

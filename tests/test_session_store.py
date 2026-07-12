import ast
import json
import tempfile
from pathlib import Path
from datetime import date
from orchestrator.session_store import SessionStore, _strip_images_for_disk

_AGENT_PY = Path(__file__).resolve().parent.parent / "orchestrator" / "agent.py"
_CLI_PY = Path(__file__).resolve().parent.parent / "orchestrator" / "cli.py"


def test_agent_calls_complete_run_on_all_termination_paths():
    """_complete_run must be the sole call site for appending run_result records.

    A static scan verifies that make_run_result() is always wrapped by
    _complete_run(), so no termination path can silently skip the JSONL append.
    """
    source = _AGENT_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Collect every Call node that is `make_run_result(...)`
    bare_returns = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return):
            continue
        value = node.value
        if value is None:
            continue
        # Return of a bare make_run_result call (not wrapped)
        if isinstance(value, ast.Call):
            func = value.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name == "make_run_result":
                bare_returns.append(node.lineno)

    assert bare_returns == [], (
        f"make_run_result() returned directly (not via _complete_run) at lines {bare_returns}. "
        "All termination paths must go through self._complete_run(make_run_result(...))."
    )

    assert "_complete_run" in source, "_complete_run helper not found in agent.py"
    assert "append_run_result" in source, "append_run_result not called anywhere in agent.py"


def test_complete_run_guards_against_io_error():
    """_complete_run must return the result dict even if append_run_result raises.

    A session-store I/O failure (e.g. full disk) must never prevent the caller
    from receiving the run result — the agent should degrade gracefully.
    """
    source = _AGENT_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Find the _complete_run function body in the AST
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_complete_run":
            body_src = ast.get_source_segment(source, node) or ""
            assert "try:" in body_src, (
                "_complete_run must wrap append_run_result in try/except so "
                "I/O failures never propagate out of agent.run()"
            )
            assert "except" in body_src, (
                "_complete_run must catch exceptions from append_run_result"
            )
            return

    raise AssertionError("_complete_run not found in agent.py")

def test_session_store_jsonl_write_and_read():
    with tempfile.TemporaryDirectory() as tmp_dir:
        base_path = Path(tmp_dir)
        store = SessionStore(base_dir=base_path, session_id="test_session")
        
        # Verify date-bucketed file structure
        today_str = date.today().isoformat()
        expected_jsonl_path = base_path / today_str / "test_session.jsonl"
        expect_summary_path = base_path / today_str / "test_session.summary.txt"
        
        assert store.session_file == expected_jsonl_path
        assert store.summary_file == expect_summary_path
        
        # Write messages
        msg1 = {"role": "user", "content": "Hello"}
        msg2 = {"role": "assistant", "content": "Hi there!"}
        
        store.append_message(msg1)
        store.append_message(msg2)
        
        # Read back using load_session
        loaded = SessionStore.load_session("test_session", base_dir=base_path)
        assert len(loaded) == 2
        assert loaded[0] == msg1
        assert loaded[1] == msg2
        
        # Write and read summary
        store.save_summary("This is a test summary.")
        assert store.summary_file.read_text().strip() == "This is a test summary."


def test_flush_exists_and_does_not_raise():
    """SessionStore.flush() must be callable without error.

    agent.py calls self._session_store.flush() when the model emits
    task_complete as a native tool call (line 576).  Without this method
    the path raises AttributeError, turning a successful task completion
    into an error result at the task_queue boundary.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = SessionStore(base_dir=Path(tmp_dir), session_id="flush_test")
        store.append_message({"role": "user", "content": "test"})
        store.flush()  # must not raise
        # Verify messages written before flush are still readable
        loaded = SessionStore.load_session("flush_test", base_dir=Path(tmp_dir))
        assert len(loaded) == 1


def test_flush_does_not_corrupt_session():
    """Calling flush() must not lose or corrupt already-written messages."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="flush_corr")
        for i in range(5):
            store.append_message({"role": "user", "content": f"msg {i}"})
        store.flush()
        loaded = SessionStore.load_session("flush_corr", base_dir=base)
        assert len(loaded) == 5
        assert loaded[2]["content"] == "msg 2"


def test_append_run_result_writes_typed_record():
    """append_run_result must append a {"type": "run_result"} line to the session JSONL."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="rr_typed")
        store.append_message({"role": "user", "content": "do the thing"})
        store.append_run_result({"success": True, "termination": "task_complete", "summary": "Done.", "screenshot": ""})

        lines = store.session_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        record = json.loads(lines[1])
        assert record["type"] == "run_result"
        assert record["success"] is True
        assert record["termination"] == "task_complete"
        assert record["summary"] == "Done."
        assert "session_id" in record
        assert "completed_at" in record


def test_append_run_result_strips_screenshot_to_flag():
    """append_run_result must not store screenshot bytes, only a had_screenshot bool."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="rr_screen")
        big_b64 = "A" * 10000
        store.append_run_result({"success": True, "termination": "task_complete", "summary": "x", "screenshot": big_b64})

        lines = store.session_file.read_text(encoding="utf-8").strip().split("\n")
        record = json.loads(lines[0])
        assert "screenshot" not in record
        assert record["had_screenshot"] is True
        # File should be much smaller than the raw b64
        assert store.session_file.stat().st_size < 500


def test_append_run_result_had_screenshot_false_when_empty():
    """had_screenshot must be False when the screenshot field is absent or empty."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="rr_noscreen")
        store.append_run_result({"success": False, "termination": "cancelled", "summary": "Stopped."})

        lines = store.session_file.read_text(encoding="utf-8").strip().split("\n")
        record = json.loads(lines[0])
        assert record["had_screenshot"] is False


def test_append_run_result_tolerates_missing_termination():
    """Old result dicts without 'termination' must not raise; termination stored as None."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="rr_notermkey")
        store.append_run_result({"success": False, "summary": "auth failed", "screenshot": ""})

        lines = store.session_file.read_text(encoding="utf-8").strip().split("\n")
        record = json.loads(lines[0])
        assert record["termination"] is None
        assert record["success"] is False


def test_load_session_includes_run_result_record():
    """load_session must return run_result records so callers can inspect them."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="rr_load")
        store.append_message({"role": "user", "content": "task"})
        store.append_message({"role": "assistant", "content": "TASK_COMPLETE: done"})
        store.append_run_result({"success": True, "termination": "task_complete", "summary": "done", "screenshot": ""})

        all_records = SessionStore.load_session("rr_load", base_dir=base)
        assert len(all_records) == 3
        last = all_records[-1]
        assert last["type"] == "run_result"
        assert last["termination"] == "task_complete"


def test_load_from_messages_skips_run_result_records():
    """ConversationMemory.load_from_messages must ignore records without a role key.

    A run_result record in a resumed session JSONL must not end up in the message
    stack — get_messages() does m["role"] and would raise KeyError otherwise.
    """
    from orchestrator.memory import ConversationMemory
    mem = ConversationMemory()
    records = [
        {"role": "user", "content": "do the task"},
        {"role": "assistant", "content": "TASK_COMPLETE: done"},
        {"type": "run_result", "session_id": "x", "success": True, "termination": "task_complete"},
    ]
    mem.load_from_messages(records)
    msgs = mem.get_messages()
    assert len(msgs) == 2
    assert all("role" in m for m in msgs)
    assert not any(m.get("type") == "run_result" for m in msgs)


def test_cli_does_not_call_append_run_result_directly():
    """cli.py must not call append_run_result after agent.run().

    agent.run() now persists every terminal result via _complete_run().
    A direct CLI call would write a duplicate run_result record to the JSONL.
    """
    source = _CLI_PY.read_text(encoding="utf-8")
    assert "append_run_result" not in source, (
        "cli.py calls append_run_result directly, which duplicates the record "
        "already written by agent._complete_run(). Remove the CLI call."
    )


# ---------------------------------------------------------------------------
# run_started lifecycle event tests
# ---------------------------------------------------------------------------

def test_append_run_started_writes_typed_record():
    """append_run_started must append a {"type": "run_started"} line to the session JSONL."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="rs_basic")
        store.append_run_started("open Chrome and navigate to example.com")

        lines = store.session_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["type"] == "run_started"


def test_run_started_record_has_required_fields():
    """run_started record must carry session_id, started_at, task, and cwd."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="rs_fields")
        task_text = "list all python files"
        store.append_run_started(task_text, cwd="/some/project")

        record = json.loads(store.session_file.read_text(encoding="utf-8").strip())
        assert record["session_id"] == "rs_fields"
        assert record["task"] == task_text
        assert record["cwd"] == "/some/project"
        assert "started_at" in record


def test_run_started_at_is_parseable_iso_utc():
    """started_at must be a valid ISO-8601 datetime string (UTC)."""
    from datetime import datetime
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="rs_ts")
        store.append_run_started("check git status")

        record = json.loads(store.session_file.read_text(encoding="utf-8").strip())
        dt = datetime.fromisoformat(record["started_at"])
        assert dt.tzinfo is not None


def test_run_started_never_contains_screenshot_bytes():
    """run_started must not store any screenshot data -- only task text and metadata."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="rs_noscreen")
        store.append_run_started("task with no screenshot")

        record = json.loads(store.session_file.read_text(encoding="utf-8").strip())
        assert "screenshot" not in record
        assert "had_screenshot" not in record
        # Record should be small (no image data)
        assert store.session_file.stat().st_size < 400


def test_run_result_includes_cwd():
    """append_run_result must now include a cwd field for inspectability."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="rr_cwd")
        store.append_run_result(
            {"success": True, "termination": "task_complete", "summary": "done", "screenshot": ""},
            cwd="/explicit/cwd",
        )

        record = json.loads(store.session_file.read_text(encoding="utf-8").strip())
        assert record["cwd"] == "/explicit/cwd"


def test_run_result_cwd_defaults_to_process_cwd():
    """append_run_result must fall back to os.getcwd() when cwd is not supplied."""
    import os
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="rr_cwd_default")
        store.append_run_result({"success": True, "termination": "task_complete", "summary": "x", "screenshot": ""})

        record = json.loads(store.session_file.read_text(encoding="utf-8").strip())
        assert "cwd" in record
        assert record["cwd"] == os.getcwd()


def test_load_from_messages_skips_run_started_records():
    """ConversationMemory.load_from_messages must ignore run_started records on resume.

    A run_started record written at the top of a session JSONL must not end up
    in the message stack when the session is resumed -- load_from_messages filters
    on presence of 'role', so any record without it is dropped.
    """
    from orchestrator.memory import ConversationMemory
    mem = ConversationMemory()
    records = [
        {"type": "run_started", "session_id": "x", "started_at": "2026-01-01T00:00:00+00:00",
         "task": "do the task", "cwd": "/proj"},
        {"role": "user", "content": "do the task"},
        {"role": "assistant", "content": "TASK_COMPLETE: done"},
        {"type": "run_result", "session_id": "x", "success": True, "termination": "task_complete"},
    ]
    mem.load_from_messages(records)
    msgs = mem.get_messages()
    assert len(msgs) == 2
    assert all("role" in m for m in msgs)
    assert not any(m.get("type") in ("run_started", "run_result") for m in msgs)


def test_run_started_and_run_result_bookend_session():
    """Full lifecycle: run_started first, conversation in middle, run_result last."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="rs_bookend")
        store.append_run_started("write a test", cwd="/proj")
        store.append_message({"role": "user", "content": "Task: write a test"})
        store.append_message({"role": "assistant", "content": "TASK_COMPLETE: done"})
        store.append_run_result({"success": True, "termination": "task_complete",
                                  "summary": "done", "screenshot": ""})

        all_records = SessionStore.load_session("rs_bookend", base_dir=base)
        assert len(all_records) == 4
        assert all_records[0]["type"] == "run_started"
        assert all_records[1]["role"] == "user"
        assert all_records[2]["role"] == "assistant"
        assert all_records[3]["type"] == "run_result"


def test_append_tool_event_writes_typed_record():
    """append_tool_event must write a compact {"type": "tool_call"} record."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="tool_basic")
        store.append_tool_event(
            "run_command",
            "completed",
            arg_keys=["cmd", "cwd"],
            duration_ms=123,
            cwd="/proj",
        )

        record = json.loads(store.session_file.read_text(encoding="utf-8").strip())
        assert record["type"] == "tool_call"
        assert record["session_id"] == "tool_basic"
        assert record["tool_name"] == "run_command"
        assert record["phase"] == "completed"
        assert record["arg_keys"] == ["cmd", "cwd"]
        assert record["duration_ms"] == 123
        assert record["cwd"] == "/proj"
        assert "timestamp" in record


def test_tool_event_persists_arg_keys_not_values():
    """Tool traces must never persist argument values or command contents."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="tool_safe")
        store.append_tool_event(
            "run_command",
            "started",
            arg_keys={"cmd": "cat ~/.ssh/id_rsa", "token": "secret-token"}.keys(),
        )

        text = store.session_file.read_text(encoding="utf-8")
        record = json.loads(text.strip())
        assert record["arg_keys"] == ["cmd", "token"]
        assert "cat ~/.ssh/id_rsa" not in text
        assert "secret-token" not in text


def test_tool_event_error_is_truncated():
    """Large error strings are capped so traces stay compact."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="tool_error")
        store.append_tool_event("web_open", "errored", error="x" * 1000)

        record = json.loads(store.session_file.read_text(encoding="utf-8").strip())
        assert record["phase"] == "errored"
        assert len(record["error"]) == 500


def test_load_from_messages_skips_tool_call_records():
    """ConversationMemory.load_from_messages must ignore tool_call traces on resume."""
    from orchestrator.memory import ConversationMemory
    mem = ConversationMemory()
    records = [
        {"role": "user", "content": "do the task"},
        {"type": "tool_call", "tool_name": "run_command", "phase": "started"},
        {"role": "assistant", "content": "TASK_COMPLETE: done"},
    ]
    mem.load_from_messages(records)
    msgs = mem.get_messages()
    assert len(msgs) == 2
    assert all("role" in m for m in msgs)
    assert not any(m.get("type") == "tool_call" for m in msgs)


def test_load_trace_events_returns_only_typed_records():
    """load_trace_events returns run/tool/result records, not chat messages."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="trace_all")
        store.append_run_started("trace this", cwd="/proj")
        store.append_message({"role": "user", "content": "Task: trace this"})
        store.append_tool_event("run_command", "started", arg_keys=["cmd"], cwd="/proj")
        store.append_tool_event("run_command", "completed", duration_ms=5, cwd="/proj")
        store.append_run_result(
            {"success": True, "termination": "task_complete", "summary": "done", "screenshot": ""},
            cwd="/proj",
        )

        events = SessionStore.load_trace_events("trace_all", base_dir=base)
        assert [event["type"] for event in events] == [
            "run_started",
            "tool_call",
            "tool_call",
            "run_result",
        ]
        assert not any(event.get("role") for event in events)


def test_load_trace_events_filters_by_type_tool_and_phase():
    """Trace reader supports narrow filtering without replaying the session."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="trace_filter")
        store.append_run_started("trace filter")
        store.append_tool_event("run_command", "started", arg_keys=["cmd"])
        store.append_tool_event("run_command", "completed", duration_ms=7)
        store.append_tool_event("web_open", "errored", error="boom")
        store.append_run_result(
            {"success": False, "termination": "provider_failed", "summary": "failed", "screenshot": ""}
        )

        tool_events = SessionStore.load_trace_events("trace_filter", base_dir=base, event_type="tool_call")
        assert len(tool_events) == 3

        run_command_events = SessionStore.load_trace_events(
            "trace_filter",
            base_dir=base,
            event_type="tool_call",
            tool_name="run_command",
        )
        assert len(run_command_events) == 2

        completed = SessionStore.load_trace_events(
            "trace_filter",
            base_dir=base,
            event_type="tool_call",
            phase="completed",
        )
        assert len(completed) == 1
        assert completed[0]["tool_name"] == "run_command"


def test_load_trace_events_missing_session_returns_empty_list():
    """Missing sessions should not warn or raise during trace lookup."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        events = SessionStore.load_trace_events("does_not_exist", base_dir=Path(tmp_dir))
        assert events == []


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(record, separators=(",", ":")) for record in records) + "\n",
        encoding="utf-8",
    )


def test_iter_trace_events_reads_across_sessions_newest_date_first():
    """iter_trace_events scans all session JSONL files and attaches source metadata."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        _write_jsonl(base / "2026-06-04" / "old.jsonl", [
            {"role": "user", "content": "old"},
            {"type": "run_started", "task": "old task"},
        ])
        _write_jsonl(base / "2026-06-05" / "new.jsonl", [
            {"type": "run_started", "task": "new task"},
        ])

        events = SessionStore.iter_trace_events(base_dir=base, event_type="run_started")
        assert [event["task"] for event in events] == ["new task", "old task"]
        assert events[0]["session_id"] == "new"
        assert events[0]["date"] == "2026-06-05"
        assert events[1]["session_id"] == "old"
        assert events[1]["date"] == "2026-06-04"


def test_iter_trace_events_filters_by_tool_phase_and_limit():
    """Cross-session trace query supports the same filters plus result limiting."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        _write_jsonl(base / "2026-06-05" / "a.jsonl", [
            {"type": "tool_call", "tool_name": "run_command", "phase": "started"},
            {"type": "tool_call", "tool_name": "run_command", "phase": "completed"},
            {"type": "tool_call", "tool_name": "web_open", "phase": "completed"},
        ])
        _write_jsonl(base / "2026-06-04" / "b.jsonl", [
            {"type": "tool_call", "tool_name": "run_command", "phase": "completed"},
        ])

        events = SessionStore.iter_trace_events(
            base_dir=base,
            event_type="tool_call",
            tool_name="run_command",
            phase="completed",
            limit=1,
        )
        assert len(events) == 1
        assert events[0]["tool_name"] == "run_command"
        assert events[0]["phase"] == "completed"
        assert events[0]["session_id"] == "a"


def test_iter_trace_events_missing_base_returns_empty_list(tmp_path):
    """Missing session base directories should be quiet and empty."""
    assert SessionStore.iter_trace_events(base_dir=tmp_path / "missing") == []


def test_append_llm_event_writes_typed_record_with_usage():
    """append_llm_event must persist compact provider-call metadata."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="llm_basic")
        store.append_llm_event(
            "completed",
            provider="OllamaProvider",
            attempt=2,
            message_count=5,
            tool_count=31,
            duration_ms=42,
            usage={
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_creation_tokens": 30,
                "cache_read_tokens": 70,
                "nested": {"drop": True},
            },
            cwd="/proj",
        )

        record = json.loads(store.session_file.read_text(encoding="utf-8").strip())
        assert record["type"] == "llm_turn"
        assert record["session_id"] == "llm_basic"
        assert record["phase"] == "completed"
        assert record["provider"] == "OllamaProvider"
        assert record["attempt"] == 2
        assert record["message_count"] == 5
        assert record["tool_count"] == 31
        assert record["duration_ms"] == 42
        assert record["usage"]["input_tokens"] == 100
        assert record["usage"]["output_tokens"] == 20
        assert record["usage"]["cache_creation_tokens"] == 30
        assert record["usage"]["cache_read_tokens"] == 70
        assert "nested" not in record["usage"]
        assert record["cwd"] == "/proj"


def test_llm_event_does_not_persist_prompt_or_output_text():
    """LLM traces must not store prompts, tool schemas, or model output text."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="llm_safe")
        store.append_llm_event(
            "errored",
            provider="BrowserProvider",
            attempt=1,
            message_count=3,
            tool_count=4,
            error_code="auth",
            usage={"prompt": "secret prompt", "completion": "secret output", "input": 10},
        )

        text = store.session_file.read_text(encoding="utf-8")
        record = json.loads(text.strip())
        assert record["error_code"] == "auth"
        assert record["usage"] == {"input": 10}
        assert "secret prompt" not in text
        assert "secret output" not in text


def test_load_from_messages_skips_llm_turn_records():
    """ConversationMemory.load_from_messages must ignore llm_turn traces on resume."""
    from orchestrator.memory import ConversationMemory
    mem = ConversationMemory()
    records = [
        {"role": "user", "content": "do the task"},
        {"type": "llm_turn", "phase": "started", "provider": "OllamaProvider"},
        {"role": "assistant", "content": "TASK_COMPLETE: done"},
    ]
    mem.load_from_messages(records)
    msgs = mem.get_messages()
    assert len(msgs) == 2
    assert all("role" in m for m in msgs)
    assert not any(m.get("type") == "llm_turn" for m in msgs)


def test_trace_queries_include_llm_turn_records():
    """Per-session and cross-session trace readers should include llm_turn records."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="llm_query")
        store.append_llm_event("started", provider="OllamaProvider", attempt=1)
        store.append_llm_event("completed", provider="OllamaProvider", attempt=1)

        session_events = SessionStore.load_trace_events(
            "llm_query",
            base_dir=base,
            event_type="llm_turn",
            phase="completed",
        )
        assert len(session_events) == 1
        assert session_events[0]["phase"] == "completed"

        all_events = SessionStore.iter_trace_events(
            base_dir=base,
            event_type="llm_turn",
            phase="started",
        )
        assert len(all_events) == 1
        assert all_events[0]["session_id"] == "llm_query"


def test_summarize_trace_events_for_single_session():
    """Trace summaries count run, tool, and LLM outcomes for one session."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="trace_summary")
        store.append_run_started("summarize")
        store.append_tool_event("run_command", "started")
        store.append_tool_event("run_command", "completed", duration_ms=10)
        store.append_tool_event("web_open", "errored", error="boom")
        store.append_llm_event("started", provider="OllamaProvider", attempt=1)
        store.append_llm_event("errored", provider="OllamaProvider", attempt=1, error_code="rate_limit")
        store.append_run_result(
            {"success": False, "termination": "provider_failed", "summary": "failed", "screenshot": ""}
        )

        summary = SessionStore.summarize_trace_events("trace_summary", base_dir=base)
        assert summary["session_id"] == "trace_summary"
        assert summary["event_count"] == 7
        assert summary["by_type"] == {
            "run_started": 1,
            "tool_call": 3,
            "llm_turn": 2,
            "run_result": 1,
        }
        assert summary["tool_calls"]["started"] == 1
        assert summary["tool_calls"]["completed"] == 1
        assert summary["tool_calls"]["errored"] == 1
        assert summary["tool_calls"]["by_tool"]["run_command"] == 2
        assert summary["tool_calls"]["by_tool"]["web_open"] == 1
        assert summary["llm_turns"]["started"] == 1
        assert summary["llm_turns"]["errored"] == 1
        assert summary["llm_turns"]["by_provider"]["OllamaProvider"] == 2
        assert summary["llm_turns"]["error_codes"]["rate_limit"] == 1
        assert summary["run"]["started"] == 1
        assert summary["run"]["result"] == 1
        assert summary["run"]["success"] is False
        assert summary["run"]["termination"] == "provider_failed"


def test_summarize_trace_events_across_sessions():
    """Omitting session_id summarizes trace records across all sessions."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        first = SessionStore(base_dir=base, session_id="trace_sum_a")
        first.append_tool_event("run_command", "completed")
        second = SessionStore(base_dir=base, session_id="trace_sum_b")
        second.append_llm_event("completed", provider="BrowserProvider")

        summary = SessionStore.summarize_trace_events(base_dir=base)
        assert summary["session_id"] is None
        assert summary["event_count"] == 2
        assert summary["by_type"]["tool_call"] == 1
        assert summary["by_type"]["llm_turn"] == 1
        assert summary["tool_calls"]["by_tool"]["run_command"] == 1
        assert summary["llm_turns"]["by_provider"]["BrowserProvider"] == 1


def test_summarize_trace_run_outcome_uses_newest_completed_at():
    """cross-session summary run.success / run.termination reflect the run with
    the newest completed_at timestamp regardless of session-file ordering."""
    import json as _json

    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)

        # Write two sessions into the SAME date directory so filename-alpha order
        # does not reliably equal temporal order.  We control which run is "newer"
        # by the completed_at timestamps embedded in the records.
        date_dir = base / "2026-06-05"
        date_dir.mkdir(parents=True)

        # Session A (alphabetically first, but OLDER run: 10:00 UTC)
        file_a = date_dir / "aaaa.jsonl"
        file_a.write_text(
            _json.dumps({
                "type": "run_result",
                "session_id": "aaaa",
                "completed_at": "2026-06-05T10:00:00+00:00",
                "success": False,
                "termination": "cancelled",
                "summary": "old run",
                "had_screenshot": False,
                "cwd": "/tmp",
            }) + "\n",
            encoding="utf-8",
        )

        # Session B (alphabetically last, but NEWER run: 11:00 UTC)
        file_b = date_dir / "zzzz.jsonl"
        file_b.write_text(
            _json.dumps({
                "type": "run_result",
                "session_id": "zzzz",
                "completed_at": "2026-06-05T11:00:00+00:00",
                "success": True,
                "termination": "task_complete",
                "summary": "new run",
                "had_screenshot": False,
                "cwd": "/tmp",
            }) + "\n",
            encoding="utf-8",
        )

        summary = SessionStore.summarize_trace_events(base_dir=base)

        assert summary["run"]["result"] == 2
        # Must reflect the NEWER run (11:00 timestamp = session zzzz)
        assert summary["run"]["success"] is True, (
            "cross-session summary should reflect newest completed_at, not last file processed"
        )
        assert summary["run"]["termination"] == "task_complete"


def test_summarize_trace_tool_error_codes():
    """tool_calls.error_codes counts error_code values from errored tool events."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="ec_summary")
        store.append_tool_event("run_command", "started", risk_level="high")
        store.append_tool_event("run_command", "errored", error="BLOCKED: rm", error_code="blocked")
        store.append_tool_event("read_file", "started", risk_level="low")
        store.append_tool_event("read_file", "errored", error="PERMISSION_ERROR: outside root", error_code="permission_denied")
        store.append_tool_event("git_commit", "started", risk_level="high")
        store.append_tool_event("git_commit", "errored", error="PERMISSION_ERROR: git path", error_code="permission_denied")
        store.append_tool_event("web_open", "started", risk_level="medium")
        store.append_tool_event("web_open", "completed", duration_ms=50)

        summary = SessionStore.summarize_trace_events("ec_summary", base_dir=base)

        assert summary["tool_calls"]["error_codes"]["blocked"] == 1
        assert summary["tool_calls"]["error_codes"]["permission_denied"] == 2
        assert "error_codes" in summary["tool_calls"]
        # Completed tools do not appear in error_codes
        assert "web_open" not in str(summary["tool_calls"]["error_codes"])


def test_summarize_trace_tool_risk_levels():
    """tool_calls.risk_levels counts risk_level values from started tool events."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="rl_summary")
        store.append_tool_event("run_command", "started", risk_level="high")
        store.append_tool_event("run_command", "completed", duration_ms=10)
        store.append_tool_event("git_commit", "started", risk_level="high")
        store.append_tool_event("git_commit", "errored", error="ERROR: not a git repo", error_code="execution_error")
        store.append_tool_event("write_file", "started", risk_level="medium")
        store.append_tool_event("write_file", "completed", duration_ms=5)
        store.append_tool_event("read_file", "started", risk_level="low")
        store.append_tool_event("read_file", "completed", duration_ms=3)
        store.append_tool_event("take_screenshot", "started", risk_level="low")
        store.append_tool_event("take_screenshot", "completed", duration_ms=80)

        summary = SessionStore.summarize_trace_events("rl_summary", base_dir=base)

        assert summary["tool_calls"]["risk_levels"]["high"] == 2
        assert summary["tool_calls"]["risk_levels"]["medium"] == 1
        assert summary["tool_calls"]["risk_levels"]["low"] == 2


def test_summarize_trace_tool_risk_and_error_absent_when_no_data():
    """risk_levels and error_codes are empty dicts when no enriched events exist."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="plain_summary")
        # Old-style events without risk_level or error_code fields
        store.append_tool_event("run_command", "started")
        store.append_tool_event("run_command", "completed", duration_ms=10)

        summary = SessionStore.summarize_trace_events("plain_summary", base_dir=base)

        assert summary["tool_calls"]["error_codes"] == {}
        assert summary["tool_calls"]["risk_levels"] == {}


def test_summarize_trace_tool_combined_risk_and_error():
    """Both risk_levels and error_codes can be populated in the same session."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        store = SessionStore(base_dir=base, session_id="combined_summary")
        store.append_tool_event("delete_file", "started", risk_level="high")
        store.append_tool_event("delete_file", "errored", error="PERMISSION_ERROR: outside root", error_code="permission_denied")
        store.append_tool_event("run_command", "started", risk_level="high")
        store.append_tool_event("run_command", "errored", error="TIMEOUT: exceeded 30s", error_code="timeout")
        store.append_tool_event("web_observe", "started", risk_level="low")
        store.append_tool_event("web_observe", "completed", duration_ms=20)

        summary = SessionStore.summarize_trace_events("combined_summary", base_dir=base)

        assert summary["tool_calls"]["risk_levels"]["high"] == 2
        assert summary["tool_calls"]["risk_levels"]["low"] == 1
        assert summary["tool_calls"]["error_codes"]["permission_denied"] == 1
        assert summary["tool_calls"]["error_codes"]["timeout"] == 1
        assert summary["tool_calls"]["errored"] == 2
        assert summary["tool_calls"]["completed"] == 1


_AGENT_PY_PATH = Path(__file__).resolve().parent.parent / "orchestrator" / "agent.py"


# ---------------------------------------------------------------------------
# Behavioral trace tests: drive the REAL agent methods against a REAL
# SessionStore and assert the JSONL trace records appear on disk.
# (The previous static source-grep versions passed even when the traced calls
# were commented out — mutation-verified false confidence.)
# ---------------------------------------------------------------------------

def _trace_records(store: SessionStore, record_type: str) -> list:
    records = []
    for line in store.session_file.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if rec.get("type") == record_type:
            records.append(rec)
    return records


def test_agent_run_writes_run_started_record(tmp_path):
    """agent.run() must append a run_started JSONL record with the task text."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from tests.conftest import make_test_agent

    store = SessionStore(base_dir=tmp_path, session_id="trace_run_started")
    agent = make_test_agent(session_store=store)
    # Terminate the run immediately after the trace is written: stub the loop
    # collaborators (the run_started append itself stays REAL).
    agent._refresh_tools = AsyncMock()
    agent._tools = []  # run() exits early on the no-tools path
    agent._complete_run = lambda result: result
    agent._check_context_pressure = AsyncMock(return_value=None)
    agent._emit_context_snapshot = lambda: None
    agent._persist_context_state_extra = lambda *a, **k: None
    agent._build_system_prompt = lambda task: "system"
    agent._log = lambda level, msg: None
    agent._is_cancelled = lambda: False
    agent.session = MagicMock()

    asyncio.run(agent.run("trace me please"))

    records = _trace_records(store, "run_started")
    assert len(records) == 1
    assert records[0]["task"] == "trace me please"
    assert records[0]["session_id"] == "trace_run_started"


def test_agent_execute_tool_appends_tool_trace_records(tmp_path):
    """The real _execute_tool must append started + completed tool_call records."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from tests.conftest import make_test_agent

    store = SessionStore(base_dir=tmp_path, session_id="trace_tool")
    agent = make_test_agent(session_store=store)
    agent._log = lambda level, msg: None
    result = MagicMock()
    result.content = [SimpleNamespace(text="file contents")]
    agent.session = MagicMock()
    agent.session.call_tool = AsyncMock(return_value=result)

    output = asyncio.run(agent._execute_tool("read_file", {"path": "x.txt"}))

    assert "file contents" in output
    records = _trace_records(store, "tool_call")
    phases = [r["phase"] for r in records]
    assert phases == ["started", "completed"]
    assert all(r["tool_name"] == "read_file" for r in records)
    assert records[0]["arg_keys"] == ["path"]
    assert records[0].get("risk_level")  # started record carries the risk tier
    assert "duration_ms" in records[1]


def test_agent_execute_tool_appends_errored_record_on_failure(tmp_path):
    """A raising MCP call must yield an errored tool_call record, not a crash."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from tests.conftest import make_test_agent

    store = SessionStore(base_dir=tmp_path, session_id="trace_tool_err")
    agent = make_test_agent(session_store=store)
    agent._log = lambda level, msg: None
    agent.session = MagicMock()
    agent.session.call_tool = AsyncMock(side_effect=RuntimeError("boom"))

    output = asyncio.run(agent._execute_tool("read_file", {"path": "x.txt"}))

    assert output.startswith("ERROR calling read_file")
    records = _trace_records(store, "tool_call")
    phases = [r["phase"] for r in records]
    assert phases == ["started", "errored"]
    assert records[1]["error_code"] == "internal_error"


def test_agent_call_with_retry_appends_llm_trace_records(tmp_path):
    """The real _call_with_retry must trace started + completed llm_turn records
    without storing prompt text."""
    import asyncio

    from tests.conftest import make_test_agent

    store = SessionStore(base_dir=tmp_path, session_id="trace_llm")
    agent = make_test_agent(session_store=store)
    agent._log = lambda level, msg: None

    messages = [{"role": "user", "content": "SECRET PROMPT TEXT"}]
    tools = [{"name": "run_command"}, {"name": "task_complete"}]
    response = asyncio.run(agent._call_with_retry(messages, tools, "system"))

    assert isinstance(response, dict)
    records = _trace_records(store, "llm_turn")
    phases = [r["phase"] for r in records]
    assert phases == ["started", "completed"]
    assert records[0]["message_count"] == 1
    assert records[0]["tool_count"] == 2
    assert records[0]["provider"] == "FakeProvider"
    assert "duration_ms" in records[1]
    # Privacy contract: prompt text must never reach the trace file
    assert "SECRET PROMPT TEXT" not in store.session_file.read_text(encoding="utf-8")


def test_agent_call_with_retry_appends_errored_record(tmp_path):
    """A non-retryable provider failure must append an errored llm_turn record."""
    import asyncio

    import pytest

    from tests.conftest import make_test_agent

    class AuthFailProvider:
        async def complete(self, messages, tools, system):
            raise PermissionError("invalid api key")

    store = SessionStore(base_dir=tmp_path, session_id="trace_llm_err")
    agent = make_test_agent(session_store=store, provider=AuthFailProvider())
    agent._log = lambda level, msg: None

    with pytest.raises(PermissionError):
        asyncio.run(agent._call_with_retry([], [], "system"))

    records = _trace_records(store, "llm_turn")
    phases = [r["phase"] for r in records]
    assert phases == ["started", "errored"]
    assert records[1]["error_code"] == "auth"


def test_no_tools_path_uses_complete_run():
    """The if-not-self._tools early return must go through _complete_run.

    append_run_started is already written before _refresh_tools() is called, so
    any early exit after that point must write the matching run_result via
    _complete_run -- otherwise the session JSONL has a run_started with no close.
    """
    source = _AGENT_PY.read_text(encoding="utf-8")
    # Negative: must not be a bare dict return for the no-tools case
    assert 'return {"success": False, "summary": "No MCP tools available"' not in source, (
        'The "if not self._tools:" path returns a bare dict instead of _complete_run(). '
        "This skips the run_result JSONL append, leaving run_started unmatched."
    )
    # Positive: the if-not-self._tools block must call _complete_run
    idx = source.find("if not self._tools:")
    assert idx != -1, '"if not self._tools:" guard not found in agent.py'
    block = source[idx: idx + 250]
    assert "_complete_run" in block, (
        '"if not self._tools:" block does not call _complete_run -- '
        "run_result will not be written for the no-tools failure path"
    )


def test_load_session_concatenates_rolls():
    """load_session must return rolled-segment messages in stamp order then live file.

    Given <id>.roll.<stamp1>.jsonl, <id>.roll.<stamp2>.jsonl and the live
    <id>.jsonl all in the same date directory, the returned list must be the
    concatenation: roll-stamp1 messages, roll-stamp2 messages, live messages —
    in that chronological stamp order.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        date_dir = base / "2026-01-01"
        sid = "rollconcat"

        # roll segment 1 (earlier stamp)
        roll1 = date_dir / f"{sid}.roll.20260101T100000Z.jsonl"
        roll1.parent.mkdir(parents=True, exist_ok=True)
        roll1.write_text(
            json.dumps({"role": "user", "content": "roll1-msg1"}) + "\n"
            + json.dumps({"role": "assistant", "content": "roll1-msg2"}) + "\n",
            encoding="utf-8",
        )

        # roll segment 2 (later stamp)
        roll2 = date_dir / f"{sid}.roll.20260101T110000Z.jsonl"
        roll2.write_text(
            json.dumps({"role": "user", "content": "roll2-msg1"}) + "\n",
            encoding="utf-8",
        )

        # live file
        live = date_dir / f"{sid}.jsonl"
        live.write_text(
            json.dumps({"role": "assistant", "content": "live-msg1"}) + "\n",
            encoding="utf-8",
        )

        messages = SessionStore.load_session(sid, base_dir=base)
        assert len(messages) == 4
        assert messages[0]["content"] == "roll1-msg1"
        assert messages[1]["content"] == "roll1-msg2"
        assert messages[2]["content"] == "roll2-msg1"
        assert messages[3]["content"] == "live-msg1"


def test_find_session_file_resolves_roll_only():
    """find_session_file must return the expected live-file path when only rolled
    segments exist and the live file has been fully rotated away.

    The returned path need not exist on disk; callers use it to locate the
    session directory.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        date_dir = base / "2026-01-02"
        sid = "rollonly"

        # Create a rolled segment but NOT the live file
        roll_file = date_dir / f"{sid}.roll.20260102T090000Z.jsonl"
        roll_file.parent.mkdir(parents=True, exist_ok=True)
        roll_file.write_text(
            json.dumps({"role": "user", "content": "rolled msg"}) + "\n",
            encoding="utf-8",
        )

        result = SessionStore.find_session_file(sid, base_dir=base)
        expected = date_dir / f"{sid}.jsonl"
        assert result == expected
        # The live file itself must NOT exist on disk (it was rotated away)
        assert not expected.exists()


def test_enumeration_excludes_roll():
    """list_sessions and iter_trace_events must skip .roll. files so rolled
    segments never appear as phantom sessions in the sidebar or trace queries.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        date_dir = base / "2026-01-03"
        date_dir.mkdir(parents=True, exist_ok=True)

        live_sid = "realsession"
        roll_sid = "realsession"

        # Live file
        live = date_dir / f"{live_sid}.jsonl"
        live.write_text(
            json.dumps({"role": "user", "content": "real"}) + "\n",
            encoding="utf-8",
        )

        # Rolled segment for the same session (must be excluded from enumeration)
        roll = date_dir / f"{roll_sid}.roll.20260103T080000Z.jsonl"
        roll.write_text(
            json.dumps({"type": "run_started", "task": "phantom"}) + "\n",
            encoding="utf-8",
        )

        # list_sessions must not produce a separate entry for the roll file
        sessions = SessionStore.list_sessions(base_dir=base)
        session_paths = [s["path"] for s in sessions]
        assert all(".roll." not in p for p in session_paths), (
            f"list_sessions returned a .roll. entry: {session_paths}"
        )
        # Exactly one session entry (the live file)
        assert len(sessions) == 1

        # iter_trace_events must not emit records from the roll file directly
        events = SessionStore.iter_trace_events(base_dir=base, event_type="run_started")
        # No run_started event should appear from the roll file via iter_trace_events;
        # the live file contains no run_started records, so the list is empty.
        for event in events:
            assert ".roll." not in event.get("session_id", ""), (
                f"iter_trace_events returned an event sourced from a roll file: {event}"
            )


def test_strip_images_handles_none_content():
    """_strip_images_for_disk on a message with content=None must drop keys that
    begin with '_' (internal metadata) rather than returning the message verbatim.
    """
    msg = {
        "role": "assistant",
        "_has_screenshot": True,
        "_internal_flag": "debug-only",
        "content": None,
    }
    result = _strip_images_for_disk(msg)

    # Non-underscore keys must be preserved
    assert result["role"] == "assistant"
    # content=None is preserved (it is not an image block)
    assert "content" in result
    assert result["content"] is None
    # Underscore-prefixed internal keys must be stripped
    assert "_has_screenshot" not in result, (
        "_strip_images_for_disk returned _has_screenshot for content=None message"
    )
    assert "_internal_flag" not in result, (
        "_strip_images_for_disk returned _internal_flag for content=None message"
    )


def test_strip_images_does_not_leak_internal_metadata_keys():
    """_strip_images_for_disk must remove _-prefixed internal keys from all content types."""
    # String content
    msg_str = {"role": "user", "content": "hello", "_has_screenshot": False}
    result_str = _strip_images_for_disk(msg_str)
    assert "_has_screenshot" not in result_str

    # List content
    msg_list = {
        "role": "user",
        "content": [{"type": "text", "text": "hi"}],
        "_has_screenshot": True,
    }
    result_list = _strip_images_for_disk(msg_list)
    assert "_has_screenshot" not in result_list


def test_session_store_atomic_id_claiming():
    with tempfile.TemporaryDirectory() as tmp_dir:
        base_path = Path(tmp_dir)
        store = SessionStore(base_dir=base_path)
        
        # Verify the chosen ID's jsonl file has been created atomically
        assert store.session_file.exists()
        assert store.session_file.stat().st_size == 0
        
        # Creating another store should not collide with the first one
        store2 = SessionStore(base_dir=base_path)
        assert store2.session_file.exists()
        assert store2.session_id != store.session_id

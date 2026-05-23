"""
Claw exhaustive end-to-end test suite — drives the claw binary via subprocess.

Usage:
    python tests/claw_test_suite.py                        # run all tests
    python tests/claw_test_suite.py --only fast            # tag/name filter
    python tests/claw_test_suite.py --only bridge,tools    # multiple tags
    python tests/claw_test_suite.py --json                 # machine-readable
    python tests/claw_test_suite.py --list                 # enumerate tests
    python tests/claw_test_suite.py --binary <path>        # override claw path

Test categories:
  smoke     — CLI surface only, no auth, no LLM
  bridge    — CLAW_FILE_BRIDGE=1 with a Python fake relay
  tools     — real file/bash tool execution verified on disk
  chain     — multi-step tool chains (write → read → edit → verify)
  safety    — permission enforcement, path escapes, dangerous commands
  error     — error recovery, bad inputs, relay failures
  search    — glob_search and grep_search tool coverage
  bash      — shell execution via the bash tool
  stress    — many relay turns, large payloads, deep chains
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DEFAULT_BINARY = (
    Path(__file__).resolve().parent.parent
    / "pythonExperimentTool" / "claw-code" / "rust" / "target" / "debug" / "claw"
)
CLAW_CWD = Path(__file__).resolve().parent.parent / "pythonExperimentTool" / "claw-code"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class TestCase:
    name: str
    args: list[str]
    timeout: int = 60
    stdin: Optional[str] = None
    assertions: list[Callable[[str, str, int, "FakeRelay | None"], Optional[str]]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    bridge_responses: Optional[list[dict]] = None
    # Pre-create files in CLAW_CWD before running (path relative to CLAW_CWD → content)
    seed_files: dict[str, str] = field(default_factory=dict)


@dataclass
class TestResult:
    name: str
    status: str           # "pass" | "fail" | "skip" | "timeout" | "transport"
    duration_s: float
    failure_reason: str = ""


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------

def returncode_is(expected: int):
    def check(out, err, rc, _relay):
        return None if rc == expected else f"Exit code {rc!r}, expected {expected}"
    return check


def returncode_nonzero():
    def check(out, err, rc, _relay):
        return None if rc != 0 else "Expected non-zero exit code"
    return check


def stdout_contains(needle: str, case_insensitive: bool = True):
    def check(out, _err, _rc, _relay):
        hay = out.lower() if case_insensitive else out
        n = needle.lower() if case_insensitive else needle
        return None if n in hay else f"Stdout missing {needle!r}\nGot: {out[:300]}"
    return check


def stdout_not_contains(needle: str):
    def check(out, _err, _rc, _relay):
        return None if needle not in out else f"Stdout should not contain {needle!r}"
    return check


def stderr_contains(needle: str, case_insensitive: bool = True):
    def check(_out, err, _rc, _relay):
        hay = err.lower() if case_insensitive else err
        n = needle.lower() if case_insensitive else needle
        return None if n in hay else f"Stderr missing {needle!r}\nGot: {err[:300]}"
    return check


def stderr_not_contains(needle: str):
    def check(_out, err, _rc, _relay):
        return None if needle not in err else f"Stderr should not contain {needle!r}"
    return check


def stdout_is_valid_json():
    def check(out, _err, _rc, _relay):
        try:
            json.loads(out)
            return None
        except json.JSONDecodeError as e:
            return f"Stdout was not valid JSON: {e}\nGot: {out[:300]}"
    return check


def stdout_json_has_key(key: str):
    def check(out, _err, _rc, _relay):
        try:
            data = json.loads(out)
            return None if key in data else f"JSON missing key {key!r}"
        except json.JSONDecodeError as e:
            return f"Stdout was not valid JSON: {e}"
    return check


def stdout_json_field_eq(key: str, expected):
    def check(out, _err, _rc, _relay):
        try:
            data = json.loads(out)
            actual = data.get(key)
            return None if actual == expected else f"JSON {key!r}={actual!r}, expected {expected!r}"
        except json.JSONDecodeError as e:
            return f"Stdout was not valid JSON: {e}"
    return check


def file_exists_with_content(path: Path, expected: str):
    def check(_out, _err, _rc, _relay):
        if not path.exists():
            return f"File {path} was not created"
        actual = path.read_text()
        if expected not in actual:
            return f"File content {actual!r} missing {expected!r}"
        return None
    return check


def file_does_not_exist(path: Path):
    def check(_out, _err, _rc, _relay):
        return None if not path.exists() else f"File {path} should not exist"
    return check


def file_exists(path: Path):
    def check(_out, _err, _rc, _relay):
        return None if path.exists() else f"File {path} was not created"
    return check


def relay_saw_n_requests(n: int):
    """Assert the fake relay received exactly n LLM round-trips."""
    def check(_out, _err, _rc, relay):
        if relay is None:
            return "No relay — test not run in bridge mode"
        got = len(relay.requests_seen)
        return None if got == n else f"Expected {n} relay requests, got {got}"
    return check


def relay_saw_at_least(n: int):
    def check(_out, _err, _rc, relay):
        if relay is None:
            return "No relay"
        got = len(relay.requests_seen)
        return None if got >= n else f"Expected ≥{n} relay requests, got {got}"
    return check


def relay_request_contains(turn: int, needle: str):
    """Assert the Nth relay request JSON contains a string."""
    def check(_out, _err, _rc, relay):
        if relay is None:
            return "No relay"
        if turn >= len(relay.requests_seen):
            return f"Turn {turn} not found (relay saw {len(relay.requests_seen)} requests)"
        raw = json.dumps(relay.requests_seen[turn])
        return None if needle in raw else f"Turn {turn} request missing {needle!r}"
    return check


def session_file_was_created():
    sessions_root = CLAW_CWD.parent.parent / ".claw" / "sessions"
    def check(_out, _err, _rc, _relay):
        if not sessions_root.exists():
            return f"No .claw/sessions/ at {sessions_root}"
        for date_dir in sessions_root.iterdir():
            if date_dir.is_dir() and any(date_dir.glob("*.jsonl")):
                return None
        return "No session JSONL files found"
    return check


# ---------------------------------------------------------------------------
# Fake relay
# ---------------------------------------------------------------------------

class FakeRelay:
    """Polls the bridge dir for requests, serves canned responses in order."""

    def __init__(self, bridge_dir: Path, responses: list[dict]):
        self.bridge_dir = bridge_dir
        self.responses = list(responses)
        self.requests_seen: list[dict] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self.bridge_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self):
        request_path = self.bridge_dir / "bridge_request.json"
        response_path = self.bridge_dir / "bridge_response.json"
        idx = 0
        while not self._stop.is_set():
            if request_path.exists() and idx < len(self.responses):
                try:
                    raw = request_path.read_text()
                    self.requests_seen.append(json.loads(raw) if raw.strip() else {})
                except Exception:
                    self.requests_seen.append({})
                try:
                    request_path.unlink()
                except FileNotFoundError:
                    pass
                tmp = response_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(self.responses[idx]))
                tmp.replace(response_path)
                idx += 1
            time.sleep(0.1)


def run_with_fake_bridge(
    binary: Path,
    args: list[str],
    responses: list[dict],
    timeout: int,
    seed_files: dict[str, str] | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess, FakeRelay]:
    seed_files = seed_files or {}
    # Seed files before launching (so Claw can read them during execution)
    for rel, content in seed_files.items():
        p = CLAW_CWD / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["CLAW_FILE_BRIDGE"] = "1"
        env["XDG_RUNTIME_DIR"] = tmpdir
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        if extra_env:
            env.update(extra_env)

        bridge_dir = Path(tmpdir) / "claw_bridge"
        relay = FakeRelay(bridge_dir, responses)
        relay.start()
        try:
            proc = subprocess.run(
                [str(binary)] + args,
                capture_output=True, text=True,
                timeout=timeout, env=env,
                cwd=str(CLAW_CWD),
            )
        finally:
            relay.stop()
        return proc, relay


# ---------------------------------------------------------------------------
# Artifact registry (for cleanup)
# ---------------------------------------------------------------------------

TOOL_TEST_ARTIFACTS = [
    # smoke / existing
    "claw_test_write.txt",
    "claw_test_pm_blocked.txt",
    "claw_test_seed.txt",
    "claw_test_edited.txt",
    # chain tests
    "claw_chain_a.txt",
    "claw_chain_b.txt",
    "claw_chain_c.txt",
    "claw_chain_multifile_1.txt",
    "claw_chain_multifile_2.txt",
    "claw_chain_multifile_3.txt",
    # bash tests
    "claw_bash_out.txt",
    "claw_bash_append.txt",
    # search tests
    "claw_search_alpha.py",
    "claw_search_beta.py",
    "claw_search_gamma.txt",
    # stress tests
    "claw_stress_counter.txt",
    "claw_stress_large.txt",
    # safety tests
    "claw_escaped.txt",
    "claw_danger_out.txt",
]


def cleanup():
    for name in TOOL_TEST_ARTIFACTS:
        (CLAW_CWD / name).unlink(missing_ok=True)


def has_anthropic_auth() -> bool:
    return bool(
        os.environ.get("ANTHROPIC_API_KEY", "").strip()
        or os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    )


# ---------------------------------------------------------------------------
# Test definitions
# ---------------------------------------------------------------------------

TESTS: list[TestCase] = [

    # ════════════════════════════════════════════════════════════════════════
    # SMOKE — CLI surface, no LLM, no auth
    # ════════════════════════════════════════════════════════════════════════

    TestCase(
        name="version_flag",
        args=["--version"],
        timeout=10,
        tags=["fast", "smoke"],
        assertions=[
            returncode_is(0),
            stdout_contains("claw"),
        ],
    ),
    TestCase(
        name="help_subcommand",
        args=["help"],
        timeout=10,
        tags=["fast", "smoke"],
        assertions=[
            returncode_is(0),
            stdout_contains("Usage"),
            stdout_contains("prompt"),
            stdout_contains("--resume"),
        ],
    ),
    TestCase(
        name="status_json",
        args=["--output-format", "json", "status"],
        timeout=15,
        tags=["fast", "smoke", "json"],
        assertions=[
            returncode_is(0),
            stdout_is_valid_json(),
            stdout_json_field_eq("kind", "status"),
            stdout_json_has_key("model"),
            stdout_json_has_key("permission_mode"),
        ],
    ),
    TestCase(
        name="doctor_runs",
        args=["doctor"],
        timeout=20,
        tags=["fast", "smoke"],
        assertions=[
            returncode_is(0),
            stdout_contains("Doctor"),
            stdout_contains("Summary"),
        ],
    ),
    TestCase(
        name="status_text",
        args=["status"],
        timeout=15,
        tags=["fast", "smoke"],
        assertions=[
            returncode_is(0),
            stdout_contains("model"),
        ],
    ),
    TestCase(
        name="prompt_no_auth_errors_cleanly",
        args=["--output-format", "json", "prompt", "ping"],
        timeout=20,
        tags=["fast", "smoke", "no-auth"],
        assertions=[
            stderr_contains('"type":"error"'),
            stderr_contains("missing Anthropic credentials"),
        ],
    ),
    TestCase(
        name="unknown_subcommand_exits_nonzero",
        args=["totally_unknown_subcommand_xyz"],
        timeout=10,
        tags=["fast", "smoke", "error"],
        assertions=[returncode_nonzero()],
    ),
    TestCase(
        name="help_flag",
        args=["--help"],
        timeout=10,
        tags=["fast", "smoke"],
        assertions=[
            returncode_is(0),
            stdout_contains("Usage"),
        ],
    ),

    # ════════════════════════════════════════════════════════════════════════
    # BRIDGE — basic protocol validation
    # ════════════════════════════════════════════════════════════════════════

    TestCase(
        name="bridge_text_only_response",
        args=["prompt", "say hi"],
        timeout=30,
        tags=["bridge", "smoke"],
        bridge_responses=[{"text": "Hi there!"}],
        assertions=[
            returncode_is(0),
            stdout_contains("Hi there"),
            relay_saw_n_requests(1),
        ],
    ),
    TestCase(
        name="bridge_empty_text_response",
        args=["prompt", "respond with empty text"],
        timeout=30,
        tags=["bridge", "smoke"],
        bridge_responses=[{"text": ""}],
        assertions=[
            returncode_is(0),
            relay_saw_n_requests(1),
        ],
    ),
    TestCase(
        name="bridge_multiline_text_response",
        args=["prompt", "give me a list"],
        timeout=30,
        tags=["bridge", "smoke"],
        bridge_responses=[{"text": "Line one\nLine two\nLine three"}],
        assertions=[
            returncode_is(0),
            stdout_contains("Line one"),
            stdout_contains("Line three"),
        ],
    ),
    TestCase(
        name="bridge_unicode_in_response",
        args=["prompt", "say something in unicode"],
        timeout=30,
        tags=["bridge", "smoke"],
        bridge_responses=[{"text": "日本語テスト — café — naïve — ñoño"}],
        assertions=[
            returncode_is(0),
            stdout_contains("café"),
        ],
    ),
    TestCase(
        name="bridge_request_contains_prompt",
        args=["prompt", "unique_probe_string_xyz"],
        timeout=30,
        tags=["bridge", "smoke"],
        bridge_responses=[{"text": "Got it."}],
        assertions=[
            returncode_is(0),
            relay_request_contains(0, "unique_probe_string_xyz"),
        ],
    ),
    TestCase(
        name="bridge_two_turn_tool_call",
        args=["prompt", "what is in Cargo.toml"],
        timeout=45,
        tags=["bridge", "tools"],
        bridge_responses=[
            {
                "text": "I'll check the Cargo.toml.",
                "tool_calls": [
                    {"name": "read_file", "id": "toolu_test1",
                     "input": {"path": "Cargo.toml"}}
                ],
            },
            {"text": "Done — saw the Cargo.toml file."},
        ],
        assertions=[
            returncode_is(0),
            stdout_contains("Done"),
            relay_saw_n_requests(2),
        ],
    ),
    TestCase(
        name="bridge_tool_result_sent_back_to_relay",
        args=["prompt", "read Cargo.toml and summarise"],
        timeout=45,
        tags=["bridge", "tools"],
        bridge_responses=[
            {
                "tool_calls": [
                    {"name": "read_file", "id": "toolu_r1",
                     "input": {"path": "Cargo.toml"}}
                ],
            },
            {"text": "Summarised."},
        ],
        assertions=[
            returncode_is(0),
            # Turn 1 is just the user prompt; turn 2 request should include
            # the tool result (file content) so the relay can summarise it.
            relay_saw_n_requests(2),
            relay_request_contains(1, "tool_result"),
        ],
    ),

    # ════════════════════════════════════════════════════════════════════════
    # TOOLS — real file operations verified on disk
    # ════════════════════════════════════════════════════════════════════════

    TestCase(
        name="bridge_write_file_creates_artifact",
        args=["prompt", "create a test file"],
        timeout=30,
        tags=["bridge", "tools", "write"],
        bridge_responses=[
            {"tool_calls": [
                {"name": "write_file", "id": "toolu_w1",
                 "input": {"path": "claw_test_write.txt",
                           "content": "hello from claw"}}
            ]},
            {"text": "File created."},
        ],
        assertions=[
            returncode_is(0),
            file_exists_with_content(CLAW_CWD / "claw_test_write.txt", "hello from claw"),
        ],
    ),
    TestCase(
        name="bridge_read_file_returns_content",
        args=["prompt", "read the seed file"],
        timeout=30,
        tags=["bridge", "tools"],
        seed_files={"claw_test_seed.txt": "seed-content-abc"},
        bridge_responses=[
            {"tool_calls": [
                {"name": "read_file", "id": "toolu_r2",
                 "input": {"path": "claw_test_seed.txt"}}
            ]},
            {"text": "Read done."},
        ],
        assertions=[
            returncode_is(0),
            # The tool result containing the file content should arrive in turn 2 request
            relay_request_contains(1, "seed-content-abc"),
        ],
    ),
    TestCase(
        name="bridge_edit_file_modifies_artifact",
        args=["prompt", "edit the seed file"],
        timeout=30,
        tags=["bridge", "tools", "edit"],
        seed_files={"claw_test_seed.txt": "before-token"},
        bridge_responses=[
            {"tool_calls": [
                {"name": "edit_file", "id": "toolu_edit",
                 "input": {"path": "claw_test_seed.txt",
                           "old_string": "before-token",
                           "new_string": "after-token"}}
            ]},
            {"text": "Edit applied."},
        ],
        assertions=[
            returncode_is(0),
            file_exists_with_content(CLAW_CWD / "claw_test_seed.txt", "after-token"),
        ],
    ),
    TestCase(
        name="bridge_write_then_overwrite",
        args=["prompt", "write then overwrite"],
        timeout=30,
        tags=["bridge", "tools", "write"],
        bridge_responses=[
            {"tool_calls": [
                {"name": "write_file", "id": "toolu_ow1",
                 "input": {"path": "claw_test_write.txt", "content": "version-1"}}
            ]},
            {"tool_calls": [
                {"name": "write_file", "id": "toolu_ow2",
                 "input": {"path": "claw_test_write.txt", "content": "version-2"}}
            ]},
            {"text": "Overwritten."},
        ],
        assertions=[
            returncode_is(0),
            file_exists_with_content(CLAW_CWD / "claw_test_write.txt", "version-2"),
        ],
    ),
    TestCase(
        name="bridge_edit_nonexistent_file_errors",
        args=["prompt", "edit a file that does not exist"],
        timeout=30,
        tags=["bridge", "tools", "error"],
        bridge_responses=[
            {"tool_calls": [
                {"name": "edit_file", "id": "toolu_e404",
                 "input": {"path": "definitely_does_not_exist_xyz.txt",
                           "old_string": "abc", "new_string": "def"}}
            ]},
            {"text": "Done."},
        ],
        assertions=[
            # Claw should still exit 0 (tool error returned as result, not crash)
            returncode_is(0),
            # Turn 2 request should contain the error from the failed edit
            relay_request_contains(1, "error"),
        ],
    ),

    # ════════════════════════════════════════════════════════════════════════
    # CHAIN — multi-step tool sequences
    # ════════════════════════════════════════════════════════════════════════

    TestCase(
        name="bridge_read_after_write_round_trip",
        args=["prompt", "write then read a file"],
        timeout=30,
        tags=["bridge", "chain"],
        bridge_responses=[
            {"tool_calls": [
                {"name": "write_file", "id": "toolu_rw_w",
                 "input": {"path": "claw_test_edited.txt",
                           "content": "round-trip-payload"}}
            ]},
            {"tool_calls": [
                {"name": "read_file", "id": "toolu_rw_r",
                 "input": {"path": "claw_test_edited.txt"}}
            ]},
            {"text": "Round trip complete."},
        ],
        assertions=[
            returncode_is(0),
            file_exists_with_content(CLAW_CWD / "claw_test_edited.txt", "round-trip-payload"),
            relay_saw_n_requests(3),
            relay_request_contains(2, "round-trip-payload"),
        ],
    ),
    TestCase(
        name="bridge_write_edit_read_three_step",
        args=["prompt", "three step chain"],
        timeout=45,
        tags=["bridge", "chain"],
        bridge_responses=[
            # step 1: write
            {"tool_calls": [
                {"name": "write_file", "id": "toolu_c1",
                 "input": {"path": "claw_chain_a.txt", "content": "original"}}
            ]},
            # step 2: edit
            {"tool_calls": [
                {"name": "edit_file", "id": "toolu_c2",
                 "input": {"path": "claw_chain_a.txt",
                           "old_string": "original", "new_string": "modified"}}
            ]},
            # step 3: read back
            {"tool_calls": [
                {"name": "read_file", "id": "toolu_c3",
                 "input": {"path": "claw_chain_a.txt"}}
            ]},
            {"text": "Chain complete."},
        ],
        assertions=[
            returncode_is(0),
            file_exists_with_content(CLAW_CWD / "claw_chain_a.txt", "modified"),
            relay_saw_n_requests(4),
            relay_request_contains(3, "modified"),
        ],
    ),
    TestCase(
        name="bridge_three_file_parallel_writes",
        args=["prompt", "write three files"],
        timeout=45,
        tags=["bridge", "chain"],
        bridge_responses=[
            {"tool_calls": [
                {"name": "write_file", "id": "toolu_p1",
                 "input": {"path": "claw_chain_multifile_1.txt", "content": "file-one"}}
            ]},
            {"tool_calls": [
                {"name": "write_file", "id": "toolu_p2",
                 "input": {"path": "claw_chain_multifile_2.txt", "content": "file-two"}}
            ]},
            {"tool_calls": [
                {"name": "write_file", "id": "toolu_p3",
                 "input": {"path": "claw_chain_multifile_3.txt", "content": "file-three"}}
            ]},
            {"text": "All three written."},
        ],
        assertions=[
            returncode_is(0),
            file_exists_with_content(CLAW_CWD / "claw_chain_multifile_1.txt", "file-one"),
            file_exists_with_content(CLAW_CWD / "claw_chain_multifile_2.txt", "file-two"),
            file_exists_with_content(CLAW_CWD / "claw_chain_multifile_3.txt", "file-three"),
        ],
    ),
    TestCase(
        name="bridge_write_read_edit_read_four_step",
        args=["prompt", "four step chain"],
        timeout=60,
        tags=["bridge", "chain"],
        bridge_responses=[
            {"tool_calls": [
                {"name": "write_file", "id": "t1",
                 "input": {"path": "claw_chain_b.txt", "content": "step-one"}}
            ]},
            {"tool_calls": [
                {"name": "read_file", "id": "t2",
                 "input": {"path": "claw_chain_b.txt"}}
            ]},
            {"tool_calls": [
                {"name": "edit_file", "id": "t3",
                 "input": {"path": "claw_chain_b.txt",
                           "old_string": "step-one", "new_string": "step-four"}}
            ]},
            {"tool_calls": [
                {"name": "read_file", "id": "t4",
                 "input": {"path": "claw_chain_b.txt"}}
            ]},
            {"text": "Four step done."},
        ],
        assertions=[
            returncode_is(0),
            file_exists_with_content(CLAW_CWD / "claw_chain_b.txt", "step-four"),
            relay_saw_n_requests(5),
            relay_request_contains(4, "step-four"),
        ],
    ),
    TestCase(
        name="bridge_write_then_glob_finds_it",
        args=["prompt", "write a file then find it with glob"],
        timeout=45,
        tags=["bridge", "chain", "search"],
        bridge_responses=[
            {"tool_calls": [
                {"name": "write_file", "id": "tg1",
                 "input": {"path": "claw_chain_c.txt", "content": "glob-target"}}
            ]},
            {"tool_calls": [
                {"name": "glob_search", "id": "tg2",
                 "input": {"pattern": "claw_chain_c.txt"}}
            ]},
            {"text": "Found it."},
        ],
        assertions=[
            returncode_is(0),
            relay_saw_n_requests(3),
            relay_request_contains(2, "claw_chain_c.txt"),
        ],
    ),

    # ════════════════════════════════════════════════════════════════════════
    # BASH — shell execution tool
    # ════════════════════════════════════════════════════════════════════════

    TestCase(
        name="bridge_bash_echo_captured",
        args=["prompt", "run echo hello"],
        timeout=30,
        tags=["bridge", "bash"],
        bridge_responses=[
            {"tool_calls": [
                {"name": "bash", "id": "tb1",
                 "input": {"command": "echo 'bash-output-marker'"}}
            ]},
            {"text": "Ran bash."},
        ],
        assertions=[
            returncode_is(0),
            relay_saw_n_requests(2),
            relay_request_contains(1, "bash-output-marker"),
        ],
    ),
    TestCase(
        name="bridge_bash_writes_file",
        args=["prompt", "use bash to write a file"],
        timeout=30,
        tags=["bridge", "bash", "write"],
        bridge_responses=[
            {"tool_calls": [
                {"name": "bash", "id": "tb2",
                 "input": {"command": "echo 'bash-wrote-this' > claw_bash_out.txt"}}
            ]},
            {"text": "Done."},
        ],
        assertions=[
            returncode_is(0),
            file_exists(CLAW_CWD / "claw_bash_out.txt"),
        ],
    ),
    TestCase(
        name="bridge_bash_exit_code_in_result",
        args=["prompt", "run a failing command"],
        timeout=30,
        tags=["bridge", "bash", "error"],
        bridge_responses=[
            {"tool_calls": [
                {"name": "bash", "id": "tb3",
                 "input": {"command": "exit 42"}}
            ]},
            {"text": "Handled."},
        ],
        assertions=[
            returncode_is(0),
            relay_request_contains(1, "42"),
        ],
    ),
    TestCase(
        name="bridge_bash_then_read_file",
        args=["prompt", "bash write then claw read"],
        timeout=45,
        tags=["bridge", "bash", "chain"],
        bridge_responses=[
            {"tool_calls": [
                {"name": "bash", "id": "tb4",
                 "input": {"command": "echo 'bash-then-read' > claw_bash_append.txt"}}
            ]},
            {"tool_calls": [
                {"name": "read_file", "id": "tb5",
                 "input": {"path": "claw_bash_append.txt"}}
            ]},
            {"text": "Cross-tool chain done."},
        ],
        assertions=[
            returncode_is(0),
            relay_request_contains(2, "bash-then-read"),
        ],
    ),

    # ════════════════════════════════════════════════════════════════════════
    # SEARCH — glob_search and grep_search
    # ════════════════════════════════════════════════════════════════════════

    TestCase(
        name="bridge_glob_search_finds_rust_files",
        args=["prompt", "find all rust files"],
        timeout=30,
        tags=["bridge", "search"],
        bridge_responses=[
            {"tool_calls": [
                {"name": "glob_search", "id": "ts1",
                 "input": {"pattern": "**/*.rs"}}
            ]},
            {"text": "Found rust files."},
        ],
        assertions=[
            returncode_is(0),
            relay_saw_n_requests(2),
            relay_request_contains(1, ".rs"),
        ],
    ),
    TestCase(
        name="bridge_grep_search_finds_pattern",
        args=["prompt", "find files with a pattern"],
        timeout=30,
        tags=["bridge", "search"],
        seed_files={
            "claw_search_alpha.py": "def alpha():\n    return 'SEARCH_MARKER_TOKEN'\n",
            "claw_search_beta.py": "def beta():\n    pass\n",
        },
        bridge_responses=[
            {"tool_calls": [
                {"name": "grep_search", "id": "ts2",
                 "input": {"pattern": "SEARCH_MARKER_TOKEN"}}
            ]},
            {"text": "Found the pattern."},
        ],
        assertions=[
            returncode_is(0),
            relay_request_contains(1, "SEARCH_MARKER_TOKEN"),
            relay_request_contains(1, "claw_search_alpha.py"),
        ],
    ),
    TestCase(
        name="bridge_grep_search_no_match_returns_result",
        args=["prompt", "search for something that does not exist"],
        timeout=30,
        tags=["bridge", "search"],
        bridge_responses=[
            {"tool_calls": [
                {"name": "grep_search", "id": "ts3",
                 "input": {"pattern": "ZZZNOMATCHZZZ_qxqxqxqx"}}
            ]},
            {"text": "No results found."},
        ],
        assertions=[
            returncode_is(0),
            relay_saw_n_requests(2),
        ],
    ),
    TestCase(
        name="bridge_glob_search_specific_file",
        args=["prompt", "find a specific file"],
        timeout=30,
        tags=["bridge", "search"],
        seed_files={"claw_search_gamma.txt": "content"},
        bridge_responses=[
            {"tool_calls": [
                {"name": "glob_search", "id": "ts4",
                 "input": {"pattern": "claw_search_gamma.txt"}}
            ]},
            {"text": "Glob done."},
        ],
        assertions=[
            returncode_is(0),
            relay_request_contains(1, "claw_search_gamma.txt"),
        ],
    ),

    # ════════════════════════════════════════════════════════════════════════
    # SAFETY — permission enforcement and path containment
    # ════════════════════════════════════════════════════════════════════════

    TestCase(
        name="bridge_read_only_blocks_write",
        args=["--permission-mode", "read-only", "prompt", "try to write something"],
        timeout=30,
        tags=["bridge", "safety"],
        bridge_responses=[
            {"tool_calls": [
                {"name": "write_file", "id": "toolu_w2",
                 "input": {"path": "claw_test_pm_blocked.txt",
                           "content": "should not exist"}}
            ]},
            {"text": "Attempted."},
        ],
        assertions=[
            file_does_not_exist(CLAW_CWD / "claw_test_pm_blocked.txt"),
            relay_request_contains(1, "error"),
        ],
    ),
    TestCase(
        name="bridge_read_only_allows_read",
        args=["--permission-mode", "read-only", "prompt", "read cargo.toml"],
        timeout=30,
        tags=["bridge", "safety"],
        bridge_responses=[
            {"tool_calls": [
                {"name": "read_file", "id": "toolu_ro_r",
                 "input": {"path": "Cargo.toml"}}
            ]},
            {"text": "Read ok."},
        ],
        assertions=[
            returncode_is(0),
            relay_saw_n_requests(2),
            relay_request_contains(1, "tool_result"),
        ],
    ),
    TestCase(
        name="bridge_read_only_blocks_bash",
        args=["--permission-mode", "read-only", "prompt", "run bash"],
        timeout=30,
        tags=["bridge", "safety"],
        bridge_responses=[
            {"tool_calls": [
                {"name": "bash", "id": "toolu_bash_blocked",
                 "input": {"command": "echo 'should be blocked'"}}
            ]},
            {"text": "Done."},
        ],
        assertions=[
            returncode_is(0),
            # Permission denied should come back as a tool error to the relay
            relay_request_contains(1, "error"),
        ],
    ),
    TestCase(
        name="bridge_path_escape_blocked",
        args=["prompt", "escape the project root"],
        timeout=30,
        tags=["bridge", "safety"],
        bridge_responses=[
            {"tool_calls": [
                {"name": "write_file", "id": "toolu_esc",
                 "input": {"path": "../../../tmp/claw_escaped.txt",
                           "content": "escaped"}}
            ]},
            {"text": "Attempted."},
        ],
        assertions=[
            returncode_is(0),
            file_does_not_exist(Path("/tmp/claw_escaped.txt")),
            relay_request_contains(1, "error"),
        ],
    ),
    TestCase(
        name="bridge_absolute_path_blocked",
        args=["prompt", "write to absolute path"],
        timeout=30,
        tags=["bridge", "safety"],
        bridge_responses=[
            {"tool_calls": [
                {"name": "write_file", "id": "toolu_abs",
                 "input": {"path": "/tmp/claw_absolute_escape.txt",
                           "content": "escaped"}}
            ]},
            {"text": "Attempted."},
        ],
        assertions=[
            returncode_is(0),
            file_does_not_exist(Path("/tmp/claw_absolute_escape.txt")),
            relay_request_contains(1, "error"),
        ],
    ),

    # ════════════════════════════════════════════════════════════════════════
    # ERROR RECOVERY — bad relay responses, partial data
    # ════════════════════════════════════════════════════════════════════════

    TestCase(
        name="bridge_relay_returns_unknown_tool",
        args=["prompt", "call unknown tool"],
        timeout=30,
        tags=["bridge", "error"],
        bridge_responses=[
            {"tool_calls": [
                {"name": "totally_fake_tool_xyz", "id": "toolu_fake",
                 "input": {"foo": "bar"}}
            ]},
            {"text": "Done."},
        ],
        assertions=[
            returncode_is(0),
            # Should get an error back in turn 2 request for the unknown tool
            relay_saw_n_requests(2),
        ],
    ),
    TestCase(
        name="bridge_relay_returns_tool_with_missing_required_arg",
        args=["prompt", "call tool with bad args"],
        timeout=30,
        tags=["bridge", "error"],
        bridge_responses=[
            # write_file requires "path" and "content" — omit "content"
            {"tool_calls": [
                {"name": "write_file", "id": "toolu_bad",
                 "input": {"path": "claw_danger_out.txt"}}
            ]},
            {"text": "Done."},
        ],
        assertions=[
            returncode_is(0),
            relay_saw_n_requests(2),
        ],
    ),
    TestCase(
        name="bridge_tool_then_text_then_done",
        args=["prompt", "mixed tool and text"],
        timeout=30,
        tags=["bridge", "error"],
        bridge_responses=[
            {"text": "I'll read the file.", "tool_calls": [
                {"name": "read_file", "id": "toolu_mix",
                 "input": {"path": "Cargo.toml"}}
            ]},
            {"text": "All done."},
        ],
        assertions=[
            returncode_is(0),
            stdout_contains("All done"),
        ],
    ),

    # ════════════════════════════════════════════════════════════════════════
    # STRESS — many relay turns, large payloads, deep tool chains
    # ════════════════════════════════════════════════════════════════════════

    TestCase(
        name="bridge_eight_turn_chain",
        args=["prompt", "do eight things"],
        timeout=90,
        tags=["bridge", "stress"],
        bridge_responses=[
            {"tool_calls": [{"name": "write_file", "id": f"t{i}",
                             "input": {"path": "claw_stress_counter.txt",
                                       "content": f"pass-{i}"}}]}
            for i in range(7)
        ] + [{"text": "Eight turns complete."}],
        assertions=[
            returncode_is(0),
            relay_saw_n_requests(8),
            file_exists_with_content(CLAW_CWD / "claw_stress_counter.txt", "pass-6"),
        ],
    ),
    TestCase(
        name="bridge_large_file_write_and_read",
        args=["prompt", "write and read a large file"],
        timeout=60,
        tags=["bridge", "stress"],
        bridge_responses=[
            {"tool_calls": [
                {"name": "write_file", "id": "tlarge1",
                 "input": {"path": "claw_stress_large.txt",
                           "content": ("X" * 50_000)}}
            ]},
            {"tool_calls": [
                {"name": "read_file", "id": "tlarge2",
                 "input": {"path": "claw_stress_large.txt"}}
            ]},
            {"text": "Large file round-trip done."},
        ],
        assertions=[
            returncode_is(0),
            file_exists(CLAW_CWD / "claw_stress_large.txt"),
            relay_saw_n_requests(3),
        ],
    ),
    TestCase(
        name="bridge_many_reads_from_same_file",
        args=["prompt", "read cargo.toml five times"],
        timeout=60,
        tags=["bridge", "stress"],
        bridge_responses=[
            {"tool_calls": [
                {"name": "read_file", "id": f"tmr{i}",
                 "input": {"path": "Cargo.toml"}}
            ]}
            for i in range(5)
        ] + [{"text": "Read five times."}],
        assertions=[
            returncode_is(0),
            relay_saw_n_requests(6),
        ],
    ),

    # ════════════════════════════════════════════════════════════════════════
    # SESSION — verify session files are persisted
    # ════════════════════════════════════════════════════════════════════════

    TestCase(
        name="bridge_session_file_created",
        args=["prompt", "simple session test"],
        timeout=30,
        tags=["bridge", "session"],
        bridge_responses=[{"text": "Session recorded."}],
        assertions=[
            returncode_is(0),
            session_file_was_created(),
        ],
    ),
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_test(tc: TestCase, binary: Path) -> TestResult:
    cleanup()

    if "no-auth" in tc.tags and has_anthropic_auth():
        return TestResult(tc.name, "skip", 0, "auth present; test verifies no-auth path")

    start = time.monotonic()
    relay: Optional[FakeRelay] = None
    try:
        if tc.bridge_responses is not None:
            proc, relay = run_with_fake_bridge(
                binary, tc.args, tc.bridge_responses, tc.timeout,
                seed_files=tc.seed_files,
            )
        else:
            for rel, content in tc.seed_files.items():
                (CLAW_CWD / rel).write_text(content)
            proc = subprocess.run(
                [str(binary)] + tc.args,
                capture_output=True, text=True,
                timeout=tc.timeout,
                cwd=str(CLAW_CWD),
                input=tc.stdin,
            )
    except subprocess.TimeoutExpired:
        return TestResult(tc.name, "timeout", time.monotonic() - start,
                          f"Exceeded {tc.timeout}s")
    except FileNotFoundError:
        return TestResult(tc.name, "transport", 0,
                          f"Binary not found: {binary}")

    duration = time.monotonic() - start
    for assertion in tc.assertions:
        err = assertion(proc.stdout, proc.stderr, proc.returncode, relay)
        if err:
            tail_out = proc.stdout.strip()[-200:] if proc.stdout else ""
            tail_err = proc.stderr.strip()[-300:] if proc.stderr else ""
            detail = err
            if tail_err:
                detail += f" | stderr: {tail_err}"
            if tail_out:
                detail += f" | stdout: {tail_out}"
            return TestResult(tc.name, "fail", duration, detail)

    return TestResult(tc.name, "pass", duration)


def print_human(results: list[TestResult]):
    print(f"\n{'TEST':<45} {'STATUS':<10} {'TIME':>7}  DETAIL")
    print("─" * 100)
    for r in results:
        icon = {
            "pass": "✅", "fail": "❌", "skip": "⊘",
            "timeout": "⏰", "transport": "🔌",
        }[r.status]
        detail = r.failure_reason[:55] if r.failure_reason else ""
        print(f"{r.name:<45} {icon} {r.status:<7} {r.duration_s:>5.1f}s  {detail}")
    passed = sum(1 for r in results if r.status == "pass")
    skipped = sum(1 for r in results if r.status == "skip")
    failed = len(results) - passed - skipped
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped ({len(results)} total)")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--only", help="Comma-separated tag or test name filter")
    p.add_argument("--json", action="store_true")
    p.add_argument("--list", action="store_true")
    p.add_argument("--binary", help="Path to claw binary")
    args = p.parse_args()

    binary = Path(args.binary) if args.binary else DEFAULT_BINARY

    tests = TESTS
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        tests = [t for t in TESTS
                 if t.name in wanted or any(tag in wanted for tag in t.tags)]

    if args.list:
        for t in tests:
            mode = "bridge" if t.bridge_responses is not None else "cli"
            print(f"{t.name:<45} tags={','.join(t.tags) or '-':<28} mode={mode}")
        return

    if not binary.exists():
        print(f"Claw binary not found at {binary}", file=sys.stderr)
        print("Build: cd pythonExperimentTool/claw-code/rust && cargo build", file=sys.stderr)
        sys.exit(1)

    results = [run_test(t, binary) for t in tests]

    if args.json:
        print(json.dumps([r.__dict__ for r in results], indent=2))
    else:
        print_human(results)

    sys.exit(0 if all(r.status in ("pass", "skip") for r in results) else 1)


if __name__ == "__main__":
    main()

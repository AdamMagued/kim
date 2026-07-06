"""Behavioral regression tests for the MCP/codex-engine bug-sweep fixes.

One test (class) per finding, tagged with the BUGSWEEP_mcp.md id:

  C1 — 64 KiB StreamReader line limit crashed both codex transports
  C2 — timed-out HITL stdin read left a zombie thread that stole the next line
  C5 — _CodexProxy compaction cache grew unbounded
  H1 — dead browser context never reset (web tools wedged permanently)
  H2 — SSRF allowlist blocked 127.0.0.1 but permitted `localhost`
  H3 — lint_file resolved ruff on the parent PATH but executed with sandbox PATH
  G1 — `git clean --force` (long form) evaded the destructive-git escalation
  G2 — run_powershell dispatched with no policy analysis at all
  G4 — glued `--opt=/path` args skipped the chokepoint path scan
  G5 — acceptForSession signature was the bare tool name
  G6 — _analyze_shell only vetted the first command of a chained string
  M1 — observe_ui click map diverged from the displayed element set
  M2 — PowerShell scripts were vetted with the POSIX checker (backtick/$())
  M3 — search patterns beginning with '-' were parsed as flags
  M4 — the inline-code blocklist was applied to whole files
  M5 — subprocess.TimeoutExpired from _run_gh was uncaught
  L1 — non-recursive list_dir crashed on a broken symlink
  L2 — off-by-one field guard in observe_ui row parse
  L3 — grid_rows unbounded in annotated screenshot
  L4 — shell timeout kill did not kill the process tree
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

# Make sure the repo root is importable regardless of cwd
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import codex_engine.app_server as app_server_mod
import orchestrator.codex_bridge_service as bridge_mod
from codex_engine.app_server import AppServerClient, Notification
from mcp_server import policy as policy_mod
from mcp_server.tools import code as code_mod
from mcp_server.tools import files as files_mod
from mcp_server.tools import github as github_mod
from mcp_server.tools import search as search_mod
from mcp_server.tools import shell as shell_mod
from mcp_server.tools import ui_observe as ui_mod
from mcp_server.tools import web as web_mod


# ---------------------------------------------------------------------------
# C1 — big JSON-RPC lines must not kill the app-server reader
# ---------------------------------------------------------------------------

_BIG_LINE_SERVER = r"""
import json, sys
big = {"jsonrpc": "2.0", "method": "item/agentMessage/delta",
       "params": {"delta": "x" * 200_000}}
print(json.dumps(big))
print(json.dumps({"jsonrpc": "2.0", "method": "turn/completed", "params": {}}))
sys.stdout.flush()
"""


class C1AppServerBigLineTest(unittest.IsolatedAsyncioTestCase):
    async def test_notification_line_over_64k_survives(self):
        """A single >64 KiB JSON-RPC line used to raise ValueError out of the
        reader ('Separator is found, but chunk is longer than limit')."""
        client = AppServerClient([sys.executable, "-c", _BIG_LINE_SERVER])
        await client.start()
        try:
            got: list[str] = []
            async for msg in client.events():
                if isinstance(msg, Notification):
                    got.append(msg.method)
                if "turn/completed" in got:
                    break
            self.assertIn("item/agentMessage/delta", got)
            self.assertIn("turn/completed", got)
        finally:
            await client.stop()

    async def test_line_over_configured_limit_degrades_gracefully(self):
        """Even a line above the (patched-small) limit must not end the
        events() stream — the reader skips it and keeps going."""
        with patch.object(app_server_mod, "STREAM_LIMIT", 64 * 1024):
            client = AppServerClient([sys.executable, "-c", _BIG_LINE_SERVER])
            await client.start()
            try:
                got: list[str] = []
                async for msg in client.events():
                    if isinstance(msg, Notification):
                        got.append(msg.method)
                    if "turn/completed" in got:
                        break
                # The oversized delta is (acceptably) dropped, but the reader
                # survived to deliver the next line.
                self.assertIn("turn/completed", got)
            finally:
                await client.stop()


class C1BridgeStreamTest(unittest.IsolatedAsyncioTestCase):
    async def test_iter_stream_lines_tolerates_over_limit_line(self):
        script = (
            "import sys; sys.stdout.write('y' * 200000 + '\\n'); "
            "sys.stdout.write('tail-line\\n'); sys.stdout.flush()"
        )
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", script,
            stdout=asyncio.subprocess.PIPE,
            limit=64 * 1024,  # simulate the old default cap
        )
        assert proc.stdout is not None
        lines = [ln async for ln in bridge_mod._iter_stream_lines(proc.stdout)]
        await proc.wait()
        self.assertIn("tail-line", lines)


# ---------------------------------------------------------------------------
# C2 — timed-out HITL read must not steal/deny the next decision
# ---------------------------------------------------------------------------

class C2HitlStdinTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bridge_mod._pending_stdin_read = None
        self._r, self._w = os.pipe()
        self._stdin = os.fdopen(self._r, "r")

    def tearDown(self):
        bridge_mod._pending_stdin_read = None
        try:
            os.close(self._w)
        except OSError:
            pass
        self._stdin.close()

    async def test_timeout_then_next_prompt_gets_the_decision(self):
        with patch.object(sys, "stdin", self._stdin):
            # Prompt 1: nothing arrives → deny on timeout.
            self.assertFalse(await bridge_mod._await_hitl_decision(timeout=0.2))
            # The read is parked, not orphaned.
            self.assertIsNotNone(bridge_mod._pending_stdin_read)
            # The supervisor answers prompt 2; the parked read delivers it.
            os.write(self._w, b'{"type": "hitl_approve", "approved": true}\n')
            self.assertTrue(await bridge_mod._await_hitl_decision(timeout=5.0))
            self.assertIsNone(bridge_mod._pending_stdin_read)

    async def test_late_decision_for_timed_out_prompt_is_discarded(self):
        with patch.object(sys, "stdin", self._stdin):
            self.assertFalse(await bridge_mod._await_hitl_decision(timeout=0.2))
            # A LATE approval for the timed-out prompt arrives before the
            # next prompt starts...
            os.write(self._w, b'{"type": "hitl_approve", "approved": true}\n')
            fut = bridge_mod._pending_stdin_read
            assert fut is not None
            await asyncio.wrap_future(fut)  # let the reader thread finish
            # ...so the next prompt must NOT be approved by it; it waits for
            # its own line (a denial here).
            os.write(self._w, b'{"type": "hitl_approve", "approved": false}\n')
            self.assertFalse(await bridge_mod._await_hitl_decision(timeout=5.0))


# ---------------------------------------------------------------------------
# C5 — compaction cache is bounded
# ---------------------------------------------------------------------------

class C5CompactionCacheBoundTest(unittest.TestCase):
    def test_cache_evicts_fifo_beyond_cap(self):
        from codex_engine.engine import _CodexProxy

        proxy = _CodexProxy.__new__(_CodexProxy)
        proxy._compaction_cache = {}
        for i in range(_CodexProxy._COMPACTION_CACHE_MAX + 8):
            proxy._cache_compaction(i, {"n": i})
        self.assertLessEqual(
            len(proxy._compaction_cache), _CodexProxy._COMPACTION_CACHE_MAX
        )
        # Newest entries survive; oldest were evicted.
        self.assertIn(_CodexProxy._COMPACTION_CACHE_MAX + 7, proxy._compaction_cache)
        self.assertNotIn(0, proxy._compaction_cache)


# ---------------------------------------------------------------------------
# C3 — a timed-out `codex --version` child must be reaped, not orphaned
# ---------------------------------------------------------------------------

class C3VersionTimeoutReapTest(unittest.IsolatedAsyncioTestCase):
    async def test_hung_version_child_is_killed(self):
        from orchestrator import codex_appserver_transport as transport

        # A "binary" that ignores --version and sleeps far past the timeout.
        hang = sys.executable
        spawned: dict[str, asyncio.subprocess.Process] = {}
        real_exec = asyncio.create_subprocess_exec

        async def _capture(*args, **kwargs):
            proc = await real_exec(
                hang, "-c", "import time; time.sleep(30)", **kwargs
            )
            spawned["proc"] = proc
            return proc

        async def _timeout(coro, *a, **k):
            # Close the wrapped coroutine (as real wait_for cancellation would)
            # so it doesn't leak an "never awaited" warning, then time out.
            if asyncio.iscoroutine(coro):
                coro.close()
            raise asyncio.TimeoutError

        with patch.object(transport.asyncio, "wait_for", _timeout), \
                patch.object(transport.asyncio, "create_subprocess_exec", _capture):
            ok, msg = await transport.check_binary_version("/fake/codex")

        # On a failed version check we still allow the run (fail-open)...
        self.assertTrue(ok)
        self.assertIsNone(msg)
        # ...but the spawned child must have been reaped, not left running.
        proc = spawned["proc"]
        await asyncio.wait_for(proc.wait(), timeout=5)
        self.assertIsNotNone(proc.returncode)


# ---------------------------------------------------------------------------
# H1 — a dead browser context is reset and reconnected
# ---------------------------------------------------------------------------

class _DeadCtx:
    closed = False

    @property
    def pages(self):
        raise RuntimeError("Target page, context or browser has been closed")

    async def close(self):
        _DeadCtx.closed = True


class _GoodPage:
    async def evaluate(self, _expr):
        return 1


class _GoodCtx:
    def __init__(self):
        self.pages = [_GoodPage()]


class H1DeadContextResetTest(unittest.IsolatedAsyncioTestCase):
    async def test_dead_context_triggers_reconnect(self):
        good_ctx = _GoodCtx()

        async def fake_connect():
            web_mod._browser_ctx = good_ctx

        with patch.object(web_mod, "_playwright", object()), \
                patch.object(web_mod, "_browser_ctx", _DeadCtx()), \
                patch.object(web_mod, "_active_page", None), \
                patch.object(web_mod, "_connect_browser_ctx", fake_connect):
            await web_mod._ensure_browser()
            self.assertIs(web_mod._browser_ctx, good_ctx)
            self.assertIsInstance(web_mod._active_page, _GoodPage)
            self.assertTrue(_DeadCtx.closed)


# ---------------------------------------------------------------------------
# H2 — localhost is an SSRF target
# ---------------------------------------------------------------------------

class H2LocalhostSsrfTest(unittest.TestCase):
    def test_localhost_blocked(self):
        self.assertTrue(web_mod._is_ssrf_target("http://localhost:9333/json"))

    def test_localhost_subdomain_blocked(self):
        self.assertTrue(web_mod._is_ssrf_target("http://cdp.localhost/x"))

    def test_localhost_trailing_dot_blocked(self):
        self.assertTrue(web_mod._is_ssrf_target("http://localhost./x"))

    def test_public_domain_allowed(self):
        self.assertFalse(web_mod._is_ssrf_target("https://example.com/"))

    def test_numeric_loopback_still_blocked(self):
        self.assertTrue(web_mod._is_ssrf_target("http://127.0.0.1:9333/"))


# ---------------------------------------------------------------------------
# H3 — lint_file runs the linter by absolute path
# ---------------------------------------------------------------------------

class H3LintAbsolutePathTest(unittest.IsolatedAsyncioTestCase):
    async def test_linter_outside_sandbox_path_executes(self):
        with TemporaryDirectory() as td:
            tool_dir = Path(td) / "bin"
            tool_dir.mkdir()
            fake_ruff = tool_dir / "ruff"
            fake_ruff.write_text("#!/bin/sh\necho fake-ruff-ran\nexit 0\n")
            fake_ruff.chmod(fake_ruff.stat().st_mode | stat.S_IEXEC)
            target = Path(td) / "target.py"
            target.write_text("x = 1\n")

            env = dict(os.environ, PATH=f"{tool_dir}:{os.environ.get('PATH', '')}")
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(code_mod, "validate_path", lambda p: Path(p)):
                result = await code_mod.handle_lint_file({"path": str(target)})

        # tool_dir is NOT on the child's sandbox PATH — only an absolute-path
        # invocation can have produced output.
        self.assertIn("[ruff]", result)
        self.assertIn("fake-ruff-ran", result)
        self.assertNotIn("is not installed", result)


# ---------------------------------------------------------------------------
# G1 — git clean --force escalates like -f
# ---------------------------------------------------------------------------

class G1GitCleanForceTest(unittest.TestCase):
    def test_long_form_force_flagged(self):
        self.assertIn(
            "git_clean_force",
            policy_mod._git_escalations(["git", "clean", "--force", "-d"]),
        )

    def test_short_form_still_flagged(self):
        self.assertIn(
            "git_clean_force", policy_mod._git_escalations(["git", "clean", "-fd"])
        )

    def test_dry_run_not_flagged(self):
        self.assertEqual(
            [], policy_mod._git_escalations(["git", "clean", "-n", "-d"])
        )


# ---------------------------------------------------------------------------
# G2 — run_powershell always requires approval
# ---------------------------------------------------------------------------

class G2PowershellEscalatesTest(unittest.TestCase):
    def test_run_powershell_is_approve(self):
        decision = policy_mod.enforce("run_powershell", {"script": "Get-ChildItem"})
        self.assertEqual(decision.action, "approve")
        self.assertIn("powershell_script_unanalyzed", decision.escalations)
        # Session-cache key is scoped to the exact script, not the tool name.
        self.assertIn("Get-ChildItem", decision.signature)


# ---------------------------------------------------------------------------
# G4 — glued --opt=/path args are path-checked
# ---------------------------------------------------------------------------

class G4GluedPathArgTest(unittest.TestCase):
    def test_glued_sensitive_path_denied(self):
        err = policy_mod._scan_path_tokens(["cat", "--file=/etc/shadow"], None)
        self.assertIsNotNone(err)

    def test_glued_non_path_value_ok(self):
        self.assertIsNone(
            policy_mod._scan_path_tokens(["grep", "--color=never", "x"], None)
        )


# ---------------------------------------------------------------------------
# G5 — acceptForSession signatures carry the reviewed path
# ---------------------------------------------------------------------------

class G5SessionSignatureScopeTest(unittest.TestCase):
    def test_write_file_signature_includes_path(self):
        from mcp_server.config import PROJECT_ROOT

        target = str(PROJECT_ROOT / "some_file.txt")
        decision = policy_mod.enforce("write_file", {"path": target, "content": "x"})
        self.assertNotEqual(decision.signature, "write_file")
        self.assertIn(target, decision.signature)


# ---------------------------------------------------------------------------
# G6 — every chained segment is vetted
# ---------------------------------------------------------------------------

class G6ChainedSegmentsTest(unittest.TestCase):
    def test_denylisted_binary_after_chain_operator_denied(self):
        analysis = policy_mod._analyze_shell("ls && rm -rf scratch", None)
        self.assertEqual(analysis.deny_reason, "denylisted_binary")

    def test_single_safe_command_still_low(self):
        analysis = policy_mod._analyze_shell("ls -la", None)
        self.assertEqual(analysis.deny_reason, "")
        self.assertEqual(analysis.effective_risk, "low")

    def test_escalation_from_second_segment_surfaces(self):
        analysis = policy_mod._analyze_shell("ls ; git push --force", None)
        self.assertIn("git_force_push", analysis.escalations)

    def test_glued_operator_still_escalates(self):
        # shlex keeps "ls;" as one token; the unrecognized "binary" escalates
        # (fail-safe) instead of silently passing as plain `ls`.
        analysis = policy_mod._analyze_shell("ls; git push --force", None)
        self.assertTrue(analysis.escalations)


# ---------------------------------------------------------------------------
# M1 — click map matches the displayed element set
# ---------------------------------------------------------------------------

class M1ClickMapConsistencyTest(unittest.TestCase):
    def test_displayed_ids_are_clickable(self):
        els = [
            ui_mod.UIElement("e1", "AXStaticText", "text-1", "", "", 0, 0, 10, 10),
            ui_mod.UIElement("e2", "AXStaticText", "text-2", "", "", 0, 20, 10, 10),
            ui_mod.UIElement("e3", "AXTextField", "field", "", "", 0, 40, 10, 10),
        ]
        out = ui_mod._format_observation("TestApp", "Win", els, limit=2)
        # The text field sorts first, so it is displayed...
        self.assertIn("e3", out)
        # ...and must be in the click map (it was NOT, pre-fix).
        self.assertIn("e3", ui_mod._LAST_ELEMENTS)
        shown = {eid for eid in ("e1", "e2", "e3") if f"- {eid}:" in out}
        self.assertEqual(shown, set(ui_mod._LAST_ELEMENTS.keys()))


# ---------------------------------------------------------------------------
# M2 — PowerShell syntax is not blocked as POSIX substitution
# ---------------------------------------------------------------------------

class M2PowershellVettingTest(unittest.TestCase):
    def test_backtick_escape_allowed_in_ps_mode(self):
        script = 'Write-Output "a`nb"'
        self.assertIsNone(
            shell_mod._check_blocked(script, allow_chaining=True, powershell=True)
        )

    def test_subexpression_allowed_in_ps_mode(self):
        script = 'Write-Output "$(Get-Date)"'
        self.assertIsNone(
            shell_mod._check_blocked(script, allow_chaining=True, powershell=True)
        )

    def test_backtick_still_blocked_in_posix_mode(self):
        self.assertIsNotNone(
            shell_mod._check_blocked("echo `rm -rf /tmp/x`", allow_chaining=True)
        )

    def test_deny_commands_still_blocked_in_ps_mode(self):
        self.assertIsNotNone(
            shell_mod._check_blocked("rm -rf /tmp/x", allow_chaining=True, powershell=True)
        )


# ---------------------------------------------------------------------------
# M3 — dash-leading search patterns
# ---------------------------------------------------------------------------

class M3DashPatternTest(unittest.IsolatedAsyncioTestCase):
    async def test_arrow_pattern_searches_instead_of_erroring(self):
        import shutil as _shutil

        if not (_shutil.which("rg") or _shutil.which("grep")):
            self.skipTest("neither rg nor grep available")
        with TemporaryDirectory() as td:
            (Path(td) / "sample.php").write_text("$obj->getValue();\n")
            with patch.object(search_mod, "validate_path", lambda p: Path(p)):
                result = await search_mod.handle_search_in_files(
                    {"pattern": "->getValue", "path": td}
                )
        self.assertNotIn("ERROR", result)
        self.assertIn("getValue", result)


# ---------------------------------------------------------------------------
# M4 — real script files are runnable (no blocklist on file mode)
# ---------------------------------------------------------------------------

class M4FileModeBlocklistTest(unittest.IsolatedAsyncioTestCase):
    async def test_py_file_with_import_os_runs(self):
        with TemporaryDirectory() as td:
            script = Path(td) / "real_script.py"
            script.write_text("import os\nprint('script-ran', len(os.sep))\n")
            with patch.object(code_mod, "validate_path", lambda p: Path(p)):
                result = await code_mod.handle_run_python(
                    {"file": str(script), "cwd": td}
                )
        self.assertNotIn("BLOCKED", result)
        self.assertIn("script-ran", result)

    def test_inline_blocklist_still_active(self):
        self.assertIsNotNone(code_mod._check_code_blocked("import os"))


# ---------------------------------------------------------------------------
# M5 — a hung gh degrades to a failed CompletedProcess
# ---------------------------------------------------------------------------

class M5GhTimeoutTest(unittest.TestCase):
    def test_timeout_returns_nonzero_result(self):
        def _boom(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="gh", timeout=15)

        with patch.object(github_mod.subprocess, "run", _boom):
            result = github_mod._run_gh(["auth", "status"], timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timed out", result.stderr)


# ---------------------------------------------------------------------------
# L1 — broken symlink does not crash list_dir
# ---------------------------------------------------------------------------

class L1BrokenSymlinkTest(unittest.IsolatedAsyncioTestCase):
    async def test_non_recursive_listing_survives_dangling_symlink(self):
        with TemporaryDirectory() as td:
            (Path(td) / "ok.txt").write_text("x")
            os.symlink(Path(td) / "does-not-exist", Path(td) / "dangling")
            with patch.object(files_mod, "validate_path", lambda p: Path(p)):
                result = await files_mod.handle_list_dir({"path": td})
        self.assertIn("ok.txt", result)
        self.assertIn("dangling", result)


# ---------------------------------------------------------------------------
# L2 — a 9-field EL row is skipped, not fatal
# ---------------------------------------------------------------------------

class L2RowParseGuardTest(unittest.TestCase):
    def test_nine_field_row_skipped(self):
        raw = "APP\tTestApp\tWin\n" + "\t".join(
            ["EL", "1", "AXButton", "OK", "", "", "1", "2", "3"]  # 9 fields
        )
        app, _win, elements = ui_mod._parse_elements(raw)
        self.assertEqual(app, "TestApp")
        self.assertEqual(elements, [])

    def test_ten_field_row_parsed(self):
        raw = "APP\tTestApp\tWin\n" + "\t".join(
            ["EL", "1", "AXButton", "OK", "", "", "1", "2", "30", "20"]
        )
        _app, _win, elements = ui_mod._parse_elements(raw)
        self.assertEqual(len(elements), 1)
        self.assertEqual(elements[0].role, "AXButton")


# ---------------------------------------------------------------------------
# L3 — grid_rows is capped
# ---------------------------------------------------------------------------

class L3GridRowsCapTest(unittest.TestCase):
    def _annotate(self):
        # conftest stubs `PIL`/`PIL.Image` when not pre-imported, which makes
        # screen_annotator (from PIL import ImageDraw) unimportable. Skip on
        # the stub rather than fail; the fix is a pure bound check.
        try:
            from PIL import Image, ImageDraw  # noqa: F401

            if not (hasattr(ImageDraw, "Draw") and hasattr(Image, "new")):
                raise ImportError("stubbed PIL")
            from mcp_server.tools.screen_annotator import annotate_screenshot
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"real Pillow (ImageDraw) not importable: {exc}")
        return Image, annotate_screenshot

    def test_huge_grid_rows_rejected(self):
        Image, annotate_screenshot = self._annotate()
        img = Image.new("RGB", (100, 100))
        with self.assertRaises(ValueError):
            annotate_screenshot(img, grid_rows=100000)

    def test_default_grid_still_works(self):
        Image, annotate_screenshot = self._annotate()
        img = Image.new("RGB", (100, 100))
        annotated, grid = annotate_screenshot(img)
        self.assertTrue(grid)


# ---------------------------------------------------------------------------
# L4 — timeout kill reaches the whole process group
# ---------------------------------------------------------------------------

@unittest.skipIf(sys.platform == "win32", "process groups are POSIX-only")
class L4ProcessTreeKillTest(unittest.IsolatedAsyncioTestCase):
    async def test_kill_process_tree_kills_grandchild(self):
        # sh spawns a grandchild sleep; killing only sh would orphan it.
        proc = await asyncio.create_subprocess_shell(
            "sleep 30 & echo $!; wait",
            stdout=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        assert proc.stdout is not None
        grandchild_pid = int((await proc.stdout.readline()).strip())
        shell_mod._kill_process_tree(proc)
        await asyncio.wait_for(proc.wait(), timeout=5)
        # The grandchild must be gone too (signal 0 probes existence).
        for _ in range(50):
            try:
                os.kill(grandchild_pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.1)
        else:
            os.kill(grandchild_pid, 9)  # cleanup before failing
            self.fail("grandchild survived _kill_process_tree")


if __name__ == "__main__":
    unittest.main()

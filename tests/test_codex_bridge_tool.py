"""
Regression tests for mcp_server/tools/codex_bridge.py — cmd-builder behaviors.

Three contracts guarded here:

1. bypass_flag_gated_by_env
   --dangerously-bypass-approvals-and-sandbox is absent when
   KIM_CODEX_BYPASS_SANDBOX is unset and present when it equals '1'.

2. cwd_defaults_to_temp_not_getcwd
   When cwd=None, working_dir is derived from tempfile.mkdtemp(prefix='kim-codex-work-'),
   never from os.getcwd().

3. argv_tail_is_C_cwd_task
   The final three elements of the built command are always ['-C', working_dir, task].
"""

from __future__ import annotations

import ast
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

_CODEX_BRIDGE = (
    Path(__file__).resolve().parent.parent / "mcp_server" / "tools" / "codex_bridge.py"
)


# ---------------------------------------------------------------------------
# Shared helper — mirrors the one in sibling test files
# ---------------------------------------------------------------------------

def _get_function_source(source_text: str, func_name: str) -> str:
    """Return the full source of a top-level (async) function."""
    tree = ast.parse(source_text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            lines = source_text.splitlines()
            return "\n".join(lines[node.lineno - 1 : node.end_lineno])
    raise ValueError(f"Function {func_name!r} not found in source")


# ---------------------------------------------------------------------------
# Static (source-level) contracts
# ---------------------------------------------------------------------------

class TestCmdBuilderStatic(unittest.TestCase):
    """Source-level regression guards for the cmd-building logic."""

    def setUp(self):
        self.source = _CODEX_BRIDGE.read_text(encoding="utf-8")
        self.fn_source = _get_function_source(self.source, "run_codex_subtask")

    # -- bypass_flag_gated_by_env (static) -----------------------------------

    def test_bypass_flag_reads_KIM_CODEX_BYPASS_SANDBOX(self):
        """The bypass sentinel must be read from KIM_CODEX_BYPASS_SANDBOX."""
        self.assertIn("KIM_CODEX_BYPASS_SANDBOX", self.fn_source,
                      "KIM_CODEX_BYPASS_SANDBOX env var lookup not found in run_codex_subtask")

    def test_bypass_flag_literal_string_in_source(self):
        """The dangerous flag literal must be present in source (it is conditionally appended)."""
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", self.fn_source,
                      "--dangerously-bypass-approvals-and-sandbox flag not found in source")

    def test_bypass_flag_is_conditional(self):
        """The dangerous flag must be inside an if-branch, not unconditionally appended."""
        tree = ast.parse(self.fn_source)
        # Walk AST: find an If node whose body contains a string that ends in '-sandbox'
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            branch_src = ast.get_source_segment(self.fn_source, node)
            if branch_src and "--dangerously-bypass-approvals-and-sandbox" in branch_src:
                return  # found a conditional that contains the flag
        self.fail(
            "--dangerously-bypass-approvals-and-sandbox is not inside any if-branch — "
            "it may be appended unconditionally"
        )

    # -- argv_tail_is_C_cwd_task (static) ------------------------------------

    def test_argv_ends_with_dash_C_working_dir_task(self):
        """-C, working_dir, and task must appear together at the end of cmd."""
        # The source must contain the pattern: cmd += ['-C', working_dir, task]
        # or cmd.extend(['-C', working_dir, task]) or similar construction.
        has_dash_c = "'-C'" in self.fn_source or '"-C"' in self.fn_source
        self.assertTrue(has_dash_c, "'-C' flag not found in run_codex_subtask cmd construction")

    def test_task_is_last_argv_element(self):
        """'task' must be the final element in the cmd list construction."""
        # Look for the list literal that closes with `task`
        tree = ast.parse(self.fn_source)
        found = False
        for node in ast.walk(tree):
            # Look for augmented-assign (cmd +=) or direct List containing '-C', working_dir, task
            if isinstance(node, ast.List):
                elts = node.elts
                if len(elts) >= 3:
                    # Last element is Name('task')
                    last = elts[-1]
                    second_last = elts[-2]
                    third_last = elts[-3]
                    last_is_task = isinstance(last, ast.Name) and last.id == "task"
                    second_is_working_dir = isinstance(second_last, ast.Name) and second_last.id == "working_dir"
                    third_is_dash_c = (
                        isinstance(third_last, ast.Constant) and third_last.value == "-C"
                    )
                    if last_is_task and second_is_working_dir and third_is_dash_c:
                        found = True
        self.assertTrue(found,
                        "cmd list ending with ['-C', working_dir, task] not found in AST of run_codex_subtask")

    # -- cwd_defaults_to_temp_not_getcwd (static) ----------------------------

    def test_cwd_fallback_uses_mkdtemp_not_getcwd(self):
        """When cwd is None, the fallback must be mkdtemp, not os.getcwd()."""
        self.assertIn("mkdtemp", self.fn_source,
                      "tempfile.mkdtemp not found as cwd fallback in run_codex_subtask")

    def test_cwd_fallback_prefix_is_kim_codex_work(self):
        """The mkdtemp call for the working dir must use prefix='kim-codex-work-'."""
        self.assertIn("kim-codex-work-", self.fn_source,
                      "mkdtemp prefix 'kim-codex-work-' not found in run_codex_subtask")

    def test_getcwd_not_used_as_fallback(self):
        """os.getcwd() must NOT be called as the cwd fallback (#1 security constraint)."""
        # os.getcwd() is forbidden as the fallback for working_dir
        # Check there's no assignment like: working_dir = ... os.getcwd() ...
        tree = ast.parse(self.fn_source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            # Check that none of the targets is 'working_dir' with getcwd() on the rhs
            targets_working_dir = any(
                isinstance(t, ast.Name) and t.id == "working_dir"
                for t in node.targets
            )
            if not targets_working_dir:
                continue
            # Walk the value for getcwd
            for child in ast.walk(node.value):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "getcwd"
                ):
                    self.fail(
                        "os.getcwd() used as working_dir fallback in run_codex_subtask — "
                        "this leaks the app root as the Codex work dir (#1)"
                    )


# ---------------------------------------------------------------------------
# Runtime contracts (mock the subprocess so the function is callable)
# ---------------------------------------------------------------------------

def _make_fake_process(returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=returncode)
    # stdout/stderr are async iterables that yield nothing
    proc.stdout = _empty_async_iter()
    proc.stderr = _empty_async_iter()
    return proc


def _empty_async_iter():
    """Return an object whose __aiter__ yields no items."""
    class _EmptyIter:
        def __aiter__(self):
            return self
        async def __anext__(self):
            raise StopAsyncIteration
    return _EmptyIter()


class _FakeBrowserProvider:
    """Minimal stub — completes() is never called in these tests because the
    subprocess is mocked to exit immediately."""
    async def complete(self, **kwargs):
        return {"content": "{}"}


def _stub_heavy_imports():
    """Stub out modules that are not installed in the test environment."""
    for name in ("aiohttp",):
        if name not in sys.modules:
            stub = types.ModuleType(name)
            sys.modules[name] = stub


class TestCmdBuilderRuntime(unittest.IsolatedAsyncioTestCase):
    """End-to-end runtime tests that capture the actual argv built by run_codex_subtask."""

    def setUp(self):
        _stub_heavy_imports()

    async def _run_with_captured_cmd(
        self,
        task: str = "write hello.py",
        cwd: str | None = None,
        env_override: dict | None = None,
    ) -> list[str]:
        """
        Call run_codex_subtask with a fake binary and capture the cmd that
        was passed to asyncio.create_subprocess_exec.
        """
        # Import inside test so module-level stubs are in place first
        from mcp_server.tools.codex_bridge import run_codex_subtask

        captured: list[list[str]] = []

        fake_proc = _make_fake_process(returncode=0)

        async def fake_create_subprocess_exec(*args, **kwargs):
            captured.append(list(args))
            # After capture, simulate immediate exit
            fake_proc.returncode = 0
            return fake_proc

        fake_proxy = MagicMock()
        fake_proxy._bearer_token = "test-token"
        fake_proxy.start = AsyncMock(return_value=19999)
        fake_proxy.stop = AsyncMock()

        provider = _FakeBrowserProvider()
        # _sent_system_prompt attr expected by run_codex_subtask
        provider._sent_system_prompt = False

        # Use a real file so shutil.which / isabs path succeeds
        fake_binary = "/usr/bin/true"  # always exists on macOS/Linux

        base_env = dict(os.environ)
        base_env.pop("KIM_CODEX_BYPASS_SANDBOX", None)
        if env_override:
            base_env.update(env_override)

        with (
            patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec),
            patch("mcp_server.tools.codex_bridge._CodexProxy", return_value=fake_proxy),
            patch("shutil.rmtree"),
            patch("os.environ", base_env),
        ):
            await run_codex_subtask(
                task=task,
                browser_provider=provider,
                cwd=cwd,
                codex_binary=fake_binary,
            )

        return captured[0] if captured else []

    # -- bypass_flag_gated_by_env --------------------------------------------

    async def test_bypass_flag_absent_when_env_unset(self):
        """With KIM_CODEX_BYPASS_SANDBOX unset, the dangerous flag must NOT appear in argv."""
        cmd = await self._run_with_captured_cmd(env_override={})
        self.assertNotIn(
            "--dangerously-bypass-approvals-and-sandbox", cmd,
            "Sandbox bypass flag present even though KIM_CODEX_BYPASS_SANDBOX is unset"
        )

    async def test_bypass_flag_present_when_env_is_1(self):
        """With KIM_CODEX_BYPASS_SANDBOX=1, the dangerous flag MUST appear in argv."""
        cmd = await self._run_with_captured_cmd(
            env_override={"KIM_CODEX_BYPASS_SANDBOX": "1"}
        )
        self.assertIn(
            "--dangerously-bypass-approvals-and-sandbox", cmd,
            "Sandbox bypass flag absent even though KIM_CODEX_BYPASS_SANDBOX=1"
        )

    async def test_bypass_flag_absent_when_env_is_zero(self):
        """KIM_CODEX_BYPASS_SANDBOX=0 must NOT trigger the bypass flag."""
        cmd = await self._run_with_captured_cmd(
            env_override={"KIM_CODEX_BYPASS_SANDBOX": "0"}
        )
        self.assertNotIn(
            "--dangerously-bypass-approvals-and-sandbox", cmd,
            "Sandbox bypass flag present when KIM_CODEX_BYPASS_SANDBOX=0 (only '1' should enable it)"
        )

    # -- argv_tail_is_C_cwd_task ---------------------------------------------

    async def test_argv_tail_is_dash_C_cwd_task(self):
        """The last three elements of argv must be ['-C', <working_dir>, <task>]."""
        task = "write fibonacci.py"
        cwd = tempfile.mkdtemp(prefix="kim-test-")
        try:
            cmd = await self._run_with_captured_cmd(task=task, cwd=cwd)
            self.assertTrue(len(cmd) >= 3,
                            f"argv too short to contain [-C, cwd, task]: {cmd}")
            self.assertEqual(cmd[-1], task,
                             f"Last argv element should be task, got: {cmd[-1]!r}")
            self.assertEqual(cmd[-2], cwd,
                             f"Second-to-last argv element should be cwd={cwd!r}, got: {cmd[-2]!r}")
            self.assertEqual(cmd[-3], "-C",
                             f"Third-to-last argv element should be '-C', got: {cmd[-3]!r}")
        finally:
            import shutil
            shutil.rmtree(cwd, ignore_errors=True)

    async def test_argv_tail_task_with_bypass_flag(self):
        """Even with the bypass flag present, argv must still end with ['-C', cwd, task]."""
        task = "run tests"
        cwd = tempfile.mkdtemp(prefix="kim-test-")
        try:
            cmd = await self._run_with_captured_cmd(
                task=task,
                cwd=cwd,
                env_override={"KIM_CODEX_BYPASS_SANDBOX": "1"},
            )
            self.assertEqual(cmd[-3:], ["-C", cwd, task],
                             f"Expected argv tail [-C, cwd, task] but got: {cmd[-3:]}")
        finally:
            import shutil
            shutil.rmtree(cwd, ignore_errors=True)

    # -- cwd_defaults_to_temp_not_getcwd -------------------------------------

    async def test_cwd_none_uses_mkdtemp_not_getcwd(self):
        """When cwd=None, working_dir must be a tempfile.mkdtemp result, not os.getcwd()."""
        real_getcwd = os.getcwd()
        cmd = await self._run_with_captured_cmd(cwd=None)

        # cmd[-2] is working_dir (the -C argument)
        self.assertTrue(len(cmd) >= 3,
                        f"argv too short — binary-not-found early exit? cmd={cmd}")
        working_dir = cmd[-2]

        # Must not be the process cwd
        self.assertNotEqual(
            working_dir, real_getcwd,
            f"working_dir is os.getcwd()={real_getcwd!r} but should be a fresh tempdir"
        )

    async def test_cwd_none_uses_kim_codex_work_prefix(self):
        """When cwd=None, the tempdir must have the 'kim-codex-work-' prefix."""
        cmd = await self._run_with_captured_cmd(cwd=None)
        self.assertTrue(len(cmd) >= 3,
                        f"argv too short — binary-not-found early exit? cmd={cmd}")
        working_dir = cmd[-2]
        dir_name = Path(working_dir).name
        self.assertTrue(
            dir_name.startswith("kim-codex-work-"),
            f"Expected working_dir name to start with 'kim-codex-work-', got: {dir_name!r}"
        )

    async def test_cwd_supplied_is_used_verbatim(self):
        """When cwd is explicitly supplied, working_dir must be exactly that value."""
        explicit_cwd = "/tmp/my-project"
        cmd = await self._run_with_captured_cmd(cwd=explicit_cwd)
        self.assertTrue(len(cmd) >= 3,
                        f"argv too short — binary-not-found early exit? cmd={cmd}")
        self.assertEqual(cmd[-2], explicit_cwd,
                         f"Supplied cwd={explicit_cwd!r} not passed through; got {cmd[-2]!r}")


if __name__ == "__main__":
    unittest.main()

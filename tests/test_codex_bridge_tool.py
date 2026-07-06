"""Behavioral argv-contract tests for the live codex spawn path (K4).

Contracts pinned here, by spawning a REAL fake codex binary through
orchestrator/codex_bridge_service._run_async and asserting the argv the child
process actually received:

1. base argv shape        — codex is invoked as ``exec --json …``
2. bypass flag gating     — --dangerously-bypass-approvals-and-sandbox appears
                            iff KIM_CODEX_BYPASS_SANDBOX == "1"
3. argv tail              — the final three args are always [-C, cwd, task]
4. git-repo gate          — outside a git repo, codex is NOT spawned unless
                            the user opted in (KIM_CODEX_SKIP_GIT_CHECK=1),
                            in which case --skip-git-repo-check is passed
5. provider gate          — a non-browser provider is rejected before spawn
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_bridge_harness import run_bridge

BYPASS_FLAG = "--dangerously-bypass-approvals-and-sandbox"


class TestCodexArgvContract(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="kim-argv-")
        self.tmp = Path(self._tmp.name)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    # -- base argv shape ------------------------------------------------------

    async def test_argv_starts_with_exec_json(self):
        result = await run_bridge(self.tmp)
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.capture["argv"][:2], ["exec", "--json"])

    async def test_argv_tail_is_dash_C_cwd_task(self):
        task = "write fibonacci.py"
        result = await run_bridge(self.tmp, task=task)
        argv = result.capture["argv"]
        self.assertEqual(
            argv[-3:],
            ["-C", result.project, task],
            f"argv must end with [-C, cwd, task]; got {argv}",
        )

    # -- bypass flag gating ---------------------------------------------------

    async def test_bypass_flag_absent_by_default(self):
        result = await run_bridge(self.tmp)
        self.assertNotIn(BYPASS_FLAG, result.capture["argv"])

    async def test_bypass_flag_present_when_env_is_1(self):
        result = await run_bridge(
            self.tmp, env={"KIM_CODEX_BYPASS_SANDBOX": "1"}
        )
        self.assertIn(BYPASS_FLAG, result.capture["argv"])
        # Even with the flag, the tail contract holds.
        self.assertEqual(result.capture["argv"][-3:-1][0], "-C")

    async def test_bypass_flag_absent_when_env_is_0(self):
        result = await run_bridge(
            self.tmp, env={"KIM_CODEX_BYPASS_SANDBOX": "0"}
        )
        self.assertNotIn(BYPASS_FLAG, result.capture["argv"])

    async def test_bypass_flag_absent_when_env_is_garbage(self):
        result = await run_bridge(
            self.tmp, env={"KIM_CODEX_BYPASS_SANDBOX": "yes"}
        )
        self.assertNotIn(BYPASS_FLAG, result.capture["argv"])

    # -- git-repo gate --------------------------------------------------------

    async def test_non_git_dir_refuses_to_spawn_without_optin(self):
        result = await run_bridge(self.tmp, git_repo=False)
        self.assertEqual(result.rc, 1)
        self.assertIsNone(
            result.capture,
            "codex was spawned in a non-git dir without explicit opt-in",
        )

    async def test_non_git_dir_with_optin_passes_skip_flag(self):
        result = await run_bridge(
            self.tmp,
            git_repo=False,
            env={"KIM_CODEX_SKIP_GIT_CHECK": "1"},
        )
        self.assertEqual(result.rc, 0)
        self.assertIn("--skip-git-repo-check", result.capture["argv"])

    async def test_git_dir_does_not_pass_skip_flag(self):
        result = await run_bridge(self.tmp)
        self.assertNotIn("--skip-git-repo-check", result.capture["argv"])

    # -- provider / binary gates ----------------------------------------------

    async def test_non_browser_provider_is_rejected_before_spawn(self):
        result = await run_bridge(self.tmp, provider_name="claude")
        self.assertEqual(result.rc, 2)
        self.assertIsNone(result.capture, "codex must not spawn for non-browser providers")

    async def test_missing_codex_binary_fails_before_spawn(self):
        result = await run_bridge(
            self.tmp, binary_override=str(self.tmp / "no-such-codex")
        )
        self.assertEqual(result.rc, 1)
        self.assertIsNone(result.capture)


if __name__ == "__main__":
    unittest.main()

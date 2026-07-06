"""Behavioral env-scoping tests for the live codex spawn path (K4/Q4).

The codex subprocess must receive ONLY the hardened minimal-allowlist env
built by orchestrator/codex_bridge_service.py — never a spread of the parent
``os.environ``. These tests spawn a REAL fake codex binary via _run_async and
assert on the env dict the child process actually observed.

(The old version of this file grepped the source of the dead
``run_codex_subtask`` — which pinned the contradictory ``**os.environ``
contract. Both are gone; this file now pins the hardened contract.)
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_bridge_harness import (
    EXPECTED_POSIX_ENV_KEYS,
    FAKE_BEARER_TOKEN,
    FAKE_PROXY_PORT,
    PLATFORM_INJECTED_ENV_KEYS,
    run_bridge,
)


class TestCodexEnvScoping(unittest.IsolatedAsyncioTestCase):
    """The env dict actually passed to the codex subprocess."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="kim-envscope-")
        self.tmp = Path(self._tmp.name)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_child_env_is_exactly_the_minimal_allowlist(self):
        """Child env keys == the hardened allowlist — nothing inherited beyond it."""
        result = await run_bridge(self.tmp)
        self.assertEqual(result.rc, 0)
        self.assertIsNotNone(result.capture, "fake codex binary was never spawned")
        child_keys = set(result.capture["env"].keys())
        # Everything on the allowlist must be present…
        self.assertTrue(
            EXPECTED_POSIX_ENV_KEYS <= child_keys,
            f"missing allowlist keys: {EXPECTED_POSIX_ENV_KEYS - child_keys}",
        )
        # …and nothing else may leak in (modulo OS-injected vars).
        extras = child_keys - EXPECTED_POSIX_ENV_KEYS - PLATFORM_INJECTED_ENV_KEYS
        self.assertEqual(
            extras,
            set(),
            f"parent env leaked into the codex subprocess: {sorted(extras)}",
        )

    async def test_parent_secrets_never_reach_the_child(self):
        """Secret-looking parent vars must not appear in the child env."""
        planted = {
            "OPENAI_API_KEY": "sk-real-parent-key-LEAK",
            "ANTHROPIC_API_KEY": "sk-ant-parent-LEAK",
            "AWS_SECRET_ACCESS_KEY": "aws-parent-LEAK",
            "GITHUB_TOKEN": "ghp_parent-LEAK",
            "KIM_TEST_PLANTED_SECRET": "planted-LEAK",
        }
        result = await run_bridge(self.tmp, env=planted)
        self.assertEqual(result.rc, 0)
        child_env = result.capture["env"]
        for key, value in planted.items():
            self.assertNotEqual(
                child_env.get(key),
                value,
                f"parent secret {key} leaked into the codex subprocess",
            )
        # And none of the planted *values* appear anywhere in the child env.
        for value in planted.values():
            self.assertNotIn(value, child_env.values())

    async def test_api_keys_are_the_per_run_proxy_bearer_token(self):
        """CODEX_API_KEY / OPENAI_API_KEY must be the proxy's per-run token."""
        result = await run_bridge(
            self.tmp, env={"OPENAI_API_KEY": "sk-real-parent-key"}
        )
        child_env = result.capture["env"]
        self.assertEqual(child_env["CODEX_API_KEY"], FAKE_BEARER_TOKEN)
        self.assertEqual(child_env["OPENAI_API_KEY"], FAKE_BEARER_TOKEN)

    async def test_base_url_points_at_the_local_proxy(self):
        result = await run_bridge(self.tmp)
        self.assertEqual(
            result.capture["env"]["OPENAI_BASE_URL"],
            f"http://127.0.0.1:{FAKE_PROXY_PORT}/v1",
        )

    async def test_codex_home_is_not_overridden(self):
        """C3: Kim must NOT point CODEX_HOME at a throwaway temp dir.

        The user's real codex home (config.toml, MCP servers, skills, rollout
        files) applies: no CODEX_HOME in the child env unless the parent
        exported one, in which case it is forwarded unchanged.
        """
        result = await run_bridge(self.tmp)
        self.assertNotIn("CODEX_HOME", result.capture["env"])

    async def test_parent_codex_home_is_forwarded(self):
        result = await run_bridge(self.tmp, env={"CODEX_HOME": "/custom/codex-home"})
        self.assertEqual(result.capture["env"].get("CODEX_HOME"), "/custom/codex-home")

    async def test_path_and_home_are_forwarded_from_parent(self):
        """The allowlisted basics come from the parent env, unmodified."""
        result = await run_bridge(self.tmp)
        child_env = result.capture["env"]
        self.assertEqual(child_env["PATH"], result.parent_env.get("PATH", ""))
        self.assertEqual(child_env["HOME"], result.parent_env.get("HOME", ""))

    async def test_parent_environ_is_not_mutated(self):
        """The spawn must scope env via the env= dict, not os.environ writes."""
        result = await run_bridge(self.tmp)
        for key in ("CODEX_API_KEY", "OPENAI_BASE_URL"):
            self.assertNotIn(
                key,
                result.parent_env,
                f"{key} was written into the parent os.environ instead of "
                "being scoped to the subprocess env dict",
            )


if __name__ == "__main__":
    unittest.main()

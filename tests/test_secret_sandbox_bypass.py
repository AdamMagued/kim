"""
Regression tests for the secret-file sandbox bypass (fix/secret-sandbox-bypass).

F1 (HIGH) — `search_in_files` returned the CONTENTS of files that
`config.validate_path` is designed to deny (id_rsa, *.pem, credentials*, .env,
.npmrc, …). Only the search *directory* was validated, never the files matched
inside it. These tests plant secret files and assert that `search_in_files`
returns NO line from any validate_path-denied file — across the ripgrep backend
AND the grep fallback — and that `find_files` does not disclose their names.

F2 (MEDIUM) — the shell arg path-scan (`policy._scan_path_tokens`) only checked
absolute / `~` / `..` tokens, so a plain relative secret read (`cat .env`,
`head id_rsa`) slipped through. Reachable when `shell.sandbox_mode` is off and
the command runs in the project root. The fix path-checks a relative token when
it names an existing file, delegating the deny decision to validate_path.

Every assertion is about what the tool DOES to a real call on real on-disk
files — the repo's behavioral-harness style.
"""
from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
import tempfile
import shutil

from mcp_server import config, policy
import mcp_server.tools.search as search


# Secret filenames whose CONTENT must never leak, paired with the sentinel
# token planted inside each. Covers dot-hidden (.env/.npmrc — only reached by
# the grep fallback) and non-dot globs (id_rsa/*.pem/credentials*).
_SECRETS = {
    "id_rsa": "SECRET_TOKEN_idrsa_KEYMATERIAL",
    "server.pem": "SECRET_TOKEN_pem_XYZ789",
    "credentials.json": "SECRET_TOKEN_credentials_ABC123",
    ".env": "SECRET_TOKEN_env_DOTENV",
    ".npmrc": "SECRET_TOKEN_npmrc_REGISTRY",
}
_SAFE_TOKEN = "SECRET_TOKEN_normal_OK"


class _PlantedSecretsMixin:
    """Create a temp project tree of secret + normal files, allowlisted so
    validate_path treats it as in-sandbox (the deny must come from the glob /
    directory rules, not from being out-of-sandbox)."""

    def setUp(self):  # noqa: N802
        self.d = Path(tempfile.mkdtemp()).resolve()
        for name, tok in _SECRETS.items():
            (self.d / name).write_text(f"prefix\n{tok}\n")
        (self.d / "normal.txt").write_text(f"{_SAFE_TOKEN}\n")
        self._orig_allowed = list(config.ALLOWED_PATHS)
        config.ALLOWED_PATHS.append(self.d)

    def tearDown(self):  # noqa: N802
        config.ALLOWED_PATHS[:] = self._orig_allowed
        shutil.rmtree(self.d, ignore_errors=True)

    def _search(self) -> str:
        return asyncio.run(
            search.handle_search_in_files(
                {"pattern": "SECRET_TOKEN", "path": str(self.d)}
            )
        )

    def _assert_no_secret_leak(self, out: str):
        for name, tok in _SECRETS.items():
            self.assertNotIn(tok, out, f"{name} content leaked: {out!r}")
            self.assertNotIn(name, out, f"{name} path leaked: {out!r}")
        self.assertIn(_SAFE_TOKEN, out, f"normal file wrongly dropped: {out!r}")


class SearchInFilesSecretLeakTests(_PlantedSecretsMixin, unittest.TestCase):
    def test_ripgrep_backend_drops_denied_files(self):
        if not search.check_tool_available("rg"):
            self.skipTest("ripgrep not installed")
        self._assert_no_secret_leak(self._search())

    def test_grep_fallback_drops_denied_files(self):
        if not search.check_tool_available("grep"):
            self.skipTest("grep not installed")
        orig = search.check_tool_available
        # Force the grep fallback (the report flagged it as strictly worse:
        # no hidden-file skip, so it additionally exposed .env / .npmrc).
        search.check_tool_available = lambda t: False if t == "rg" else orig(t)
        try:
            self._assert_no_secret_leak(self._search())
        finally:
            search.check_tool_available = orig

    def test_search_that_only_matches_secrets_reports_no_matches(self):
        # A pattern present ONLY in denied files must yield nothing, not a hit.
        out = asyncio.run(
            search.handle_search_in_files(
                {"pattern": "KEYMATERIAL", "path": str(self.d)}
            )
        )
        self.assertNotIn("KEYMATERIAL", out)
        self.assertIn("No matches found", out)


class FindFilesSecretNameTests(_PlantedSecretsMixin, unittest.TestCase):
    def test_find_files_hides_denied_names(self):
        out = asyncio.run(
            search.handle_find_files({"pattern": "*", "path": str(self.d)})
        )
        for name in _SECRETS:
            self.assertNotIn(name, out, f"find_files disclosed {name}: {out!r}")
        self.assertIn("normal.txt", out)


class PolicyRelativeSecretReadTests(unittest.TestCase):
    """F2 — `policy.enforce` must deny a shell command that reads a secret file
    via a plain relative token when that file exists in the working dir."""

    def setUp(self):  # noqa: N802
        self.d = Path(tempfile.mkdtemp()).resolve()
        for name in ("id_rsa", "server.pem", "credentials.json", ".env",
                     ".npmrc", "foo.py", "README.md"):
            (self.d / name).write_text("x\n")
        (self.d / "sub").mkdir()
        (self.d / "sub" / ".env").write_text("x\n")
        self._orig_allowed = list(config.ALLOWED_PATHS)
        config.ALLOWED_PATHS.append(self.d)

    def tearDown(self):  # noqa: N802
        config.ALLOWED_PATHS[:] = self._orig_allowed
        shutil.rmtree(self.d, ignore_errors=True)

    def _enforce(self, cmd: str):
        return policy.enforce("run_command", {"cmd": cmd, "cwd": str(self.d)})

    def test_relative_secret_reads_denied(self):
        for cmd in (
            "cat .env",
            "head id_rsa",
            "cat credentials.json",
            "cat ./server.pem",
            "grep API_KEY .npmrc",
            "cat sub/.env",
        ):
            d = self._enforce(cmd)
            self.assertEqual(d.action, "deny", f"{cmd!r} was not denied: {d}")

    def test_normal_relative_reads_still_allowed(self):
        # No false-denial of ordinary project files or grep patterns that
        # merely resemble a secret name but are not an existing file.
        for cmd in (
            "cat foo.py",
            "cat README.md",
            "grep credentials README.md",
            "echo hello",
        ):
            d = self._enforce(cmd)
            self.assertNotEqual(d.action, "deny", f"{cmd!r} wrongly denied: {d}")


if __name__ == "__main__":
    unittest.main()

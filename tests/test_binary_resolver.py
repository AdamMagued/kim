"""Unit tests for codex_engine.binary_resolver (#61).

Covers the fallback chain in isolation: CODEX_BIN env -> ~/.kim/bin/kimcli ->
`kimcli` on PATH -> `codex` on PATH -> bare "codex" last resort. Each test
patches only the inputs relevant to the level under test so a higher-priority
match never leaks in from the real environment.
"""

from __future__ import annotations

import os
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_engine.binary_resolver import resolve_codex_binary


class ResolveCodexBinaryTest(unittest.TestCase):
    def test_codex_bin_env_wins_over_everything(self) -> None:
        with patch.dict(os.environ, {"CODEX_BIN": "/custom/codex-bin"}):
            self.assertEqual(resolve_codex_binary(), "/custom/codex-bin")

    def test_blank_codex_bin_env_is_ignored(self) -> None:
        with patch.dict(os.environ, {"CODEX_BIN": "   "}):
            with patch("shutil.which", return_value=None):
                with patch.object(Path, "exists", return_value=False):
                    self.assertEqual(resolve_codex_binary(), "codex")

    def test_kim_bin_kimcli_used_when_env_unset_and_executable(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_BIN", None)
            fake_home = Path("/fake/home")
            kimcli_path = fake_home / ".kim" / "bin" / "kimcli"

            def fake_exists(self: Path) -> bool:
                return self == kimcli_path

            def fake_access(path, mode) -> bool:
                return Path(path) == kimcli_path

            with patch.object(Path, "home", return_value=fake_home):
                with patch.object(Path, "exists", fake_exists):
                    with patch("os.access", fake_access):
                        self.assertEqual(resolve_codex_binary(), str(kimcli_path))

    def test_kim_bin_kimcli_present_but_not_executable_is_skipped(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_BIN", None)
            fake_home = Path("/fake/home")
            kimcli_path = fake_home / ".kim" / "bin" / "kimcli"

            with patch.object(Path, "home", return_value=fake_home):
                with patch.object(Path, "exists", lambda self: self == kimcli_path):
                    with patch("os.access", return_value=False):
                        with patch("shutil.which", return_value="/usr/local/bin/kimcli"):
                            self.assertEqual(resolve_codex_binary(), "/usr/local/bin/kimcli")

    def test_which_kimcli_used_when_no_env_and_no_kim_bin(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_BIN", None)
            with patch.object(Path, "exists", return_value=False):
                with patch("shutil.which", side_effect=lambda name: (
                    "/opt/homebrew/bin/kimcli" if name == "kimcli" else None
                )):
                    self.assertEqual(resolve_codex_binary(), "/opt/homebrew/bin/kimcli")

    def test_which_codex_used_when_kimcli_absent_everywhere(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_BIN", None)
            with patch.object(Path, "exists", return_value=False):
                with patch("shutil.which", side_effect=lambda name: (
                    "/usr/local/bin/codex" if name == "codex" else None
                )):
                    self.assertEqual(resolve_codex_binary(), "/usr/local/bin/codex")

    def test_bare_codex_last_resort_when_nothing_found(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_BIN", None)
            with patch.object(Path, "exists", return_value=False):
                with patch("shutil.which", return_value=None):
                    self.assertEqual(resolve_codex_binary(), "codex")

    def test_real_kim_bin_dir_executable_bit_via_tempdir(self) -> None:
        """End-to-end sanity check with a real temp file (no Path mocking)."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            kim_bin_dir = fake_home / ".kim" / "bin"
            kim_bin_dir.mkdir(parents=True)
            kimcli_path = kim_bin_dir / "kimcli"
            kimcli_path.write_text("#!/bin/sh\necho fake kimcli\n")
            kimcli_path.chmod(kimcli_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CODEX_BIN", None)
                with patch.object(Path, "home", return_value=fake_home):
                    self.assertEqual(resolve_codex_binary(), str(kimcli_path))


if __name__ == "__main__":
    unittest.main()

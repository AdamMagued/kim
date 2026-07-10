"""
MCP file-sandbox security tests — audit items G1, G2, G3.

G1: secret files (`.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa*`, `id_ed25519*`,
    `credentials`, `.npmrc`, `.pypirc`) must be denied at ANY depth inside an
    allowed root — not just directly in $HOME. The exploit being closed is
    `read_file(".env")` returning project secrets to the model.
G2: the sensitive-directory deny list must cover browser-credential and
    cloud-SDK dirs when `~` is granted.
G3: `write_file`'s base64 branch must only fire for a clean whole-content
    data-URI; a text file that merely starts with `data:...;base64,` is written
    as text. An explicit `binary` flag forces the binary path.

Each test is written to FAIL against the pre-fix code and PASS after.
"""
from __future__ import annotations

import asyncio
import base64
import shutil
import tempfile
import unittest
from pathlib import Path

from mcp_server import config
from mcp_server.config import validate_path, _HOME
from mcp_server.tools.files import handle_read_file, handle_write_file


class _AllowExtraPathMixin:
    """Temporarily grant an extra allowed root (in-place mutation of the
    shared ALLOWED_PATHS list that validate_path reads)."""

    extra_paths: list[Path] = []

    def setUp(self):  # noqa: N802
        self._orig_allowed = list(config.ALLOWED_PATHS)
        config.ALLOWED_PATHS.extend(self.extra_paths)

    def tearDown(self):  # noqa: N802
        config.ALLOWED_PATHS[:] = self._orig_allowed


# ── G1 — secret-file glob deny at any depth ────────────────────────────────


class SensitiveFileGlobTests(unittest.TestCase):
    def test_project_root_env_denied(self):
        # The headline exploit: project-root .env was readable pre-fix.
        with self.assertRaises(PermissionError):
            validate_path(".env")

    def test_nested_env_local_denied(self):
        with self.assertRaises(PermissionError):
            validate_path("sub/dir/.env.local")

    def test_pem_denied(self):
        with self.assertRaises(PermissionError):
            validate_path("keys/server.pem")

    def test_private_key_denied(self):
        with self.assertRaises(PermissionError):
            validate_path("certs/tls.key")

    def test_id_rsa_denied(self):
        with self.assertRaises(PermissionError):
            validate_path("id_rsa")

    def test_id_ed25519_pub_denied(self):
        with self.assertRaises(PermissionError):
            validate_path("id_ed25519.pub")

    def test_credentials_denied(self):
        with self.assertRaises(PermissionError):
            validate_path("aws/credentials")

    def test_credential_file_variants_denied(self):
        for path in (
            "credentials.json",
            "credentials.yml",
            "client_secret_example.json",
            "service.credentials",
        ):
            with self.subTest(path=path), self.assertRaises(PermissionError):
                validate_path(path)

    def test_credentials_prefixed_variants_denied(self):
        """`credentials*` is prefix-anchored and misses these -- the common
        naming convention prefixes 'credentials' rather than starting with
        it (google_credentials.json, aws_credentials.json, a service
        account export). `*credentials*` (substring, anywhere) closes the
        gap."""
        for path in (
            "google_credentials.json",
            "aws_credentials.json",
            "service_account_credentials.json",
            "config/my_credentials.yaml",
        ):
            with self.subTest(path=path), self.assertRaises(PermissionError):
                validate_path(path)

    def test_oauth_cache_filenames_denied(self):
        """token.json / authorized_user.json are the literal filenames
        Google's OAuth client libraries cache credentials to on disk."""
        for path in ("token.json", "authorized_user.json", "creds/token.json"):
            with self.subTest(path=path), self.assertRaises(PermissionError):
                validate_path(path)

    def test_token_as_ordinary_identifier_not_overmatched(self):
        """The fix must NOT introduce a bare `*token*` substring glob --
        that would block ordinary, non-secret source files that merely
        have 'token' in their name (tokenizer/parser code, tests, etc)."""
        for path in ("tokenizer.py", "auth_token_test.py", "token_bucket.py"):
            p = validate_path(path)
            self.assertEqual(p.name, path.split("/")[-1])

    def test_npmrc_denied(self):
        with self.assertRaises(PermissionError):
            validate_path(".npmrc")

    def test_pypirc_denied(self):
        with self.assertRaises(PermissionError):
            validate_path(".pypirc")

    def test_normal_file_allowed(self):
        p = validate_path("notes.txt")
        self.assertEqual(p.name, "notes.txt")

    def test_environment_file_not_overmatched(self):
        # ".environment" is not a dotenv file — must stay allowed.
        p = validate_path("docs/.environment")
        self.assertEqual(p.name, ".environment")


class ReadEnvExploitTests(_AllowExtraPathMixin, unittest.TestCase):
    """End-to-end: the actual read_file path must refuse a real .env file."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.extra_paths = [self.tmp]
        super().setUp()
        self.env_file = self.tmp / ".env"
        self.env_file.write_text("OPENAI_API_KEY=sk-secret\n", encoding="utf-8")

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_read_env_denied(self):
        with self.assertRaises(PermissionError):
            asyncio.run(handle_read_file({"path": str(self.env_file)}))


# ── G2 — sensitive-directory deny list ─────────────────────────────────────


class SensitiveDirTests(_AllowExtraPathMixin, unittest.TestCase):
    extra_paths = [_HOME]

    def _assert_denied(self, rel_under_home: str):
        target = _HOME / rel_under_home
        with self.assertRaises(PermissionError) as cm:
            validate_path(str(target))
        self.assertIn("sensitive", str(cm.exception).lower())

    def test_gcloud_denied(self):
        self._assert_denied(".config/gcloud/credentials.db")

    def test_mozilla_denied(self):
        self._assert_denied(".mozilla/firefox/profile/key4.db")

    def test_password_store_denied(self):
        self._assert_denied(".password-store/site.gpg")

    def test_chrome_app_support_denied(self):
        self._assert_denied("Library/Application Support/Google/Chrome/Default/Cookies")

    def test_firefox_app_support_denied(self):
        self._assert_denied("Library/Application Support/Firefox/profile")

    def test_vscode_app_support_denied(self):
        self._assert_denied("Library/Application Support/Code/User/secrets")


# ── G3 — write_file base64 / binary handling ───────────────────────────────


class WriteFileBinaryTests(_AllowExtraPathMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.extra_paths = [self.tmp]
        super().setUp()

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, content, **extra):
        args = {"path": str(self.tmp / name), "content": content}
        args.update(extra)
        return asyncio.run(handle_write_file(args))

    def test_clean_data_uri_written_as_binary(self):
        raw = b"\x89PNG\r\n\x1a\n binary payload"
        content = "data:image/png;base64," + base64.b64encode(raw).decode()
        self._write("out.png", content)
        self.assertEqual((self.tmp / "out.png").read_bytes(), raw)

    def test_text_starting_with_data_uri_written_as_text(self):
        # data-URI prefix followed by real prose — NOT clean base64 → write text.
        content = (
            "data:text/plain;base64," + base64.b64encode(b"hi").decode()
            + "\n\nThese are my actual notes, not a binary blob."
        )
        self._write("note.txt", content)
        self.assertEqual((self.tmp / "note.txt").read_text(encoding="utf-8"), content)

    def test_explicit_binary_flag_decodes(self):
        raw = b"\x00\x01\x02\xff binary"
        content = "data:application/octet-stream;base64," + base64.b64encode(raw).decode()
        self._write("blob.bin", content, binary=True)
        self.assertEqual((self.tmp / "blob.bin").read_bytes(), raw)


# ── Regression: case-insensitive secret-file glob matching ────────────────────


class CaseInsensitiveSecretGlobTests(unittest.TestCase):
    """Behaviour 1 — secret-file globs must fire regardless of filename case."""

    def test_uppercase_credentials_denied(self):
        # AWS/CREDENTIALS — upper-cased name must still match the 'credentials' glob.
        with self.assertRaises(PermissionError):
            validate_path("AWS/CREDENTIALS")

    def test_uppercase_dotenv_denied(self):
        # .ENV — upper-cased name must still match the '.env' glob.
        with self.assertRaises(PermissionError):
            validate_path(".ENV")

    def test_uppercase_id_rsa_denied(self):
        # ID_RSA — upper-cased name must still match the 'id_rsa*' glob.
        with self.assertRaises(PermissionError):
            validate_path("ID_RSA")


# ── Regression: case-insensitive sensitive-directory matching ─────────────────


class CaseInsensitiveSensitiveDirTests(_AllowExtraPathMixin, unittest.TestCase):
    """Behaviour 2 — sensitive-dir deny list must block upper/mixed-case variants."""

    extra_paths = [_HOME]

    def test_uppercase_aws_dir_denied(self):
        # ~/.AWS/credentials — dir name upper-cased; must still be blocked via
        # the lower-cased prefix comparison in validate_path().
        target = _HOME / ".AWS" / "credentials"
        with self.assertRaises(PermissionError):
            validate_path(str(target))

    def test_lowercase_google_chrome_library_denied(self):
        # macOS Library path with 'google'/'chrome' lower-cased — must still
        # be blocked because the comparison is done after lowercasing both sides.
        target = _HOME / "Library" / "Application Support" / "google" / "chrome" / "Cookies"
        with self.assertRaises(PermissionError):
            validate_path(str(target))


# ── Regression: Linux and Windows browser-profile paths are denied ────────────


class LinuxWindowsBrowserPathTests(_AllowExtraPathMixin, unittest.TestCase):
    """Behaviour 3 — newly-listed Linux/Windows browser paths must be blocked."""

    extra_paths = [_HOME]

    def test_linux_google_chrome_config_denied(self):
        # Linux Chrome profile under ~/.config/google-chrome.
        target = _HOME / ".config" / "google-chrome" / "Default" / "History"
        with self.assertRaises(PermissionError):
            validate_path(str(target))

    def test_windows_chrome_appdata_roaming_denied(self):
        # Windows Chrome profile under AppData/Roaming (listed unconditionally).
        target = (
            _HOME / "AppData" / "Roaming" / "Google" / "Chrome" / "User Data" / "Default"
        )
        with self.assertRaises(PermissionError):
            validate_path(str(target))


# ── Regression: normal project files must still be allowed ────────────────────


class AllowedPathPassesTests(unittest.TestCase):
    """Behaviour 4 — deny-list additions must not over-block ordinary project files."""

    def test_normal_project_file_passes(self):
        from mcp_server.config import PROJECT_ROOT

        # A regular Python source file inside PROJECT_ROOT must pass without error.
        p = validate_path(str(PROJECT_ROOT / "mcp_server" / "config.py"))
        self.assertEqual(p.name, "config.py")

    def test_plain_text_file_passes(self):
        # A relative non-secret filename must resolve and validate cleanly.
        p = validate_path("notes.txt")
        self.assertEqual(p.name, "notes.txt")


if __name__ == "__main__":
    unittest.main()

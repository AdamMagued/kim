"""
Tests for the `view_image` MCP tool (codex-parity item 2).

view_image returns the image as a whole-content data URI — the same return
shape as take_screenshot — so the agent/provider image-upload path works
unchanged. Guards: extension allowlist, 10 MB byte cap, 25 MP pixel cap,
validate_path sandbox.
"""
from __future__ import annotations

import asyncio
import base64
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from mcp_server import config
from mcp_server.tools.files import (
    _MAX_IMAGE_BYTES,
    handle_view_image,
)


def _png_bytes(width: int = 4, height: int = 4) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (width, height), (200, 30, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _TmpDirMixin:
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self._orig_allowed = list(config.ALLOWED_PATHS)
        config.ALLOWED_PATHS.append(self.tmp)

    def tearDown(self):
        config.ALLOWED_PATHS[:] = self._orig_allowed
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _view(self, name: str) -> str:
        return asyncio.run(handle_view_image({"path": str(self.tmp / name)}))


class HappyPathTests(_TmpDirMixin, unittest.TestCase):
    def test_png_returns_data_uri_matching_screenshot_shape(self):
        raw = _png_bytes()
        (self.tmp / "img.png").write_bytes(raw)
        out = self._view("img.png")
        # Exactly the shape handle_take_screenshot returns.
        self.assertTrue(out.startswith("data:image/png;base64,"), out[:60])
        decoded = base64.b64decode(out[len("data:image/png;base64,"):])
        self.assertEqual(decoded, raw)

    def test_jpg_mime(self):
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (4, 4)).save(buf, format="JPEG")
        (self.tmp / "photo.jpg").write_bytes(buf.getvalue())
        out = self._view("photo.jpg")
        self.assertTrue(out.startswith("data:image/jpeg;base64,"))


class GuardTests(_TmpDirMixin, unittest.TestCase):
    def test_nonexistent_file(self):
        out = self._view("missing.png")
        self.assertTrue(out.startswith("ERROR:"))
        self.assertIn("not found", out.lower())

    def test_unsupported_extension(self):
        (self.tmp / "doc.txt").write_text("hello", encoding="utf-8")
        out = self._view("doc.txt")
        self.assertTrue(out.startswith("ERROR:"))
        self.assertIn("unsupported image extension", out)

    def test_oversize_bytes_guard(self):
        # Sparse-ish big file with a valid extension: byte cap fires before decode.
        big = self.tmp / "big.png"
        with open(big, "wb") as f:
            f.seek(_MAX_IMAGE_BYTES)
            f.write(b"\0")
        out = self._view("big.png")
        self.assertTrue(out.startswith("ERROR:"))
        self.assertIn("too large", out)

    def test_corrupt_image_rejected(self):
        (self.tmp / "fake.png").write_bytes(b"not actually a png")
        out = self._view("fake.png")
        self.assertTrue(out.startswith("ERROR:"))
        self.assertIn("failed to decode", out)


class SandboxTests(_TmpDirMixin, unittest.TestCase):
    def test_secret_glob_denied(self):
        # A .key file is denied by the secret-glob sandbox even with image args.
        with self.assertRaises(PermissionError):
            asyncio.run(handle_view_image({"path": str(self.tmp / "server.key")}))

    def test_out_of_allowed_path_denied(self):
        outside = Path(tempfile.mkdtemp()).resolve()
        try:
            target = outside / "img.png"
            target.write_bytes(_png_bytes())
            with self.assertRaises(PermissionError):
                asyncio.run(handle_view_image({"path": str(target)}))
        finally:
            shutil.rmtree(outside, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

"""
Regression tests for orchestrator/stuck_detection.py.

All tests are pure-Python and require no network, Tauri, or MCP server.
PIL (Pillow) must be installed; conftest.py stubs it only for heavy modules
like mss/pynput. stuck_detection.py falls back gracefully when PIL is absent,
but these tests explicitly exercise the PIL path (tuple) and the fallback
(MD5 string) path.
"""
from __future__ import annotations

import hashlib

import pytest

from orchestrator.stuck_detection import screenshot_signature, signatures_similar


@pytest.fixture(autouse=True)
def _ensure_real_pil():
    """Evict conftest's lightweight PIL stub before each test in this module.

    conftest._ensure_stubs() inserts a bare ``types.ModuleType`` for ``PIL`` /
    ``PIL.Image`` when they are not yet imported. Another test's fixture can
    install that stub earlier in a full run, so ``from PIL import Image`` here
    would resolve to the stub (no real ``Image.open``) and screenshot_signature
    would silently fall back to the MD5 path — breaking the perceptual-hash
    tests order-dependently. A real installed module has ``__file__``; the stub
    does not, so we drop the stub and let the real Pillow import (or, if Pillow
    truly is absent, importorskip below skips cleanly).
    """
    import sys
    for _m in ("PIL.Image", "PIL"):
        _mod = sys.modules.get(_m)
        if _mod is not None and not getattr(_mod, "__file__", None):
            del sys.modules[_m]
    yield


# ---------------------------------------------------------------------------
# Minimal valid 1×1 white PNG encoded as base64.
# Used wherever a real decodable image is needed.
# ---------------------------------------------------------------------------
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


# ---------------------------------------------------------------------------
# 1. screenshot_signature — valid PNG returns a 256-length tuple of ints
# ---------------------------------------------------------------------------

def test_signature_tuple_for_valid_png():
    """screenshot_signature on a valid PNG returns a 256-length tuple of ints.

    16×16 thumbnail → 256 pixels; each element is an int in [0, 15] (p // 16).
    """
    pytest.importorskip("PIL", reason="Pillow not installed — skipping perceptual-hash path")
    result = screenshot_signature(_TINY_PNG_B64)
    assert isinstance(result, tuple), f"expected tuple, got {type(result)}"
    assert len(result) == 256, f"expected 256 elements, got {len(result)}"
    assert all(isinstance(p, int) for p in result), "all elements must be int"
    assert all(0 <= p <= 15 for p in result), "luminance levels must be in [0, 15]"


# ---------------------------------------------------------------------------
# 2. screenshot_signature — garbage input falls back to hex MD5 string
# ---------------------------------------------------------------------------

def test_signature_md5_fallback_on_garbage():
    """screenshot_signature on unparseable input returns a hex MD5 string, not a tuple.

    The fallback must be: hashlib.md5(input.encode()).hexdigest()
    """
    garbage = "not-base64-image"
    result = screenshot_signature(garbage)
    assert isinstance(result, str), f"expected str fallback, got {type(result)}"
    # MD5 hex digest is always exactly 32 lowercase hex characters
    assert len(result) == 32, f"expected 32-char hex string, got len={len(result)!r}"
    assert all(c in "0123456789abcdef" for c in result), f"not a hex string: {result!r}"
    expected = hashlib.md5(garbage.encode()).hexdigest()
    assert result == expected, f"MD5 mismatch: {result!r} != {expected!r}"


# ---------------------------------------------------------------------------
# 3. signatures_similar — threshold behaviour
# ---------------------------------------------------------------------------

def test_signatures_similar_identical_is_true():
    """signatures_similar(a, a) must always return True."""
    a = tuple([7] * 256)
    assert signatures_similar(a, a) is True


def test_signatures_similar_within_max_differing_pixels_is_true():
    """Sigs differing at exactly max_differing_pixels (default 4) above pixel_diff_threshold
    (default 1) must compare as similar.

    We build a signature pair where 4 pixels differ by 2 (which is > threshold of 1,
    so they count), and the remaining 252 are identical.  4 <= 4 → True.
    """
    base = [5] * 256
    altered = list(base)
    for i in range(4):          # exactly max_differing_pixels
        altered[i] = base[i] + 2   # diff = 2 > pixel_diff_threshold(1) → counted
    a, b = tuple(base), tuple(altered)
    assert signatures_similar(a, b) is True


def test_signatures_similar_beyond_max_differing_pixels_is_false():
    """Sigs differing at more than max_differing_pixels above threshold must be dissimilar.

    5 pixels differing by 2 → 5 > 4 (max_differing_pixels default) → False.
    """
    base = [5] * 256
    altered = list(base)
    for i in range(5):          # one more than max_differing_pixels
        altered[i] = base[i] + 2
    a, b = tuple(base), tuple(altered)
    assert signatures_similar(a, b) is False


def test_signatures_similar_string_fallback_exact_match():
    """When either signature is a str (MD5 fallback), equality is used — not pixel diff."""
    md5_a = hashlib.md5(b"screen_a").hexdigest()
    md5_b = hashlib.md5(b"screen_b").hexdigest()
    assert signatures_similar(md5_a, md5_a) is True   # same string → similar
    assert signatures_similar(md5_a, md5_b) is False  # different strings → dissimilar


# ---------------------------------------------------------------------------
# 4. pixel_diff_threshold kwarg is respected
# ---------------------------------------------------------------------------

def test_pixel_diff_threshold_kwarg_respected():
    """Raising pixel_diff_threshold suppresses single-level noise.

    We create two sigs with 10 pixels that differ by exactly 1 (single-level
    noise). With the default threshold=1, abs(x-y)=1 is NOT > 1, so those
    pixels don't count and the sigs are similar.  With threshold=0, abs(x-y)=1
    IS > 0, so 10 pixels count, which exceeds max_differing_pixels(4) → False.
    """
    base = [8] * 256
    noisy = list(base)
    for i in range(10):         # 10 pixels with single-level difference
        noisy[i] = base[i] + 1  # diff = 1
    a, b = tuple(base), tuple(noisy)

    # threshold=0 → every non-zero diff counts → 10 > 4 → dissimilar
    assert signatures_similar(a, b, pixel_diff_threshold=0) is False

    # default threshold=1 → diff of 1 is NOT > 1 → 0 pixels counted → similar
    assert signatures_similar(a, b) is True

    # explicit threshold=1 same result → similar
    assert signatures_similar(a, b, pixel_diff_threshold=1) is True

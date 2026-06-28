"""
Perceptual stuck-detection helpers extracted from agent.py.

All functions here are pure (no KimAgent state) and are safe to import
and test in isolation. KimAgent delegates to these via thin wrappers that
pass the relevant instance state lists, so the public attribute interface
on KimAgent (._screenshot_hashes, ._recent_action_sigs, ._is_stuck,
._note_repeated_action) is preserved for backwards-compatible test access.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json


def screenshot_signature(screenshot_b64: str) -> tuple | str:
    """Perceptual signature: 16x16 grayscale thumbnail, 16 luminance levels.

    Falls back to an exact MD5 string when the image cannot be decoded,
    which degrades comparison to exact-match behavior.
    """
    try:
        from PIL import Image
        raw = base64.b64decode(screenshot_b64)
        img = Image.open(io.BytesIO(raw)).convert("L").resize((16, 16))
        return tuple(p // 16 for p in img.getdata())
    except Exception:
        return hashlib.md5(screenshot_b64.encode()).hexdigest()


def signatures_similar(
    a: tuple | str,
    b: tuple | str,
    *,
    pixel_diff_threshold: int = 1,
    max_differing_pixels: int = 4,
) -> bool:
    """True when two perceptual signatures differ by ≤ *max_differing_pixels* thumbnail
    pixels, counting only pixels whose absolute luminance-level difference exceeds
    *pixel_diff_threshold*.

    Defaults (tunable via kwargs):
      pixel_diff_threshold=1  — ignore single-level noise; raise to suppress more noise
      max_differing_pixels=4  — ~1.6% of a 16×16 grid; lower → more sensitive to change

    Reducing *max_differing_pixels* from the previous value of 8 cuts false-positives
    on incrementally-changing screens where only a handful of pixels shift per frame.
    """
    if isinstance(a, str) or isinstance(b, str):
        return a == b
    if len(a) != len(b):
        return False
    differing = sum(1 for x, y in zip(a, b) if abs(x - y) > pixel_diff_threshold)
    return differing <= max_differing_pixels


def is_stuck(hashes: list, screenshot_b64: str) -> bool:
    """True when the last 3 screenshots are visually unchanged.

    Mutates *hashes* in-place (same semantics as the original agent code so
    existing tests that reset _screenshot_hashes = [] continue to work).
    """
    sig = screenshot_signature(screenshot_b64)
    hashes.append(sig)
    if len(hashes) > 3:
        hashes.pop(0)
    return (
        len(hashes) == 3
        and all(signatures_similar(hashes[i], hashes[i + 1]) for i in range(2))
    )


def note_repeated_action(sigs: list, tool_name: str, tool_args: dict, result_text: str) -> bool:
    """Track identical (tool, args, result) triples; True on the 3rd consecutive repeat.

    Mutates *sigs* in-place for the same reason as is_stuck above.
    """
    try:
        args_sig = json.dumps(tool_args or {}, sort_keys=True)[:300]
    except (TypeError, ValueError):
        args_sig = str(tool_args)[:300]
    sig = f"{tool_name}|{args_sig}|{str(result_text)[:200]}"
    if sigs and sigs[-1] != sig:
        sigs.clear()
    sigs.append(sig)
    if len(sigs) >= 3:
        sigs.clear()  # fire nudge once per streak
        return True
    return False

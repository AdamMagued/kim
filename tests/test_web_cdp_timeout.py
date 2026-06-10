"""
Static contract test: _connect_over_cdp must pass an explicit timeout to
chromium.connect_over_cdp() so a hung CDP endpoint cannot stall the agent loop
for 30 s × 2 hosts × up to 12 retry iterations (≈ 12 minutes worst-case).

Why static? Playwright is not available in the test Python environment
(it lives in the project venv under Python 3.11). An AST scan is sufficient
to verify the timeout contract without importing the module.
"""

from __future__ import annotations

import ast
from pathlib import Path

_WEB_PY = Path(__file__).resolve().parent.parent / "mcp_server" / "tools" / "web.py"


def test_connect_over_cdp_passes_explicit_timeout():
    """_connect_over_cdp must pass timeout= to chromium.connect_over_cdp().

    Without an explicit timeout, Playwright uses its 30 s default.  The retry
    loops in _ensure_browser() call _connect_over_cdp up to 12 times across
    2 hosts each, making worst-case browser init ~720 s (12 min).  An explicit
    timeout caps each attempt and makes the bound visible in the source.
    """
    source = _WEB_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != "_connect_over_cdp":
            continue
        body_src = ast.get_source_segment(source, node) or ""
        assert "connect_over_cdp" in body_src, (
            "_connect_over_cdp body does not call chromium.connect_over_cdp"
        )
        # Verify the call passes a timeout= keyword
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            attr = getattr(func, "attr", "")
            if attr != "connect_over_cdp":
                continue
            kw_names = [kw.arg for kw in inner.keywords]
            assert "timeout" in kw_names, (
                "chromium.connect_over_cdp() in _connect_over_cdp is called without "
                "an explicit timeout= kwarg.  Without it, Playwright uses its 30 s "
                "default, and the _ensure_browser() retry loops multiply that to a "
                "~720 s worst-case hang.  Add timeout=_CDP_CONNECT_TIMEOUT_MS."
            )
            return

    raise AssertionError("_connect_over_cdp function not found in web.py")


def test_cdp_connect_timeout_constant_defined():
    """_CDP_CONNECT_TIMEOUT_MS must be defined and env-overridable."""
    source = _WEB_PY.read_text(encoding="utf-8")
    assert "_CDP_CONNECT_TIMEOUT_MS" in source, (
        "_CDP_CONNECT_TIMEOUT_MS constant not found in web.py — "
        "add it so operators can tune the per-attempt timeout via env var."
    )
    assert "KIM_CDP_CONNECT_TIMEOUT_MS" in source, (
        "KIM_CDP_CONNECT_TIMEOUT_MS env var not referenced in web.py — "
        "the timeout should be overridable without code changes."
    )

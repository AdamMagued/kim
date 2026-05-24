"""
Browser provider — re-export shim.

The implementation has been split into orchestrator/providers/browser/:
  - site_configs.py   — per-site CSS selectors and constants
  - prompt_builder.py — prompt formatting and data URI extraction
  - response_parser.py — DOM text -> canonical response dict
  - bridge_client.py  — in-app webview bridge HTTP client
  - provider.py       — main BrowserProvider class

This file preserves the original import path:
  from orchestrator.providers.browser_provider import BrowserProvider
"""

from orchestrator.providers.browser.site_configs import SITE_CONFIGS  # noqa: F401


def __getattr__(name):
    if name == "BrowserProvider":
        from orchestrator.providers.browser.provider import BrowserProvider
        return BrowserProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["BrowserProvider", "SITE_CONFIGS"]

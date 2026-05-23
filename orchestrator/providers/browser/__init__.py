"""Browser provider package — API-key-free LLM access via Playwright CDP.

BrowserProvider is imported lazily to avoid requiring playwright at module load.
Use: from orchestrator.providers.browser.provider import BrowserProvider
Or:  from orchestrator.providers.browser_provider import BrowserProvider
"""


def __getattr__(name):
    if name == "BrowserProvider":
        from orchestrator.providers.browser.provider import BrowserProvider
        return BrowserProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

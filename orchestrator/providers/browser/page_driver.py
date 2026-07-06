"""
PageDriver — the injectable browser-I/O seam for BrowserProvider (K3).

``BrowserProvider.complete()`` normally connects to a live Chrome over CDP via
Playwright and drives a real ``playwright.async_api.Page``.  Every downstream
step (popup dismissal, prompt injection + verification, the two-phase
response wait, the markdown scrape) only touches the small structural surface
documented here — so a test (or an alternative transport) can hand
``complete()`` a *driver* that yields a page-shaped object and the whole
pipeline runs without Playwright, a browser, or the network.

Usage:
    provider = BrowserProvider(config, page_driver=my_driver)
    # or per call (call-site override wins):
    await provider.complete(messages, tools, page_driver=my_driver)

A driver exposes one method::

    async def acquire(self) -> tuple[PageLike | None, str | None]

returning the page-like object plus the site key ("chatgpt", "gemini", …)
used to look up selectors in ``SITE_CONFIGS``.  Returning ``(None, None)``
maps to the same NEED_HELP result as a lost browser tab.

A real Playwright ``Page`` satisfies ``PageLike`` structurally — the
protocols below document exactly the attribute/method surface the provider
uses, nothing more.  Keep them in sync with ``provider.py`` if the provider
grows new page interactions.
"""
from __future__ import annotations

from typing import Any, Optional, Protocol, Sequence, runtime_checkable


class ElementLike(Protocol):
    """A response element yielded by ``LocatorLike.all()``.

    ``evaluate`` receives the markdown-serializer JS
    (``MARKDOWN_SERIALIZER_JS``); fakes typically mirror that algorithm over
    a parsed DOM instead of executing JavaScript.
    """

    async def evaluate(self, expression: str) -> Any: ...

    async def inner_text(self) -> str: ...

    async def text_content(self) -> Optional[str]: ...


class LocatorLike(Protocol):
    """Result of ``PageLike.locator(selector)``.

    ``nth``/``first`` return handles that must support ``is_visible`` /
    ``inner_text`` / ``click`` / ``wait_for`` (Playwright returns Locators;
    fakes may return themselves or element objects).
    """

    async def count(self) -> int: ...

    def nth(self, index: int) -> Any: ...

    @property
    def first(self) -> Any: ...

    async def all(self) -> Sequence[ElementLike]: ...


class KeyboardLike(Protocol):
    async def press(self, key: str) -> None: ...


class PageLike(Protocol):
    """The exact page surface ``BrowserProvider`` drives after tab selection.

    Attributes:
        url:       current page URL (plain attribute/property).
        keyboard:  key-press interface (Escape sweeps, Mod+A/V, Enter).
    Methods:
        goto:              only used by ``clear_chat`` (reload).
        locator:           selector lookup for inputs/buttons/responses.
        evaluate:          JS evaluation — clipboard writes, editor readback
                           (``el.value ?? el.innerText ?? el.textContent``),
                           and ``document.hasFocus()`` during tab scans.
        click:             focus the editor before injection.
        wait_for_timeout:  settle pause after image upload (ms).
    """

    url: str
    keyboard: KeyboardLike

    async def goto(self, url: str, **kwargs: Any) -> Any: ...

    def locator(self, selector: str) -> LocatorLike: ...

    async def evaluate(self, expression: str, arg: Any = None) -> Any: ...

    async def click(self, selector: str, **kwargs: Any) -> None: ...

    async def wait_for_timeout(self, timeout: float) -> None: ...


@runtime_checkable
class PageDriver(Protocol):
    """Injectable source of a ready-to-drive chat page.

    ``acquire()`` replaces the whole connect/find-tab phase of
    ``BrowserProvider.complete()``: it returns the page-like object and the
    site key whose ``SITE_CONFIGS`` entry describes its selectors.
    ``(None, None)`` signals "no usable chat page" (NEED_HELP).
    """

    async def acquire(self) -> tuple[Optional[PageLike], Optional[str]]: ...

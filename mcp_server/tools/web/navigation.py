"""Navigation tools for Kim's controlled browser.

``web_open`` (with its SSRF / URL-scheme guards and origin-scoped HTTP
Basic-auth), ``web_back``, ``web_close`` and the two wait tools.
"""

from __future__ import annotations

import base64
import logging
import re
import urllib.parse
from typing import Any

from mcp_server.config import USE_REAL_BROWSER

from . import browser
# _is_ssrf_target (+ its WHATWG-IP-literal helpers) now lives in browser.py
# so the page-level navigation-event guard installed by _ensure_browser can
# call it without a circular import (#1: SSRF validation used to be a single
# call site here; it is now ALSO enforced structurally on every navigation —
# see browser._install_ssrf_guard). Re-exported here for backward
# compatibility: this module's own pre-goto check below, web/__init__.py's
# facade, and the test suite all reach for `navigation._is_ssrf_target`.
from .browser import _is_ssrf_target, _parse_host_as_ip, _whatwg_ipv4_part  # noqa: F401

logger = logging.getLogger(__name__)


def _default_port(scheme: str) -> int | None:
    return {"http": 80, "https": 443}.get((scheme or "").lower())


async def handle_web_open(args: dict) -> str:
    url = str(args.get("url", "")).strip()
    username = str(args.get("username", "")).strip()
    password = str(args.get("password", "")).strip()

    if not url:
        return "ERROR: url is required"
    # Security: the controlled browser must NOT read local files or privileged
    # pages. file://, chrome://, about:, view-source:, filesystem: would bypass the
    # path sandbox entirely — e.g. open_url("file:///Users/<u>/.ssh/id_rsa") followed
    # by web_text() exfiltrates any file with no approval gate. Only http(s) (and
    # inline data:) are permitted.
    if url.lower().startswith(("file:", "chrome:", "about:", "view-source:", "filesystem:")):
        return (
            "ERROR: refusing to open a local or privileged URL scheme. "
            "Only http(s) URLs are allowed."
        )
    if not url.startswith(("http://", "https://", "data:")):
        url = "https://" + url

    # Block SSRF: refuse requests to loopback/private/link-local IP ranges (#51).
    # This is a fast, friendly rejection for the common case (bad URL typed or
    # passed directly to web_open) so the caller gets a clear error without a
    # wasted navigation attempt. It is deliberately NOT the only guard: the
    # page-level route installed by browser._install_ssrf_guard (#1) is what
    # actually enforces this on every navigation — including redirects away
    # from a URL that passed this check, and link/button-driven navigation
    # from web_click, neither of which go through handle_web_open at all.
    if _is_ssrf_target(url):
        return (
            "ERROR: refusing to open an internal/loopback address. "
            "Only public internet URLs are allowed."
        )

    page = await browser._page()

    # Track any URL-scoped route we add so we can remove it after navigation.
    _auth_route_matcher: Any = None
    _auth_route_handler: Any = None
    _auth_unroute_failed = False

    if username and password:
        # BrowserContext.set_http_credentials() does not exist in Playwright (#50).
        # The only supported way to scope HTTP Basic-auth credentials without
        # affecting every other request on the shared context is to install a
        # temporary page.route() handler that injects the Authorization header,
        # then unroute it after navigation completes.
        #
        # 3.2: the route is scoped to the EXACT requested URL (scheme + host +
        # port + path + query), not "{origin}/**" — an origin glob kept
        # replaying the Authorization header onto every same-origin request,
        # including redirects to other paths, for the whole navigation.
        target = urllib.parse.urlparse(url)
        target_path = target.path or "/"
        creds_b64 = base64.b64encode(f"{username}:{password}".encode()).decode()
        auth_header_value = f"Basic {creds_b64}"

        def _matches_target(request_url: str) -> bool:
            try:
                req = urllib.parse.urlparse(str(request_url))
            except Exception:
                return False
            return (
                req.scheme == target.scheme
                and req.hostname == target.hostname
                and (req.port or _default_port(req.scheme)) == (target.port or _default_port(target.scheme))
                and (req.path or "/") == target_path
                and req.query == target.query
            )

        async def _do_auth_route(route, request):
            await route.continue_(headers={**request.headers, "Authorization": auth_header_value})

        _auth_route_handler = _do_auth_route
        _auth_route_matcher = _matches_target
        await page.route(_auth_route_matcher, _auth_route_handler)
    # (No else branch: the old `set_extra_http_headers({})` call was leftover
    # from the removed set_http_credentials approach (#50) — it cleared headers
    # that were never set. L7.)

    _goto_error: Exception | None = None
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
    except Exception as e:
        _goto_error = e
    finally:
        # Always unroute the auth handler so it doesn't leak into future
        # navigations. 3.2: a swallowed unroute failure used to let the
        # credential-injecting handler silently persist on the reused page —
        # now the failure is logged and surfaced to the caller.
        if _auth_route_matcher and _auth_route_handler:
            try:
                await page.unroute(_auth_route_matcher, _auth_route_handler)
            except Exception as unroute_err:
                logger.error(
                    "web_open: failed to remove HTTP-auth route handler — "
                    f"credentials may persist on this page: {unroute_err}"
                )
                _auth_unroute_failed = True
                try:
                    # unroute_all() removes EVERY page.route() handler, not
                    # just the stuck auth one — including the persistent SSRF
                    # navigation guard installed once per page in
                    # _ensure_browser (#1). Without reinstating it here, this
                    # recovery path would silently strip SSRF protection for
                    # the rest of this page's lifetime (_ensure_browser's
                    # fast liveness-check path never re-installs it for an
                    # already-live page), reopening exactly the redirect/
                    # click-driven-navigation hole #1 exists to close.
                    await page.unroute_all()
                    await browser._install_ssrf_guard(page)
                    _auth_unroute_failed = False
                    logger.info(
                        "web_open: unroute_all() cleared the stuck auth route "
                        "(SSRF guard reinstalled)"
                    )
                except Exception:
                    pass

    unroute_warn = (
        "\nWARNING: the temporary HTTP-auth handler could not be removed; "
        "credentials may still be injected on this page until it is closed."
        if _auth_unroute_failed
        else ""
    )

    if _goto_error is not None:
        err_text = str(_goto_error)
        if (
            "ERR_INVALID_AUTH_CREDENTIALS" in err_text
            or "ERR_HTTP_RESPONSE_CODE_FAILURE" in err_text
            or "401" in err_text
        ):
            mode_str = " (Real Browser)" if browser._is_real_browser else " (Dedicated Kim Browser)"
            if username or password:
                return (
                    f"AUTH_FAILED: {url}{mode_str}\n"
                    "The site rejected the supplied HTTP authentication credentials. "
                    "Do not call web_observe/web_text/web_screenshot for page content yet; "
                    "ask the user to verify the username/password or sign in manually."
                    + unroute_warn
                )
            return (
                f"AUTH_REQUIRED: {url}{mode_str}\n"
                "The site is open but blocked by an HTTP authentication popup. "
                "Call web_open again with username and password if you have them; "
                "otherwise ask the user to sign in manually."
                + unroute_warn
            )
        return f"ERROR: navigation failed: {_goto_error}{unroute_warn}"

    if page.url.startswith("chrome-error://"):
        mode_str = " (Real Browser)" if browser._is_real_browser else " (Dedicated Kim Browser)"
        return (
            f"ERROR: browser error page after opening {url}{mode_str}\n"
            "The controlled browser did not reach usable page content. "
            "Do not treat this as open; ask for help or retry with valid credentials."
            + unroute_warn
        )
    try:
        title = await page.title()
    except Exception:
        title = ""

    if not browser._is_real_browser and USE_REAL_BROWSER:
        err_hint = f" ({browser._last_connection_error})" if browser._last_connection_error else ""
        if "target closed" in err_hint.lower() or "connection closed" in err_hint.lower():
            err_hint += " — This usually means Chrome is already open with your profile. You must QUIT Chrome completely before Kim can control it."
        mode_str = f" (Fallback: Dedicated Kim Browser — Could not connect to your real browser{err_hint})"
    elif browser._is_real_browser:
        mode_str = " (Real Browser)"
    else:
        mode_str = " (Dedicated Kim Browser)"

    return f"Opened: {page.url}{mode_str}\nTitle: {title}{unroute_warn}"


async def handle_web_wait_for(args: dict) -> str:
    text = str(args.get("text") or "").strip()
    selector = str(args.get("selector") or "").strip()
    target = text or selector
    timeout = int(args.get("timeout_ms", 10000))
    if not target:
        return "ERROR: 'text' or 'selector' is required"
    page = await browser._page()
    try:
        # A leading "/" only means XPath when the caller explicitly used the
        # `selector` argument — plain page text like "/pricing" must be
        # treated as text, not a selector (L6).
        if selector or target.startswith(("css=", "xpath=")):
            await page.locator(target).first.wait_for(timeout=timeout, state="visible")
            return f"Selector matched: {target}"
        await page.get_by_text(target, exact=False).first.wait_for(
            timeout=timeout, state="visible"
        )
        return f"Text appeared: {target!r}"
    except Exception as e:
        return f"Timeout waiting for {target!r}: {e}"


async def handle_web_wait_for_url(args: dict) -> str:
    contains = str(args.get("url_contains") or "").strip()
    regex = str(args.get("url_regex") or "").strip()
    timeout = int(args.get("timeout_ms", 10000))
    if not contains and not regex:
        return "ERROR: 'url_contains' or 'url_regex' is required"
    page = await browser._page()
    try:
        if regex:
            pattern = re.compile(regex)
            await page.wait_for_url(lambda url: bool(pattern.search(str(url))), timeout=timeout)
            return f"URL matched regex: {page.url}"
        await page.wait_for_url(lambda url: contains in str(url), timeout=timeout)
        return f"URL contains {contains!r}: {page.url}"
    except Exception as e:
        current = getattr(page, "url", "")
        target = regex or contains
        return f"Timeout waiting for URL {target!r}; current URL is {current!r}: {e}"


async def handle_web_back(args: dict) -> str:
    page = await browser._page()
    try:
        await page.go_back(wait_until="domcontentloaded", timeout=10000)
    except Exception as e:
        return f"ERROR: back failed: {e}"
    return f"Navigated back to {page.url}"


async def handle_web_close(args: dict) -> str:
    """
    Prevent the LLM from closing the browser automatically.
    The browser should stay open so the user remains signed in.
    """
    return "The Kim browser remains open so that your session and logins are preserved for the next task."

"""Navigation tools for Kim's controlled browser.

``web_open`` (with its SSRF / URL-scheme guards and origin-scoped HTTP
Basic-auth), ``web_back``, ``web_close`` and the two wait tools.
"""

from __future__ import annotations

import base64
import ipaddress
import re
import urllib.parse
from typing import Any

from mcp_server.config import USE_REAL_BROWSER

from . import browser

def _whatwg_ipv4_part(tok: str) -> int:
    """Parse one IPv4 host part the way the WHATWG URL parser does.

    Base is selected by prefix: ``0x``/``0X`` -> hex, a leading ``0`` (len>1)
    -> octal, otherwise decimal. (Plain ``int(tok, 0)`` rejects leading-zero
    octal like ``0177`` — the gap that let ``0177.0.0.1`` slip past the old
    SSRF check — so the base is chosen explicitly here.)

    Raises ValueError if the token is not a valid numeric part.
    """
    if not tok:
        raise ValueError("empty IPv4 part")
    if tok[:2] in ("0x", "0X"):
        return int(tok, 16)
    if tok[0] == "0" and len(tok) > 1:
        return int(tok, 8)
    return int(tok, 10)


def _parse_host_as_ip(host: str):
    """Try to parse *host* as a numeric IP address in any encoding browsers accept.

    Mirrors the WHATWG URL IPv4 parser so the SSRF check sees the SAME address
    Chromium will actually dial. Covers:
    - Standard dotted-decimal IPv4 / IPv6 literals ('127.0.0.1', '::1').
    - Bare integer in decimal/hex/octal ('2130706433', '0x7f000001', '017700000001').
    - Dotted notation with non-decimal octets ('0177.0.0.1', '0x7f.0.0.1').
    - Short dotted forms where the final part absorbs the remaining bytes
      ('127.1' -> 127.0.0.1, '127.0.1' -> 127.0.0.1, '10.1' -> 10.0.0.1).

    Returns an ipaddress.IPv4Address or IPv6Address on success.
    Raises ValueError if the host cannot be interpreted as any numeric IP literal
    (i.e. it is a DNS domain name).
    """
    # Fast path: standard dotted-decimal IPv4 or IPv6 literal.
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass

    # WHATWG IPv4: 1-4 dot-separated parts, each in any base; a single trailing
    # empty part (host ending in '.') is tolerated. The last part absorbs all
    # remaining low-order bytes, so fewer than 4 parts is valid.
    parts = host.split(".")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    if not (1 <= len(parts) <= 4):
        raise ValueError(f"Not a numeric IP literal: {host!r}")
    try:
        nums = [_whatwg_ipv4_part(p) for p in parts]
    except ValueError:
        raise ValueError(f"Not a numeric IP literal: {host!r}")

    n = len(nums)
    # Every part except the last is a single octet (<256); the last part holds
    # the remaining (4 - (n-1)) bytes.
    if any(x < 0 or x > 255 for x in nums[:-1]):
        raise ValueError(f"Not a numeric IP literal: {host!r}")
    last_max = 256 ** (4 - (n - 1))
    if not (0 <= nums[-1] < last_max):
        raise ValueError(f"Not a numeric IP literal: {host!r}")

    value = nums[-1]
    for i, octet in enumerate(nums[:-1]):
        value += octet << (8 * (3 - i))
    if not (0 <= value <= 0xFFFFFFFF):
        raise ValueError(f"Not a numeric IP literal: {host!r}")
    return ipaddress.ip_address(value)


def _is_ssrf_target(url: str) -> bool:
    """Return True if the URL resolves to a loopback/private/link-local address (#51).

    Closes the bypass where integer/hex/octal IP encodings (e.g. 2130706433,
    0x7f000001, 0177.0.0.1) were passed through because ipaddress.ip_address()
    raised ValueError on them, causing the except branch to classify them as
    safe domain names.  _parse_host_as_ip() normalises all browser-accepted
    numeric forms before the loopback/private checks run.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ""
        # Strip IPv6 brackets that urlparse leaves on literal addresses.
        host = host.strip("[]")
        # `localhost` (and RFC 6761 `*.localhost`) always resolves to loopback
        # but is not a numeric IP, so it fell through the ValueError→allow
        # branch below — the most obvious spelling of the exact class the
        # numeric-loopback block (#51) exists to stop (H2).
        lowered = host.lower().rstrip(".")
        if lowered == "localhost" or lowered.endswith(".localhost"):
            return True
        try:
            addr = _parse_host_as_ip(host)
        except ValueError:
            # Host is a DNS domain name — allow it (DNS-rebind is out of scope here).
            return False
        return (
            addr.is_loopback
            or addr.is_private
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
        )
    except Exception:
        # Malformed URL: default-deny to be safe.
        return True


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
    if _is_ssrf_target(url):
        return (
            "ERROR: refusing to open an internal/loopback address. "
            "Only public internet URLs are allowed."
        )

    page = await browser._page()

    # Track any origin-scoped route we add so we can remove it after navigation.
    _auth_route_pattern: str | None = None
    _auth_route_handler: Any = None

    if username and password:
        # BrowserContext.set_http_credentials() does not exist in Playwright (#50).
        # The only supported way to scope HTTP Basic-auth credentials to a specific
        # origin without affecting every other request on the shared context is to
        # install a temporary page.route() handler that injects the Authorization
        # header for matching URLs, then unroute it after navigation completes.
        parsed_origin = urllib.parse.urlparse(url)
        origin_prefix = f"{parsed_origin.scheme}://{parsed_origin.netloc}"
        creds_b64 = base64.b64encode(f"{username}:{password}".encode()).decode()
        auth_header_value = f"Basic {creds_b64}"

        async def _do_auth_route(route, request):
            await route.continue_(headers={**request.headers, "Authorization": auth_header_value})

        _auth_route_handler = _do_auth_route
        _auth_route_pattern = f"{origin_prefix}/**"
        await page.route(_auth_route_pattern, _auth_route_handler)
    # (No else branch: the old `set_extra_http_headers({})` call was leftover
    # from the removed set_http_credentials approach (#50) — it cleared headers
    # that were never set. L7.)

    _goto_error: Exception | None = None
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
    except Exception as e:
        _goto_error = e
    finally:
        # Always unroute the auth handler so it doesn't leak into future navigations.
        if _auth_route_pattern and _auth_route_handler:
            try:
                await page.unroute(_auth_route_pattern, _auth_route_handler)
            except Exception:
                pass

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
                )
            return (
                f"AUTH_REQUIRED: {url}{mode_str}\n"
                "The site is open but blocked by an HTTP authentication popup. "
                "Call web_open again with username and password if you have them; "
                "otherwise ask the user to sign in manually."
            )
        return f"ERROR: navigation failed: {_goto_error}"

    if page.url.startswith("chrome-error://"):
        mode_str = " (Real Browser)" if browser._is_real_browser else " (Dedicated Kim Browser)"
        return (
            f"ERROR: browser error page after opening {url}{mode_str}\n"
            "The controlled browser did not reach usable page content. "
            "Do not treat this as open; ask for help or retry with valid credentials."
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

    return f"Opened: {page.url}{mode_str}\nTitle: {title}"


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

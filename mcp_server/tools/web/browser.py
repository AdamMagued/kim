"""Browser session lifecycle for Kim's controlled web browser.

Owns the Playwright / Chromium singletons (the MCP server is a single
subprocess, so they survive across tool calls within a session) and the
connect / launch / reconnect ladder:

  1. USE_REAL_BROWSER=1: CDP-connect to (or launch) the user's real Chrome.
  2. CDP-connect to (or launch) Kim's own detached dedicated browser.
  3. Last resort: a Playwright-owned persistent context.

Other web submodules must access the mutable globals through this module
(``browser._active_page``), never ``from``-import them — they are rebound.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import shutil
import socket
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any

from mcp_server.config import PROJECT_ROOT, USE_REAL_BROWSER
from mcp_server.os_utils import IS_MACOS, IS_WINDOWS, IS_LINUX, minimal_subprocess_env

logger = logging.getLogger(__name__)


# ── SSRF target detection (#51) ──────────────────────────────────────────────
#
# Lives here (not navigation.py) so _install_ssrf_guard below — and any other
# page-lifecycle code — can call it without a circular import (navigation.py
# already depends on this module for browser._page()). navigation.py
# re-exports these names for backward compatibility (web_open's own
# pre-goto check, __init__.py's facade, and the test suite).

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


async def _install_ssrf_guard(page: Any) -> None:
    """Validate EVERY top-level navigation on *page*, not just the one
    ``web_open`` makes before its own ``page.goto()`` (HIGH audit finding #1).

    The old guard was a single call site: it checked the URL once, before the
    initial navigation, and was never re-checked on redirects (a public URL
    that 302s to ``http://169.254.169.254/`` sailed through) nor on
    click-driven navigation (``web_click`` never called it at all). That is
    architectural, not a missing call — so the fix hooks Playwright's request
    interception at the page level instead of sprinkling more one-time checks
    at each call site: a route handler installed once per page inspects every
    outgoing *navigation* request (``request.is_navigation_request()``) on the
    main frame and aborts it before the browser ever dials the target if
    ``_is_ssrf_target`` flags it. This covers goto(), redirect hops (each hop
    is its own navigation request through the same route), and link/button
    driven navigation from ``web_click`` — all for free, with no per-call-site
    plumbing.
    """

    async def _guard(route, request) -> None:
        try:
            is_nav = request.is_navigation_request()
        except Exception:
            is_nav = False
        if is_nav:
            try:
                is_main_frame = request.frame == page.main_frame
            except Exception:
                is_main_frame = True
            if is_main_frame:
                try:
                    blocked = _is_ssrf_target(request.url)
                except Exception:
                    blocked = True  # malformed URL: default-deny, matches _is_ssrf_target
                if blocked:
                    logger.warning(
                        "web: blocked navigation to internal/loopback address "
                        f"{request.url!r} (SSRF guard, #1)"
                    )
                    with contextlib.suppress(Exception):
                        await route.abort("blockedbyclient")
                    return
        with contextlib.suppress(Exception):
            await route.continue_()

    try:
        await page.route("**/*", _guard)
    except Exception as e:
        logger.warning(f"web: failed to install SSRF navigation guard: {e}")

# Module-level singletons. The MCP server is a single subprocess, so these
# survive across tool calls within a session.
_lock = asyncio.Lock()
_playwright: Any = None
_browser_ctx: Any = None
_active_page: Any = None
_is_real_browser: bool = False
_last_connection_error: str = ""
_DEDICATED_CDP_PORT = int(os.environ.get("KIM_DEDICATED_BROWSER_CDP_PORT", "9333"))
# Port for the user's *real* Chrome/Chromium when USE_REAL_BROWSER=1 (#3).
# The Chrome default is 9222, but users may already have a process on that port.
_REAL_BROWSER_CDP_PORT = int(os.environ.get("KIM_REAL_BROWSER_CDP_PORT", "9222"))

def _resolve_user_data_dir() -> Path:
    """Pick a writable directory for the persistent Chromium profile.

    config.yaml's ``project_root`` is sometimes a stale path baked in from a
    different machine, so we try it first but fall back to a directory next
    to this file (which always exists) and finally ``~/.kim``.
    """
    candidates = [
        PROJECT_ROOT / "sessions" / "kim_browser",
        # parents[3]: …/mcp_server/tools/web/browser.py -> repo root
        Path(__file__).resolve().parents[3] / "sessions" / "kim_browser",
        Path.home() / ".kim" / "sessions" / "kim_browser",
    ]
    for c in candidates:
        try:
            c.mkdir(parents=True, exist_ok=True)
            return c
        except OSError:
            continue
    raise RuntimeError("Could not locate a writable directory for kim_browser profile")


USER_DATA_DIR = _resolve_user_data_dir()

# 3.3: single-flight launch state. Chrome launches were fire-and-forget
# (Popen with no handle), so on repeated CDP-connect failures the retry
# ladder spawned MULTIPLE Chrome processes on the SAME --user-data-dir —
# profile-lock ("SingletonLock") failures and corruption. Track the launch
# so a second attempt while one is still coming up becomes a no-op.
_LAUNCH_COOLDOWN_S = float(os.environ.get("KIM_BROWSER_LAUNCH_COOLDOWN_S", "30"))
_dedicated_launch_proc: subprocess.Popen | None = None
_last_launch_at: dict[int, float] = {}  # port -> monotonic timestamp


def _port_listening(port: int, timeout_s: float = 0.5) -> bool:
    """True if something already accepts TCP connections on the CDP port."""
    for host in ("127.0.0.1", "localhost"):
        try:
            with socket.create_connection((host, port), timeout=timeout_s):
                return True
        except OSError:
            continue
    return False


def _launch_allowed(port: int) -> bool:
    """Single-flight gate: refuse a new launch if one is plausibly in flight.

    A launch is "in flight" when the port already answers, when the tracked
    dedicated process is still alive, or when a launch on this port happened
    within the cooldown window (covers the macOS ``open -a`` path, whose
    Popen handle exits immediately and cannot be tracked).
    """
    if _port_listening(port):
        return False
    if (
        port == _DEDICATED_CDP_PORT
        and _dedicated_launch_proc is not None
        and _dedicated_launch_proc.poll() is None
    ):
        return False
    last = _last_launch_at.get(port)
    if last is not None and (time.monotonic() - last) < _LAUNCH_COOLDOWN_S:
        return False
    return True


async def _launch_real_browser() -> bool:
    """Attempt to launch the user's real browser with remote debugging enabled."""
    cdp_arg = f"--remote-debugging-port={_REAL_BROWSER_CDP_PORT}"

    if not _launch_allowed(_REAL_BROWSER_CDP_PORT):
        logger.info(
            "web: real-browser launch skipped — one is already running or "
            f"was launched <{_LAUNCH_COOLDOWN_S:.0f}s ago (single-flight, 3.3)"
        )
        return True
    _last_launch_at[_REAL_BROWSER_CDP_PORT] = time.monotonic()

    try:
        _env = minimal_subprocess_env()  # S4: no parent-env (secrets) inherit
        if IS_MACOS:
            # 'open' is cleaner than launching Chrome directly when an instance
            # is already running with the debugging flag.
            subprocess.Popen(["open", "-a", "Google Chrome", "--args", cdp_arg], env=_env)
            return True
        elif IS_WINDOWS:
            for p in [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ]:
                if Path(p).exists():
                    subprocess.Popen([p, cdp_arg], env=_env)
                    return True
        elif IS_LINUX:
            for bin_name in ["google-chrome", "chromium-browser", "chromium"]:
                if shutil.which(bin_name):
                    subprocess.Popen([bin_name, cdp_arg], env=_env)
                    return True
    except Exception as e:
        logger.warning(f"web: failed to launch real browser: {e}")

    return False


def _browser_executable() -> str | None:
    env_path = os.environ.get("KIM_BROWSER_EXECUTABLE", "").strip()
    if env_path and Path(env_path).exists():
        return env_path
    if IS_MACOS:
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if Path(chrome).exists():
            return chrome
        chromium = "/Applications/Chromium.app/Contents/MacOS/Chromium"
        if Path(chromium).exists():
            return chromium
    elif IS_WINDOWS:
        for path in [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]:
            if Path(path).exists():
                return path
    else:
        for bin_name in ["google-chrome", "chromium-browser", "chromium"]:
            found = shutil.which(bin_name)
            if found:
                return found
    return None


_CDP_CONNECT_TIMEOUT_MS = int(os.environ.get("KIM_CDP_CONNECT_TIMEOUT_MS", "10000"))


async def _connect_over_cdp(port: int) -> Any | None:
    global _last_connection_error
    for host in ["127.0.0.1", "localhost"]:
        try:
            return await _playwright.chromium.connect_over_cdp(
                f"http://{host}:{port}",
                timeout=_CDP_CONNECT_TIMEOUT_MS,
            )
        except Exception as e:
            _last_connection_error = str(e)
    return None


async def _launch_dedicated_browser() -> bool:
    """Launch Kim's own detached browser process so it survives MCP exit."""
    global _dedicated_launch_proc, _last_connection_error
    executable = _browser_executable()
    if not executable:
        _last_connection_error = "No Chrome/Chromium executable found"
        return False

    if not _launch_allowed(_DEDICATED_CDP_PORT):
        logger.info(
            "web: dedicated-browser launch skipped — one is already running or "
            f"was launched <{_LAUNCH_COOLDOWN_S:.0f}s ago (single-flight, 3.3)"
        )
        return True
    _last_launch_at[_DEDICATED_CDP_PORT] = time.monotonic()

    args = [
        executable,
        f"--remote-debugging-port={_DEDICATED_CDP_PORT}",
        f"--user-data-dir={USER_DATA_DIR}",
        "--no-default-browser-check",
        "--no-first-run",
        "--disable-blink-features=AutomationControlled",
        "about:blank",
    ]
    try:
        # 3.3: keep the handle so a later launch attempt can see this one is
        # still alive (poll() also reaps it if it exited, avoiding zombies).
        _dedicated_launch_proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True, env=minimal_subprocess_env(),  # S4: minimal env
        )
        return True
    except Exception as e:
        logger.warning(f"web: failed to launch dedicated browser: {e}")
        return False


async def _active_browser_context() -> Any:
    if hasattr(_browser_ctx, "pages"):
        return _browser_ctx
    contexts = getattr(_browser_ctx, "contexts", [])
    if contexts:
        return contexts[0]
    return await _browser_ctx.new_context()


async def _connect_browser_ctx() -> None:
    """Populate ``_browser_ctx`` via CDP connect / launch / persistent-context fallback."""
    global _browser_ctx, _is_real_browser, _last_connection_error
    _last_connection_error = ""

    if USE_REAL_BROWSER:
        # 1. Try to connect to an existing browser on the configured port (#3).
        _browser_ctx = await _connect_over_cdp(_REAL_BROWSER_CDP_PORT)
        if _browser_ctx is not None:
            _is_real_browser = True
            logger.info(f"web: connected to existing browser via CDP on port {_REAL_BROWSER_CDP_PORT}")

        if _browser_ctx is None:
            # 2. If connection fails, try to launch the real browser with CDP flag
            logger.info(f"web: no browser on {_REAL_BROWSER_CDP_PORT} ({_last_connection_error}), attempting launch...")
            if await _launch_real_browser():
                # Retry connection a few times
                for attempt in range(1, 6):
                    logger.info(f"web: connection attempt {attempt}/5...")
                    await asyncio.sleep(2)
                    _browser_ctx = await _connect_over_cdp(_REAL_BROWSER_CDP_PORT)
                    if _browser_ctx is not None:
                        _is_real_browser = True
                        logger.info("web: connected to real browser via CDP after launch")
                        break

    # 3. Fallback to Kim's own persistent context if CDP failed or was disabled
    if _browser_ctx is None:
        _is_real_browser = False
        _browser_ctx = await _connect_over_cdp(_DEDICATED_CDP_PORT)
        if _browser_ctx is None:
            logger.info(
                f"web: launching detached dedicated Kim browser profile "
                f"(Reason: {_last_connection_error})"
            )
            if await _launch_dedicated_browser():
                for attempt in range(1, 8):
                    logger.info(f"web: dedicated browser connection attempt {attempt}/7...")
                    await asyncio.sleep(1)
                    _browser_ctx = await _connect_over_cdp(_DEDICATED_CDP_PORT)
                    if _browser_ctx is not None:
                        break

        if _browser_ctx is None:
            logger.warning(
                "web: detached dedicated browser failed; falling back to "
                f"Playwright-owned persistent context ({_last_connection_error})"
            )
            _browser_ctx = await _playwright.chromium.launch_persistent_context(
                user_data_dir=str(USER_DATA_DIR),
                headless=False,
                viewport={"width": 1280, "height": 820},
                args=[
                    "--no-default-browser-check",
                    "--no-first-run",
                    "--disable-blink-features=AutomationControlled",
                ],
            )


async def _ensure_browser() -> None:
    """Lazily start Playwright + Chromium, reuse across tool calls."""
    global _playwright, _browser_ctx, _active_page, _is_real_browser

    if _active_page is not None:
        try:
            await _active_page.evaluate("1")
            return
        except Exception:
            logger.info("web: active page died, re-creating")
            _active_page = None

    from playwright.async_api import async_playwright

    if _playwright is None:
        _playwright = await async_playwright().start()

    for attempt in (1, 2):
        if _browser_ctx is None:
            await _connect_browser_ctx()
        try:
            context = await _active_browser_context()
            pages = context.pages
            page = pages[-1] if pages else await context.new_page()
            # Liveness probe: a stale context happily hands back dead pages.
            await page.evaluate("1")
            # SSRF guard (#1): installed once per adopted page (not on the
            # fast liveness-check path above, which reuses an already-guarded
            # _active_page), so it covers redirects and click-driven
            # navigation on this page for its whole lifetime.
            await _install_ssrf_guard(page)
            _active_page = page
            return
        except Exception:
            # The browser quit/crashed but the stale context object is not
            # None, so the reconnect path above never ran — every web tool
            # would raise TargetClosedError forever. Reset the context so
            # the relaunch/reconnect path actually runs (H1).
            logger.info(
                "web: browser context appears dead, resetting for reconnect "
                f"(attempt {attempt}/2)"
            )
            if _browser_ctx is not None:
                try:
                    await _browser_ctx.close()
                except Exception:
                    pass
            _browser_ctx = None
            _active_page = None
            if attempt == 2:
                raise


# _on_context_closed was defined here but never wired to a context event, so it
# was dead code that gave a false sense of cleanup (#52).  Removed.


async def _page():
    async with _lock:
        await _ensure_browser()
    return _active_page

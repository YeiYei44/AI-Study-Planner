"""Canvas browser session lifecycle.

Entry points, deliberately separate:

* ``CanvasSession`` — headless, for syncing. Checks auth on entry and
  raises ``SessionExpiredError`` if the stored session is dead. Never
  prompts.
* ``interactive_login`` — a visible browser the user signs into. Needs a
  real display.
* ``install_cookies`` — for headless boxes with no display (a Codespace):
  load Canvas cookies exported from the user's normal browser into the
  persistent profile.

Fulton County Schools uses Microsoft (Entra ID) SSO, so an interactive
login redirects Canvas -> login.microsoftonline.com -> Canvas. We don't
script that chain; we poll ``/api/v1/users/self`` until it returns 200,
which is true regardless of how many redirects happened.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, async_playwright
from playwright.async_api import Error as PlaywrightError

from app.config import Settings, get_settings

_SINGLETON_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie")


def _clear_stale_singleton_locks(profile_dir: Path) -> None:
    """Remove Chrome's process-singleton files from the profile dir.

    Chrome writes ``SingletonLock`` as a symlink recording the hostname and
    pid that opened the profile, and refuses to launch against a profile
    whose lock names a different, unverifiable host. A Codespace rebuild
    changes the container's hostname but leaves ``data/`` (on the
    workspace volume) untouched, so every rebuild would otherwise brick
    the profile with a "profile appears to be in use ... on another
    computer" error, even though nothing is actually running. We don't
    run concurrent syncs against this profile, so clearing them
    unconditionally before each launch is safe.
    """
    for name in _SINGLETON_FILES:
        with contextlib.suppress(OSError):
            (profile_dir / name).unlink()


class SessionExpiredError(RuntimeError):
    """The stored Canvas session is no longer authenticated."""


class LoginTimeoutError(RuntimeError):
    """An interactive login did not complete before the timeout."""


class BrowserClosedError(RuntimeError):
    """The browser window closed (by the user or something else) before
    the login could be confirmed."""


class NoDisplayError(RuntimeError):
    """A visible browser was requested but there is no display."""


class BrowserLaunchError(RuntimeError):
    """We launched Chromium ourselves but it never opened a debugging port."""


@dataclass
class AuthStatus:
    ok: bool
    user: dict[str, Any] | None = None


async def check_auth(context: BrowserContext, base_url: str) -> AuthStatus:
    """Return whether ``context``'s cookies still authenticate to Canvas."""
    resp = await context.request.get(f"{base_url}/api/v1/users/self")
    if resp.ok:
        return AuthStatus(ok=True, user=await resp.json())
    return AuthStatus(ok=False)


async def _persist_session_cookies(
    context: BrowserContext, ttl_s: int
) -> None:
    """Give any session-only cookie in ``context`` (no Expires/Max-Age,
    which is what Canvas's own session cookie is) a concrete future
    expiry, so it survives ``context.close()``.

    Every CLI invocation launches its own ``launch_persistent_context``,
    does one thing, and closes it — and Chromium's correct, by-design
    behavior is to drop cookies that were never given an expiration when
    a context closes (that *is* what "session cookie" means). We're
    effectively "the browser" across many short-lived processes rather
    than one long-running one, so without this, a successful sign-in can
    report success and then leave nothing for the very next invocation to
    find. Only changes local retention — Canvas's server enforces its own
    session expiry independently of whatever we tell our own profile to
    keep.
    """
    cookies = await context.cookies()
    future = int(time.time()) + ttl_s
    session_only = [
        {**c, "expires": future}
        for c in cookies
        if not c.get("expires") or c["expires"] <= 0
    ]
    if session_only:
        await context.add_cookies(session_only)


def _launch_args() -> list[str]:
    return ["--disable-blink-features=AutomationControlled"]


def _has_display() -> bool:
    if sys.platform != "linux":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _cdp_port(cdp_url: str) -> int:
    return urllib.parse.urlparse(cdp_url).port or 9222


async def _cdp_alive(cdp_url: str) -> bool:
    def _check() -> None:
        urllib.request.urlopen(f"{cdp_url}/json/version", timeout=1).read()

    try:
        await asyncio.to_thread(_check)
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


async def _wait_for_cdp(cdp_url: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await _cdp_alive(cdp_url):
            return True
        await asyncio.sleep(0.3)
    return False


def _spawn_kwargs() -> dict[str, Any]:
    """Detach the child so it outlives us and doesn't inherit our stdio."""
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _spawn_chromium(
    executable_path: str, cdp_url: str, profile_dir: Path
) -> subprocess.Popen:
    """Launch Chromium as a plain OS process — not through Playwright's
    driver, which is what ``launch()``/``launch_persistent_context()`` do.

    This is the whole point: a driver-spawned browser (Node driver as
    parent, ``--remote-debugging-pipe`` IPC, a distinctive automation-flag
    set) is what gets killed on some managed devices. A browser started
    directly by our own process, over a plain TCP debugging port, is the
    same shape as a human launching it from a terminal — which is exactly
    what worked when done by hand. ``connect_over_cdp`` never spawns
    anything itself, so it's unaffected either way.
    """
    _clear_stale_singleton_locks(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        executable_path,
        f"--remote-debugging-port={_cdp_port(cdp_url)}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    return subprocess.Popen(args, **_spawn_kwargs())


class CanvasSession:
    """Headless authenticated context for syncing.

    Usage::

        async with CanvasSession() as session:
            client = CanvasClient(session.context.request)
            ...
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._pw = None
        self._context: BrowserContext | None = None
        self.user: dict[str, Any] | None = None

    @property
    def base_url(self) -> str:
        return self.settings.canvas_base_url

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("CanvasSession is not open")
        return self._context

    async def __aenter__(self) -> "CanvasSession":
        _clear_stale_singleton_locks(self.settings.browser_profile_dir)
        self._pw = await async_playwright().start()
        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.settings.browser_profile_dir),
            headless=self.settings.headless,
            viewport={"width": 1280, "height": 900},
            args=_launch_args(),
        )
        self._context.set_default_timeout(self.settings.request_timeout_ms)

        status = await check_auth(self._context, self.base_url)
        if not status.ok:
            await self._shutdown()
            raise SessionExpiredError(
                "Canvas session expired or missing. Run `python -m app.cli login` "
                "(or `python -m app.cli import-cookies <file>` on a headless box)."
            )
        self.user = status.user
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._shutdown()

    async def _shutdown(self) -> None:
        with contextlib.suppress(Exception):
            if self._context is not None:
                await self._context.close()
        with contextlib.suppress(Exception):
            if self._pw is not None:
                await self._pw.stop()
        self._context = None
        self._pw = None


async def interactive_login(settings: Settings | None = None) -> dict[str, Any]:
    """Open a visible browser at Canvas and wait for the user to sign in.

    Reuses the persistent profile, so a successful login here is what every
    later headless sync depends on. Returns the ``users/self`` payload.
    """
    settings = settings or get_settings()

    if not _has_display():
        raise NoDisplayError(
            "`python -m app.cli login` needs a visible browser, but this machine has no "
            "display.\nOn a Codespace / headless server, instead:\n"
            "  - python -m app.cli import-cookies <file>   (export Canvas cookies from "
            "your normal browser)\n"
            "  - or run `python -m app.cli login` on your own laptop/desktop"
        )

    _clear_stale_singleton_locks(settings.browser_profile_dir)
    pw = await async_playwright().start()
    try:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(settings.browser_profile_dir),
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=_launch_args(),
        )
    except Exception:
        await pw.stop()
        raise

    try:
        page = context.pages[0] if context.pages else await context.new_page()
        with contextlib.suppress(Exception):
            await page.goto(
                f"{settings.canvas_base_url}/login",
                wait_until="domcontentloaded",
            )

        deadline = time.monotonic() + settings.login_timeout_s
        while time.monotonic() < deadline:
            try:
                status = await check_auth(context, settings.canvas_base_url)
            except PlaywrightError as e:
                raise BrowserClosedError(
                    "The browser window closed before sign-in was "
                    "confirmed. If you didn't close it yourself, something "
                    "else on this machine (a crash, or a security/policy "
                    "agent) terminated it — check for any notification "
                    "that appeared, then try `python -m app.cli login` again."
                ) from e
            if status.ok:
                with contextlib.suppress(Exception):
                    await _persist_session_cookies(
                        context, settings.local_session_cookie_ttl_s
                    )
                return status.user or {}
            await asyncio.sleep(2)

        raise LoginTimeoutError(
            f"No successful Canvas login within {settings.login_timeout_s}s."
        )
    finally:
        with contextlib.suppress(Exception):
            await context.close()
        with contextlib.suppress(Exception):
            await pw.stop()


async def login_via_cdp(
    cdp_url: str = "http://localhost:9222",
    settings: Settings | None = None,
    auto_launch: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Sign in through a Chromium connected to over the Chrome DevTools
    Protocol, for machines where ``launch_persistent_context`` can't keep
    its own browser process alive — some managed-device security software
    kills a freshly spawned, automation-flagged browser within seconds,
    headed or headless, regardless of destination site.

    With ``auto_launch`` (the default), we start Chromium ourselves as a
    plain OS process via ``subprocess.Popen`` — not through Playwright's
    driver, which is specifically what ``launch()`` does and what gets
    killed. A browser started directly by our own process over a plain
    TCP debugging port is the same shape as a human launching it from a
    terminal, which is what worked when done by hand. If something's
    already listening at ``cdp_url`` (e.g. launched manually), we connect
    to that instead of starting a second one. Pass ``auto_launch=False``
    to always require a browser already running there.

    Doesn't rely on any persistent profile — everything needed comes back
    live over the connection during this one call: the confirmed user and
    the Canvas cookies, ready to write out (e.g. ``python -m app.cli
    login-cdp``) and carry to wherever the sync actually runs. We only
    *connect* (``connect_over_cdp`` never spawns anything itself), so
    closing our end afterward disconnects, it doesn't close the browser.
    """
    settings = settings or get_settings()
    pw = await async_playwright().start()

    if auto_launch and not await _cdp_alive(cdp_url):
        _spawn_chromium(
            pw.chromium.executable_path,
            cdp_url,
            settings.data_dir / "cdp-login-profile",
        )
        if not await _wait_for_cdp(cdp_url, timeout_s=20):
            await pw.stop()
            raise BrowserLaunchError(
                f"Launched Chromium but it never opened a debugging port "
                f"at {cdp_url}. Check for a security-software notification "
                f"that appeared, or start it yourself and try again."
            )

    try:
        browser = await pw.chromium.connect_over_cdp(cdp_url)
    except Exception:
        await pw.stop()
        raise

    try:
        context = (
            browser.contexts[0] if browser.contexts else await browser.new_context()
        )
        page = context.pages[0] if context.pages else await context.new_page()
        with contextlib.suppress(Exception):
            await page.goto(
                f"{settings.canvas_base_url}/login", wait_until="domcontentloaded"
            )

        deadline = time.monotonic() + settings.login_timeout_s
        while time.monotonic() < deadline:
            try:
                status = await check_auth(context, settings.canvas_base_url)
            except PlaywrightError as e:
                raise BrowserClosedError(
                    "Lost the connection to Chromium before sign-in was "
                    "confirmed. Make sure the window you launched stays "
                    "open, then run this again."
                ) from e
            if status.ok:
                cookies = await context.cookies()
                canvas_cookies = [
                    c for c in cookies if "instructure.com" in c.get("domain", "")
                ]
                return status.user or {}, canvas_cookies
            await asyncio.sleep(2)

        raise LoginTimeoutError(
            f"No successful Canvas login within {settings.login_timeout_s}s."
        )
    finally:
        with contextlib.suppress(Exception):
            await browser.close()  # disconnects only; doesn't kill the user's browser
        with contextlib.suppress(Exception):
            await pw.stop()


async def install_cookies(
    cookies: list[dict[str, Any]], settings: Settings | None = None
) -> dict[str, Any]:
    """Write browser-exported cookies into the persistent profile.

    Raises ``SessionExpiredError`` if they don't actually authenticate
    (usually means they were already stale when exported).
    """
    settings = settings or get_settings()
    _clear_stale_singleton_locks(settings.browser_profile_dir)
    pw = await async_playwright().start()
    try:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(settings.browser_profile_dir),
            headless=True,
            args=_launch_args(),
        )
        try:
            await context.add_cookies(cookies)  # type: ignore[arg-type]
            status = await check_auth(context, settings.canvas_base_url)
            if status.ok:
                with contextlib.suppress(Exception):
                    await _persist_session_cookies(
                        context, settings.local_session_cookie_ttl_s
                    )
        finally:
            await context.close()
    finally:
        await pw.stop()

    if not status.ok:
        raise SessionExpiredError(
            "Imported the cookies, but Canvas still reports not signed in. "
            "Re-export them while logged in and try again."
        )
    return status.user or {}

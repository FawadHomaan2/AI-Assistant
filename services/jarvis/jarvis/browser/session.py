"""Browser session management.

Jarvis drives its **own** Chromium profile, not yours. That is a deliberate
security decision, not a limitation: sharing your profile would mean a
prompt-injected page could act as you on every site you are signed into. Logging
the assistant into something is therefore a separate, explicit decision.

Navigation is also allowlist-gated by default. A model that reads a malicious
page and is told "now go to attacker.example and paste this" cannot, because the
host is not approved.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jarvis.config import paths
from jarvis.util.errors import JarvisError
from jarvis.util.logging import get_logger

log = get_logger(__name__)

#: Schemes that are never navigated to. `file:` would turn the browser into a
#: way around the path jail; `javascript:` executes in the page.
BLOCKED_SCHEMES = frozenset(
    {"file", "javascript", "data", "blob", "about", "chrome", "chrome-extension", "view-source"}
)

#: This machine. Separately switchable, because "open localhost:3000" is a
#: real thing a developer asks for.
_LOOPBACK_HOST = re.compile(
    r"^(localhost(\.localdomain)?|127(\.\d{1,3}){3}|0\.0\.0\.0|\[?::1\]?|::1)$",
    re.IGNORECASE,
)

#: The local network. Never navigable: a browser that can reach these becomes a
#: way to attack devices the user never exposed to the internet.
_OCTET = r"(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_PRIVATE_HOST = re.compile(
    rf"^(?:10\.{_OCTET}\.{_OCTET}\.{_OCTET}"
    rf"|192\.168\.{_OCTET}\.{_OCTET}"
    rf"|169\.254\.{_OCTET}\.{_OCTET}"
    rf"|172\.(?:1[6-9]|2\d|3[01])\.{_OCTET}\.{_OCTET}"
    rf"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.{_OCTET}\.{_OCTET}"
    rf"|f[cd][0-9a-f]{{2}}:.*"
    rf"|fe80:.*"
    rf"|.*\.local|.*\.internal|.*\.lan|.*\.home\.arpa)$",
    re.IGNORECASE,
)

#: Any RFC-3986 scheme, with or without "//".
_HAS_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*:", re.IGNORECASE)


def _why(exc: Exception) -> str:
    """The actionable first line of a Playwright error, not its whole call log."""
    first = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return first.replace("Page.goto: ", "").strip()


DEFAULT_TIMEOUT_MS = 20_000
MAX_EXTRACT_CHARS = 200_000


class NavigationRefused(JarvisError):
    code = "jarvis.browser.refused"
    http_status = 403


class NavigationFailed(JarvisError):
    """The address was allowed, but loading it did not work."""

    code = "jarvis.browser.navigation_failed"
    http_status = 502


class InteractionFailed(JarvisError):
    """A selector did not match, or the element would not accept the action."""

    code = "jarvis.browser.interaction_failed"
    http_status = 400


class BrowserUnavailable(JarvisError):
    """Playwright or the Chromium build is missing. Rule 16: say which."""

    code = "jarvis.browser.unavailable"
    http_status = 501


@dataclass
class BrowserSettings:
    headless: bool = True
    #: Hosts the browser may visit. Empty means nothing is allowed yet.
    allowed_hosts: set[str] = field(default_factory=set)
    #: When True, any public host is allowed. Off by default.
    allow_any_host: bool = False
    #: Loopback only — `localhost` and `127.x`. Off by default: a browser that
    #: can reach this machine is a way to drive services the user never
    #: exposed, including Jarvis's own API. Private LAN ranges stay refused
    #: whatever this says; "Jarvis browses my router" is not worth the risk.
    allow_loopback: bool = False
    #: Path to a Chromium or Edge binary, when one is already installed. Empty
    #: means use the Chromium that `playwright install` downloaded.
    executable_path: str = ""
    timeout_ms: int = DEFAULT_TIMEOUT_MS


def check_url(raw: str, settings: BrowserSettings) -> str:
    """Validate a URL before navigating. Returns the normalised URL."""
    text = (raw or "").strip()
    if not text:
        raise NavigationRefused("No address was given.")

    # Bare domains are common in speech ("go to example.com"), but only add a
    # scheme when there genuinely is none — "javascript:alert(1)" has one, and
    # prepending https:// to it would turn a scheme check into a host check and
    # produce a misleading refusal.
    if not _HAS_SCHEME.match(text):
        text = f"https://{text}"

    try:
        parsed = urlparse(text)
    except ValueError as exc:
        raise NavigationRefused(f"{raw!r} is not a usable address.") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme in BLOCKED_SCHEMES:
        raise NavigationRefused(
            f"Jarvis will not open {scheme}: addresses. "
            + (
                "Reading local files through the browser would bypass the folder "
                "permissions you set."
                if scheme == "file"
                else "That scheme can execute code in the page."
            )
        )
    if scheme not in ("http", "https"):
        raise NavigationRefused(f"Only http and https addresses are supported, not {scheme!r}.")

    host = (parsed.hostname or "").lower()
    if not host:
        raise NavigationRefused(f"{raw!r} has no host.")

    if _LOOPBACK_HOST.match(host):
        if not settings.allow_loopback:
            raise NavigationRefused(
                f"{host} is on this machine. Jarvis will not browse there unless "
                f"you turn on local browsing, because it would turn the browser "
                f"into a way to reach services you never exposed — including "
                f"Jarvis's own API."
            )
        # The host allowlist governs which *sites* Jarvis may visit. Loopback is
        # governed by its own switch, so it does not also need allowlisting.
        return text

    if _PRIVATE_HOST.match(host):
        raise NavigationRefused(
            f"{host} is on your local network. Jarvis does not browse there: a "
            f"page it reads could otherwise aim it at your router or a device "
            f"that is not on the internet at all."
        )

    if not settings.allow_any_host:
        permitted = settings.allowed_hosts
        if not any(host == allowed or host.endswith(f".{allowed}") for allowed in permitted):
            raise NavigationRefused(
                f"{host} is not in the list of sites Jarvis may visit. "
                f"Allowed: {', '.join(sorted(permitted)) or 'none yet'}. "
                f"You can add it in Privacy settings."
            )

    return text


@dataclass
class PageSnapshot:
    url: str
    title: str
    text: str
    links: list[dict[str, str]] = field(default_factory=list)
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "text": self.text,
            "links": self.links[:100],
            "truncated": self.truncated,
        }


class BrowserSession:
    """A Playwright browser Jarvis owns, started on demand."""

    def __init__(self, settings: BrowserSettings | None = None) -> None:
        self.settings = settings or BrowserSettings()
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._lock = asyncio.Lock()

    @property
    def profile_dir(self) -> Path:
        return paths.data_dir() / "browser-profile"

    @staticmethod
    def availability(executable_path: str = "") -> tuple[bool, str]:
        """Whether a browser can actually be started, and if not, what is missing.

        Checked in two parts because they fail for different reasons and need
        different fixes: the Python package, then the browser binary itself.
        """
        try:
            import playwright  # noqa: F401
        except ImportError:
            return False, (
                "Browser automation needs the 'playwright' package. Install the "
                "'browser' extra: pip install 'jarvis[browser]'"
            )
        if executable_path and not Path(executable_path).exists():
            return False, (
                f"The browser set in configuration does not exist: {executable_path}. "
                f"Correct the path, or clear it to use the Chromium Playwright "
                f"downloads."
            )
        return True, "Playwright is installed."

    async def _ensure(self) -> Any:
        """Start the browser if it is not already running."""
        if self._page is not None:
            return self._page

        ok, detail = self.availability(self.settings.executable_path)
        if not ok:
            raise BrowserUnavailable(detail)

        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        launch: dict[str, Any] = {"headless": self.settings.headless}
        if self.settings.executable_path:
            launch["executable_path"] = self.settings.executable_path
        try:
            self._browser = await self._playwright.chromium.launch(**launch)
        except Exception as exc:
            await self.close()
            raise BrowserUnavailable(
                f"Could not start Chromium: {exc}. If this is a fresh install, "
                f"run: playwright install chromium"
            ) from exc

        # A dedicated context: Jarvis never borrows your signed-in browser.
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            accept_downloads=False,
        )
        self._context.set_default_timeout(self.settings.timeout_ms)
        self._page = await self._context.new_page()
        log.info("browser started", headless=self.settings.headless)
        return self._page

    async def goto(self, url: str) -> PageSnapshot:
        checked = check_url(url, self.settings)
        async with self._lock:
            page = await self._ensure()
            try:
                await page.goto(checked, wait_until="domcontentloaded")
            except Exception as exc:
                raise NavigationFailed(f"Could not open {checked}: {_why(exc)}") from exc
            return await self._snapshot(page)

    async def snapshot(self) -> PageSnapshot:
        async with self._lock:
            page = await self._ensure()
            return await self._snapshot(page)

    def _landed_somewhere_allowed(self, page: Any) -> None:
        """Check where the page actually ended up, not where it was sent.

        `goto` validates the address it is given, but a redirect, a meta
        refresh or a click can land somewhere else entirely. Extraction is the
        one place page content enters the system, so it is the right choke
        point: if the final host is not allowed, nothing is read from it.
        """
        landed = str(page.url or "")
        if not landed or landed.startswith("about:"):
            return
        try:
            check_url(landed, self.settings)
        except NavigationRefused as exc:
            raise NavigationRefused(
                f"The page ended up at {landed}, which is not allowed: "
                f"{exc.message} Nothing was read from it."
            ) from exc

    async def _snapshot(self, page: Any) -> PageSnapshot:
        """Read the page as text, the way a person would see it.

        `innerText` rather than the HTML source: it reflects what is rendered,
        skips scripts and markup, and is far cheaper to hand a model.
        """
        self._landed_somewhere_allowed(page)
        title = await page.title()
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        links = await page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href]'))
                .slice(0, 200)
                .map(a => ({ text: (a.innerText || '').trim().slice(0, 120), href: a.href }))
                .filter(l => l.text)"""
        )
        truncated = len(text) > MAX_EXTRACT_CHARS
        return PageSnapshot(
            url=page.url,
            title=title,
            text=text[:MAX_EXTRACT_CHARS],
            links=links,
            truncated=truncated,
        )

    async def click(self, selector: str) -> str:
        async with self._lock:
            page = await self._ensure()
            try:
                await page.click(selector)
                await page.wait_for_load_state("domcontentloaded")
            except Exception as exc:
                raise InteractionFailed(f"Could not click {selector!r}: {_why(exc)}") from exc
            landed = str(page.url)
            # A click can navigate anywhere. If it left the allowed set, blank
            # the page so its content cannot reach the model, and say so.
            try:
                check_url(landed, self.settings)
            except NavigationRefused as exc:
                with contextlib.suppress(Exception):
                    await page.goto("about:blank")
                raise NavigationRefused(
                    f"Clicking {selector!r} navigated to {landed}, which Jarvis "
                    f"may not visit: {exc.message} The page was closed without "
                    f"being read."
                ) from exc
            return landed

    async def fill(self, selector: str, value: str) -> None:
        async with self._lock:
            page = await self._ensure()
            try:
                await page.fill(selector, value)
            except Exception as exc:
                raise InteractionFailed(f"Could not type into {selector!r}: {_why(exc)}") from exc

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        """Run one of Jarvis's own extraction scripts against the page.

        The script is always a literal from this repository — never anything a
        model wrote and never anything read off a page. That restriction is the
        only thing keeping "extract the results" from becoming "run whatever
        the page talked the model into running", so it is not parameterised.
        """
        async with self._lock:
            page = await self._ensure()
            self._landed_somewhere_allowed(page)
            try:
                return await page.evaluate(script, arg)
            except Exception as exc:
                raise InteractionFailed(f"Could not read the page: {_why(exc)}") from exc

    async def current_url(self) -> str:
        if self._page is None:
            return ""
        return str(self._page.url)

    async def close(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                with contextlib.suppress(Exception):
                    await closer.close()
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
        self._playwright = self._browser = self._context = self._page = None

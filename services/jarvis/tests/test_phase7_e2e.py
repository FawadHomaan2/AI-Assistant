"""Phase 7 end-to-end: a real Chromium against a real HTTP server.

This is the phase gate — navigate, extract, and a confirmation-gated form
submission — exercised against an actual browser rather than a stub, because
the parts most likely to be wrong (selector behaviour, what `innerText`
returns, whether a submit really reaches the server) cannot be faked.

The server runs on loopback, so these tests switch `allow_loopback` on
explicitly. That is the same switch a developer would use for their own dev
server, and it is off everywhere else.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import http.server
import os
import threading
from pathlib import Path
from typing import Any, ClassVar

import pytest

from jarvis.agents.executor import Executor
from jarvis.agents.types import EventType
from jarvis.browser.session import (
    BrowserSession,
    BrowserSettings,
    InteractionFailed,
    NavigationRefused,
    playwright_available,
)
from jarvis.db.engine import Database
from jarvis.db.repositories import AuditRepository
from jarvis.governance.consent import ConsentAnswer, ConsentBroker
from jarvis.governance.estop import EmergencyStop
from jarvis.governance.policy import Policy
from jarvis.governance.risk import Risk
from jarvis.governance.scopes import Scope, ScopeGrants
from jarvis.tools.browser import BrowserTool
from jarvis.tools.registry import ToolRegistry

ARTICLE = """<!doctype html><html><head><title>Quiet keyboards</title></head><body>
<h1>The quietest keyboards of 2026</h1>
<p>Linear switches are quieter than clicky ones.</p>
<p>Lubrication helps more than the switch choice.</p>
<a href="/form">Buy one</a>
<a href="/other">Related reading</a>
<script>document.title = document.title;</script>
</body></html>"""

FORM = """<!doctype html><html><head><title>Checkout</title></head><body>
<h1>Confirm your order</h1>
<form action="/submitted" method="get">
  <input id="card" name="card" value="">
  <input id="note" name="note" value="">
  <button id="place-order" type="submit">Place order</button>
</form>
</body></html>"""

DONE = """<!doctype html><html><head><title>Order placed</title></head>
<body><h1>Thank you</h1></body></html>"""

OTHER = """<!doctype html><html><head><title>Related</title></head>
<body><p>More words.</p></body></html>"""

PAGES = {"/": ARTICLE, "/form": FORM, "/submitted": DONE, "/other": OTHER}


class Handler(http.server.BaseHTTPRequestHandler):
    #: Every path the server was asked for, so a test can prove a request
    #: was never made as well as that it was.
    seen: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        Handler.seen.append(self.path)
        body = PAGES.get(path)
        if body is None:
            self.send_error(404)
            return
        encoded = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_: Any) -> None:
        """Silence the default stderr logging."""


def find_browser() -> str:
    """A Chromium that actually exists, or "" to let Playwright find its own."""
    if env := os.environ.get("JARVIS_TEST_CHROMIUM"):
        return env
    # Preinstalled builds, as used by this container's image.
    for candidate in ("/opt/pw-browsers/chromium",):
        if Path(candidate).exists():
            return candidate
    return ""


@functools.cache
def browser_runs() -> bool:
    """Try actually starting Chromium, rather than trusting that it is there.

    `availability()` only proves the Python package imports. A CI image with
    playwright installed but no browser downloaded would pass that check and
    then fail every test here with a launch error, which reads as "the browser
    code is broken" rather than "the browser is not installed".
    """
    ok, _ = playwright_available(find_browser())
    if not ok:
        return False

    async def launch() -> bool:
        session = BrowserSession(BrowserSettings(headless=True, executable_path=find_browser()))
        try:
            await session._ensure()
            return True
        except Exception:
            return False
        finally:
            await session.close()

    return asyncio.new_event_loop().run_until_complete(launch())


requires_browser = pytest.mark.skipif(
    not browser_runs(), reason="no usable Chromium; run `playwright install chromium`"
)


@pytest.fixture
def server():
    Handler.seen = []
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def no_proxy_env(monkeypatch):
    """Keep the browser off any HTTP proxy for loopback addresses.

    CI images often set `http_proxy`; Chromium honours it and would try to
    fetch 127.0.0.1 through it.
    """
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(name, "127.0.0.1,localhost")


@pytest.fixture
async def session(no_proxy_env):
    browser = BrowserSession(
        BrowserSettings(
            headless=True,
            allow_loopback=True,
            executable_path=find_browser(),
            timeout_ms=15_000,
        )
    )
    yield browser
    await browser.close()


# ── navigate and extract ─────────────────────────────────────────────────
@requires_browser
async def test_navigate_and_extract_real_page(session, server) -> None:
    snapshot = await session.goto(f"{server}/")
    assert snapshot.title == "Quiet keyboards"
    assert "quietest keyboards of 2026" in snapshot.text
    assert "Linear switches are quieter" in snapshot.text
    # innerText, not the HTML source: no tags and no script bodies.
    assert "<h1>" not in snapshot.text
    assert "document.title" not in snapshot.text
    assert not snapshot.truncated


@requires_browser
async def test_links_are_extracted_with_absolute_urls(session, server) -> None:
    snapshot = await session.goto(f"{server}/")
    hrefs = {link["href"] for link in snapshot.links}
    assert f"{server}/form" in hrefs
    assert f"{server}/other" in hrefs
    assert {link["text"] for link in snapshot.links} == {"Buy one", "Related reading"}


@requires_browser
async def test_a_refused_address_never_starts_a_request(session, server) -> None:
    session.settings.allow_loopback = False
    with pytest.raises(NavigationRefused):
        await session.goto(f"{server}/")
    assert Handler.seen == []


@requires_browser
async def test_a_bad_selector_fails_with_its_name(session, server) -> None:
    await session.goto(f"{server}/form")
    with pytest.raises(InteractionFailed) as exc:
        await session.fill("#does-not-exist", "x")
    assert "#does-not-exist" in exc.value.message


@requires_browser
async def test_evaluate_reads_the_rendered_page(session, server) -> None:
    await session.goto(f"{server}/")
    count = await session.evaluate("() => document.querySelectorAll('p').length")
    assert count == 2


# ── fill and submit ──────────────────────────────────────────────────────
@requires_browser
async def test_fill_and_submit_reaches_the_server(session, server) -> None:
    await session.goto(f"{server}/form")
    await session.fill("#card", "4111111111111111")
    landed = await session.click("#place-order")
    assert landed.endswith("card=4111111111111111&note=")
    assert any("/submitted?" in seen for seen in Handler.seen)
    assert (await session.snapshot()).title == "Order placed"


@requires_browser
async def test_clicking_off_the_allowlist_blanks_the_page(session, server) -> None:
    """A click can navigate anywhere. Content from a refused host is not read."""
    await session.goto(f"{server}/")
    # Narrow the rules *after* loading, as revoking a permission mid-session would.
    session.settings.allow_loopback = False
    session.settings.allowed_hosts = {"example.com"}
    with pytest.raises(NavigationRefused) as exc:
        await session.click("a[href='/form']")
    assert "without being read" in exc.value.message
    assert await session.current_url() == "about:blank"


@requires_browser
async def test_extraction_is_refused_when_the_page_is_off_the_allowlist(session, server) -> None:
    """The choke point: page content cannot enter the system from a refused host."""
    await session.goto(f"{server}/")
    session.settings.allow_loopback = False
    with pytest.raises(NavigationRefused) as exc:
        await session.snapshot()
    assert "Nothing was read from it" in exc.value.message


# ── the full gated path ──────────────────────────────────────────────────
class Gate:
    """A real executor around the real browser tool."""

    def __init__(self, session: BrowserSession, *, approve: bool) -> None:
        self.db = Database(":memory:")
        self.audit = AuditRepository(self.db)
        self.estop = EmergencyStop()
        self.policy = Policy(ScopeGrants.of({Scope.BROWSER_USE}), mode="guarded")
        self.prompts: list[Any] = []
        self.approve = approve
        self.consent = ConsentBroker(prompt=self._answer, timeout=5)
        registry = ToolRegistry()
        registry.register(BrowserTool(session))
        self.executor = Executor(registry, self.policy, self.consent, self.audit, self.estop)

    async def _answer(self, request: Any) -> None:
        self.prompts.append(request)
        asyncio.get_running_loop().call_soon(
            self.consent.resolve, request.id, ConsentAnswer(approved=self.approve, remember="no")
        )

    async def run(self, args: dict[str, Any]) -> list[Any]:
        return [e async for e in self.executor.run("browser", args, origin="test")]

    def close(self) -> None:
        self.db.close()


def events_of(events: list[Any], kind: EventType) -> list[Any]:
    return [e for e in events if e.type is kind]


@requires_browser
async def test_opening_a_page_needs_no_confirmation(session, server) -> None:
    gate = Gate(session, approve=True)
    try:
        events = await gate.run({"operation": "open", "url": f"{server}/"})
        results = events_of(events, EventType.TOOL_RESULT)
        assert results and results[0].data["ok"] is True
        assert gate.prompts == []
    finally:
        gate.close()


@requires_browser
async def test_submitting_an_order_asks_first_and_says_what_it_is(session, server) -> None:
    gate = Gate(session, approve=True)
    try:
        await gate.run({"operation": "open", "url": f"{server}/form"})
        await gate.run({"operation": "fill", "selector": "#note", "value": "gift wrap"})
        # Typing on a page called "Checkout" is itself confirmed in guarded
        # mode. That is intended, so the submit is measured on its own.
        assert len(gate.prompts) == 1, "typing on a checkout page is confirmed"
        gate.prompts.clear()

        events = await gate.run({"operation": "submit", "selector": "#place-order"})

        assert len(gate.prompts) == 1, "a submit must be confirmed"
        prompt = gate.prompts[0]
        assert prompt.risk == Risk.CRITICAL.label
        assert "purchase" in prompt.summary or "purchase" in prompt.blast_radius
        assert prompt.reversible == "permanent"
        # A tier-5 action is not confirmed with a single click.
        assert prompt.confirm_phrase == "CONFIRM"
        assert prompt.allow_remember is False, "a purchase must never be remembered"

        results = events_of(events, EventType.TOOL_RESULT)
        assert results and results[0].data["ok"] is True
        assert any("/submitted?" in seen for seen in Handler.seen)
    finally:
        gate.close()


@requires_browser
async def test_declining_the_confirmation_means_nothing_is_submitted(session, server) -> None:
    gate = Gate(session, approve=False)
    try:
        await gate.run({"operation": "open", "url": f"{server}/form"})
        Handler.seen = []
        events = await gate.run({"operation": "submit", "selector": "#place-order"})

        assert len(gate.prompts) == 1
        assert events_of(events, EventType.TOOL_RESULT) == []
        notices = events_of(events, EventType.NOTICE)
        assert notices and notices[0].data["blocked"] is True
        assert Handler.seen == [], "the form must not have been sent"
        assert await session.current_url() == f"{server}/form"
    finally:
        gate.close()


@requires_browser
async def test_browsing_without_the_scope_is_denied_before_the_browser_starts(
    session, server
) -> None:
    gate = Gate(session, approve=True)
    gate.policy = Policy(ScopeGrants.of(set()), mode="guarded")
    gate.executor.policy = gate.policy
    try:
        events = await gate.run({"operation": "open", "url": f"{server}/"})
        notices = events_of(events, EventType.NOTICE)
        assert notices and notices[0].data["blocked"] is True
        assert "browser.use" in notices[0].data["missingScopes"]
        assert Handler.seen == []
    finally:
        gate.close()


@requires_browser
async def test_emergency_stop_halts_browsing(session, server) -> None:
    gate = Gate(session, approve=True)
    try:
        gate.estop.engage("test")
        events = await gate.run({"operation": "open", "url": f"{server}/"})
        assert events_of(events, EventType.ERROR)
        assert Handler.seen == []
    finally:
        gate.close()


@requires_browser
async def test_audit_records_the_submission_without_the_typed_value(session, server) -> None:
    gate = Gate(session, approve=True)
    try:
        await gate.run({"operation": "open", "url": f"{server}/form"})
        await gate.run({"operation": "fill", "selector": "#card", "value": "4111111111111111"})
        entries = gate.audit.recent(limit=20)
        assert any(e["action"] == "tool.browser.fill" for e in entries)
        assert "4111111111111111" not in str(entries)
    finally:
        gate.close()


@requires_browser
async def test_the_browser_closes_cleanly(no_proxy_env, server) -> None:
    browser = BrowserSession(BrowserSettings(allow_loopback=True, executable_path=find_browser()))
    await browser.goto(f"{server}/")
    await browser.close()
    assert await browser.current_url() == ""
    # Closing twice must not raise; shutdown runs on paths that may already
    # have closed it.
    with contextlib.suppress(Exception):
        await browser.close()


@requires_browser
async def test_a_second_navigation_reuses_the_same_browser(session, server) -> None:
    await session.goto(f"{server}/")
    # Reaching into the private handle on purpose: the claim is that the
    # Chromium process is reused, which only the handle can show.
    first = session._browser
    await session.goto(f"{server}/other")
    assert session._browser is first
    assert (await session.snapshot()).title == "Related"


@requires_browser
async def test_concurrent_navigations_do_not_interleave(session, server) -> None:
    """The session lock means two turns cannot drive one page at once."""
    results = await asyncio.gather(
        session.goto(f"{server}/"),
        session.goto(f"{server}/other"),
    )
    assert {r.title for r in results} == {"Quiet keyboards", "Related"}

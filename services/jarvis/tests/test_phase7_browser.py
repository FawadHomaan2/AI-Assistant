"""Browser tool: sensitivity detection, risk escalation and preview honesty.

These run against a stub session, so they test the tool's judgement rather than
Chromium. The real browser is exercised in `test_phase7_e2e.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from jarvis.browser.session import BrowserSettings, PageSnapshot
from jarvis.governance.risk import Risk
from jarvis.tools.base import ToolInputInvalid
from jarvis.tools.browser import BrowserTool, sensitivity, words
from jarvis.tools.websearch import ENGINES, WebSearchTool, parse_results, unwrap


class StubSession:
    """A session that reports a page without starting a browser."""

    def __init__(self, url: str = "https://shop.example.com/cart", title: str = "Your basket"):
        self.settings = BrowserSettings(allowed_hosts={"example.com"})
        self._url = url
        self._title = title
        self.calls: list[tuple[str, Any]] = []

    def availability(self) -> tuple[bool, str]:
        """A stand-in session is always usable.

        This is why `availability` is an instance method. When the tool asked
        the concrete class instead, every test in this file silently became a
        test of "is Playwright installed" and passed only where it was.
        """
        return True, "Stub session."

    async def current_url(self) -> str:
        return self._url

    async def snapshot(self) -> PageSnapshot:
        return PageSnapshot(url=self._url, title=self._title, text="body text")

    async def goto(self, url: str) -> PageSnapshot:
        self.calls.append(("goto", url))
        self._url = url
        return PageSnapshot(url=url, title=self._title, text="body text")

    async def click(self, selector: str) -> str:
        self.calls.append(("click", selector))
        return self._url

    async def fill(self, selector: str, value: str) -> None:
        self.calls.append(("fill", (selector, value)))

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        self.calls.append(("evaluate", arg))
        return []


def tool(**kw: Any) -> BrowserTool:
    return BrowserTool(StubSession(**kw))  # type: ignore[arg-type]


# ── what counts as sensitive ─────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("#password", "a password"),
        ("input[name=passwd]", "a password"),
        ("Sign in to your account", "sign-in credentials"),
        ("#login-form", "sign-in credentials"),
        ("#username", "sign-in credentials"),
        ("Enter the verification code", "a one-time code"),
        ("#card-number", "payment details"),
        ("cvv", "payment details"),
        ("Checkout", "a purchase"),
        ("Place order", "a purchase"),
        ("Transfer money", "a money transfer"),
        ("iban", "a money transfer"),
        ("Delete account", "closing or cancelling an account"),
        ("Cancel subscription", "closing or cancelling an account"),
        ("Change password", "a password"),
        ("Recovery email", "account security settings"),
        ("national insurance number", "government identity details"),
    ],
)
def test_sensitive_text_is_recognised(text: str, expected: str) -> None:
    assert sensitivity(text) == expected


@pytest.mark.parametrize(
    "text",
    ["#q", "Search results", "Newsletter signup", "#comment", "Read more", "Wikipedia"],
)
def test_ordinary_text_is_not_sensitive(text: str) -> None:
    assert sensitivity(text) == ""


def test_sensitivity_ignores_empty_parts() -> None:
    assert sensitivity("", "#card", "") == "payment details"


@pytest.mark.parametrize(
    "selector",
    [
        "button#place-order",
        "#placeOrder",
        "#place_order",
        "form.checkout > button[name=placeOrder]",
        "a[href='/shop/place-order']",
    ],
)
def test_word_breaks_in_selectors_are_understood(selector: str) -> None:
    """Regression: "#place-order" contains no space, so "place order" missed it."""
    assert sensitivity(selector) == "a purchase"


def test_word_breaks_in_urls_are_understood() -> None:
    assert sensitivity("https://bank.example/transfer-money/new") == "a money transfer"
    assert sensitivity("https://example.com/account/delete-account") == (
        "closing or cancelling an account"
    )


def test_words_leaves_ordinary_prose_readable() -> None:
    assert words("Sign in to your account") == "Sign in to your account"


# ── risk escalation ──────────────────────────────────────────────────────
async def test_reading_is_safe() -> None:
    t = tool()
    for operation in ("read", "links", "current"):
        args = {"operation": operation}
        assert t.risk_for(args, await t.preview(args)) is Risk.SAFE


async def test_opening_a_page_is_low() -> None:
    t = tool()
    args = {"operation": "open", "url": "https://example.com"}
    assert t.risk_for(args, await t.preview(args)) is Risk.LOW


async def test_typing_into_a_password_field_is_critical() -> None:
    t = tool(title="Sign in")
    args = {"operation": "fill", "selector": "#password", "value": "hunter2"}
    preview = await t.preview(args)
    assert preview.metadata["sensitiveWhere"] == "field"
    assert t.risk_for(args, preview) is Risk.CRITICAL


async def test_a_password_shaped_value_escalates_even_in_an_innocent_field() -> None:
    """The value is checked too: a password typed into "#q" is still a password."""
    t = tool(title="Search")
    args = {"operation": "fill", "selector": "#q", "value": "my passphrase is open sesame"}
    assert t.risk_for(args, await t.preview(args)) is Risk.CRITICAL


async def test_typing_into_a_search_box_on_a_login_page_is_not_critical() -> None:
    """Page context alone must not escalate typing.

    If it did, every box on a page titled "Sign in" would demand the same
    confirmation as a password field — and a prompt that cries wolf is one
    people learn to click through.
    """
    t = tool(url="https://example.com/login", title="Sign in")
    args = {"operation": "fill", "selector": "#q", "value": "cats"}
    preview = await t.preview(args)
    assert preview.metadata["sensitiveWhere"] == "page"
    assert t.risk_for(args, preview) is Risk.MEDIUM


async def test_submitting_an_ordinary_form_is_high() -> None:
    t = tool(url="https://example.com/contact", title="Contact us")
    args = {"operation": "submit", "selector": "#send"}
    assert t.risk_for(args, await t.preview(args)) is Risk.HIGH


async def test_submitting_on_a_checkout_page_is_critical() -> None:
    t = tool(url="https://example.com/checkout", title="Checkout — pay now")
    args = {"operation": "submit", "selector": "#confirm"}
    preview = await t.preview(args)
    assert t.risk_for(args, preview) is Risk.CRITICAL
    assert preview.reversible == "permanent"
    assert "cannot undo" in preview.blast_radius


async def test_clicking_a_place_order_button_is_critical() -> None:
    t = tool(url="https://example.com/cart", title="Your basket")
    args = {"operation": "click", "selector": "button#place-order"}
    assert t.risk_for(args, await t.preview(args)) is Risk.CRITICAL


async def test_clicking_on_a_sensitive_page_is_high_not_critical() -> None:
    t = tool(url="https://example.com/checkout", title="Checkout")
    args = {"operation": "click", "selector": "a.help-link"}
    assert t.risk_for(args, await t.preview(args)) is Risk.HIGH


async def test_clicking_an_ordinary_link_is_medium() -> None:
    t = tool(url="https://example.com/news", title="Today's news")
    args = {"operation": "click", "selector": "a.story"}
    assert t.risk_for(args, await t.preview(args)) is Risk.MEDIUM


async def test_risk_is_not_read_out_of_the_prose() -> None:
    """Regression: risk once came from searching `blast_radius` for "sensitive".

    Prose gets reworded; a flag does not. This asserts the wording and the
    decision are independent.
    """
    t = tool(url="https://example.com/news", title="Today's news")
    args = {"operation": "click", "selector": "a.story"}
    preview = await t.preview(args)
    preview.blast_radius = "this text mentions sensitive things"
    assert t.risk_for(args, preview) is Risk.MEDIUM


# ── previews tell the truth ──────────────────────────────────────────────
async def test_unknown_operation_is_rejected() -> None:
    with pytest.raises(ToolInputInvalid):
        await tool().preview({"operation": "download_everything"})


async def test_refused_address_is_a_blocked_preview_not_an_error() -> None:
    preview = await tool().preview({"operation": "open", "url": "file:///etc/passwd"})
    assert preview.blocked
    assert "folder permissions" in preview.blocked


async def test_interacting_with_no_page_open_is_blocked() -> None:
    preview = await tool(url="").preview({"operation": "click", "selector": "a"})
    assert "No page is open" in preview.blocked


async def test_open_preview_says_the_signed_in_browser_is_not_used() -> None:
    preview = await tool().preview({"operation": "open", "url": "https://example.com"})
    assert "signed-in browser is not used" in preview.blast_radius


async def test_fill_never_records_the_value() -> None:
    """A typed value may be a password, and results are written to disk."""
    t = tool()
    args = {"operation": "fill", "selector": "#password", "value": "hunter2"}
    preview = await t.preview(args)
    result = await t.execute(args)
    assert "hunter2" not in preview.summary + preview.blast_radius + str(preview.metadata)
    assert "hunter2" not in result.summary + str(result.data) + str(result.changes)


async def test_selector_is_required_for_interaction() -> None:
    with pytest.raises(ToolInputInvalid):
        await tool().preview({"operation": "click"})


# ── web search ───────────────────────────────────────────────────────────
def test_search_url_encodes_the_query() -> None:
    url = ENGINES["duckduckgo"].url_for("rust async traits & futures")
    assert "rust+async+traits+%26+futures" in url


def test_redirect_links_are_unwrapped() -> None:
    wrapped = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&rut=x"
    assert unwrap(wrapped, "uddg") == "https://example.com/a"


def test_unwrap_leaves_a_plain_link_alone() -> None:
    assert unwrap("https://example.com/a", "uddg") == "https://example.com/a"
    assert unwrap("https://example.com/a", "") == "https://example.com/a"


def test_fallback_parser_skips_the_engines_own_links() -> None:
    snapshot = PageSnapshot(
        url="https://duckduckgo.com/html/?q=x",
        title="x at DuckDuckGo",
        text="",
        links=[
            {"text": "Settings", "href": "https://duckduckgo.com/settings"},
            {"text": "A result", "href": "https://example.com/a"},
            {"text": "Same again", "href": "https://example.com/a"},
            {"text": "Relative", "href": "/about"},
        ],
    )
    results = parse_results(snapshot, ENGINES["duckduckgo"])
    assert [r["url"] for r in results] == ["https://example.com/a"]


async def test_search_preview_says_where_the_words_go() -> None:
    session = StubSession()
    session.settings = BrowserSettings(allowed_hosts={"duckduckgo.com"})
    preview = await WebSearchTool(session).preview({"query": "quietest keyboard"})  # type: ignore[arg-type]
    assert "duckduckgo.com" in preview.blast_radius
    assert "sees what you asked" in preview.blast_radius


async def test_search_is_blocked_when_the_engine_is_not_allowlisted() -> None:
    session = StubSession()
    session.settings = BrowserSettings(allowed_hosts={"example.com"})
    preview = await WebSearchTool(session).preview({"query": "x"})  # type: ignore[arg-type]
    assert preview.blocked
    assert "duckduckgo.com" in preview.blocked


async def test_empty_query_is_rejected() -> None:
    with pytest.raises(ToolInputInvalid):
        await WebSearchTool(StubSession()).preview({"query": "   "})  # type: ignore[arg-type]


async def test_overlong_query_is_rejected_with_the_limit() -> None:
    with pytest.raises(ToolInputInvalid) as exc:
        await WebSearchTool(StubSession()).preview({"query": "a" * 5000})  # type: ignore[arg-type]
    assert "400" in exc.value.message


async def test_the_tool_asks_its_session_not_the_class(monkeypatch) -> None:
    """Regression: these tests used to need Playwright installed to say anything.

    The CI job that installs no browser extra turned 14 assertions about risk
    judgement into assertions about a missing package.
    """
    import jarvis.browser.session as session_module

    monkeypatch.setattr(
        session_module,
        "playwright_available",
        lambda path="": (False, "Playwright is not installed"),
    )
    preview = await tool().preview({"operation": "open", "url": "https://example.com"})
    assert preview.blocked == "", "the injected session decides, not the installed package"


def test_an_unknown_engine_falls_back_rather_than_crashing() -> None:
    assert WebSearchTool(StubSession(), "not-an-engine").engine_name == "duckduckgo"  # type: ignore[arg-type]


# ── the status endpoint ──────────────────────────────────────────────────
async def test_browser_status_reports_the_real_allowlist(client) -> None:
    """The interface must show the configured hosts, not an intended default."""
    response = await client.get("/browser/status")
    assert response.status_code == 200
    body = response.json()
    assert body["allowedHosts"] == ["duckduckgo.com"]
    assert body["allowAnyHost"] is False
    assert body["allowLoopback"] is False
    assert body["ownProfile"] is True
    assert body["searchEngine"] == "duckduckgo"
    assert body["running"] is False


async def test_browser_status_needs_a_token(app) -> None:
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as anonymous:
        assert (await anonymous.get("/browser/status")).status_code == 401


async def test_browser_and_search_tools_are_registered(ctx) -> None:
    assert ctx.registry.has("browser")
    assert ctx.registry.has("websearch")
    spec = next(s for s in ctx.registry.specs() if s["name"] == "browser")
    assert spec["scopes"] == ["browser.use"]

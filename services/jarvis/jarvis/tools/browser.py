"""BrowserTool — drive a web page.

Risk rises with what an action does to the *world*, not to this computer:

  navigate / read      LOW / SAFE  — looking at a page
  click / fill         MEDIUM      — interacting with a page
  submit               HIGH        — the point of no return on most sites
  anything sensitive   CRITICAL    — credentials, payment, purchase, account

That last tier exists because "fill in this form and submit it" is the request
most likely to do something irreversible on the user's behalf. The tool decides
this itself from what the field and the page appear to be for, rather than
trusting the model to flag it — a prompt-injected page would simply not flag it.

Sensitivity is judged in two places, because they mean different things:

  * the **field** being acted on ("#card", a value that looks like a password)
    — acting on it IS the sensitive act, so it goes straight to CRITICAL;
  * the **page** it sits on (title "Checkout", a /login URL) — context, which
    raises submitting and clicking but not typing into an unrelated box.

Without that split, typing into a search box on a page titled "Sign in" would
demand the same confirmation as typing a password, and a confirmation prompt
that cries wolf is one people click through.
"""

from __future__ import annotations

import re
from typing import Any

from jarvis.browser.session import (
    BrowserSession,
    NavigationRefused,
    check_url,
)
from jarvis.governance.risk import Risk
from jarvis.governance.scopes import Scope
from jarvis.tools.base import Preview, Tool, ToolError, ToolInputInvalid, ToolResult, ToolSpec
from jarvis.util.logging import get_logger

log = get_logger(__name__)

OPERATIONS = ("open", "read", "links", "click", "fill", "submit", "current")

#: Operations that only look at the page.
READ_ONLY = ("read", "links", "current")

#: What makes a field or a page sensitive. Matched against the selector and the
#: typed value (field) and against the page title and URL (page).
SENSITIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(password|passwd|passphrase|pwd)\b", re.I), "a password"),
    (
        re.compile(
            r"\b(log ?in|log ?on|sign ?in|sign ?on|credentials?|username|user ?name)\b", re.I
        ),
        "sign-in credentials",
    ),
    (
        re.compile(r"\b(otp|2fa|two.factor|mfa|verification code|security code)\b", re.I),
        "a one-time code",
    ),
    (
        re.compile(r"\b(card|cvv|cvc|credit|debit|payment|billing|expiry)\b", re.I),
        "payment details",
    ),
    (
        re.compile(r"\b(checkout|purchase|buy now|place order|pay now|subscribe)\b", re.I),
        "a purchase",
    ),
    (
        re.compile(
            r"\b(transfer|withdraw|send money|iban|sort code|account number|routing)\b", re.I
        ),
        "a money transfer",
    ),
    (
        re.compile(
            r"\b(delete account|close account|deactivate|cancel subscription|unsubscribe)\b", re.I
        ),
        "closing or cancelling an account",
    ),
    (
        re.compile(
            r"\b(security question|recovery (code|email|phone)|change (email|password))\b", re.I
        ),
        "account security settings",
    ),
    (
        re.compile(r"\b(ssn|social security|passport|national insurance|tax id)\b", re.I),
        "government identity details",
    ),
)


#: A camelCase boundary, where a selector hides a word break.
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_PUNCT = re.compile(r"[^A-Za-z0-9]+")


def words(text: str) -> str:
    """Split a selector or URL into words the patterns can match.

    Selectors and paths separate words with punctuation and capitals rather
    than spaces: `#place-order`, `#placeOrder`, `#place_order` and
    `/checkout/place-order` all mean the same thing, and none of them contains
    the string "place order".
    """
    return _PUNCT.sub(" ", _CAMEL.sub(" ", text)).strip()


def sensitivity(*texts: str) -> str:
    """What makes this sensitive, or an empty string if nothing does."""
    haystack = " ".join(words(t) for t in texts if t)
    for pattern, label in SENSITIVE_PATTERNS:
        if pattern.search(haystack):
            return label
    return ""


class BrowserTool(Tool):
    def __init__(self, session: BrowserSession) -> None:
        self.session = session

    @property
    def spec(self) -> ToolSpec:
        available, detail = self.session.availability()
        return ToolSpec(
            name="browser",
            description=(
                "Open a web page, read it, follow links and fill in forms. Uses "
                "its own browser profile, never your signed-in one."
            ),
            scopes=[Scope.BROWSER_USE],
            risk=Risk.LOW,
            input_schema={
                "type": "object",
                "required": ["operation"],
                "properties": {
                    "operation": {"type": "string", "enum": list(OPERATIONS)},
                    "url": {"type": "string"},
                    "selector": {"type": "string", "description": "CSS selector"},
                    "value": {"type": "string", "description": "Text to type"},
                },
            },
            available=available,
            unavailable_reason="" if available else detail,
        )

    def risk_for(self, args: dict[str, Any], preview: Preview) -> Risk:
        operation = str(args.get("operation", ""))
        # Read from the preview's structured finding, never from its prose:
        # `blast_radius` is written for a person and will be reworded.
        where = str(preview.metadata.get("sensitiveWhere", ""))

        if operation in READ_ONLY:
            return Risk.SAFE
        if operation == "open":
            return Risk.LOW
        if operation == "fill":
            # Typing sends nothing, so page context alone does not escalate —
            # but typing INTO a password or card field is the sensitive act.
            return Risk.CRITICAL if where == "field" else Risk.MEDIUM
        if operation == "click":
            if where == "field":
                return Risk.CRITICAL  # "Place order" is the purchase
            return Risk.HIGH if where == "page" else Risk.MEDIUM
        if operation == "submit":
            return Risk.CRITICAL if where else Risk.HIGH
        return Risk.MEDIUM

    def scopes_for(self, args: dict[str, Any]) -> list[Scope]:
        del args
        return [Scope.BROWSER_USE]

    async def observe(self, args: dict[str, Any]) -> dict[str, Any]:
        """Where the browser is. Recorded so the audit log shows the movement."""
        del args
        return {"url": await self.session.current_url()}

    async def preview(self, args: dict[str, Any]) -> Preview:
        operation = str(args.get("operation", ""))
        if operation not in OPERATIONS:
            raise ToolInputInvalid(
                f"Unknown operation {operation!r}. Supported: {', '.join(OPERATIONS)}."
            )

        available, detail = self.session.availability()
        if not available:
            return Preview(
                summary="Browser automation is unavailable",
                blocked=detail,
                reversible="undoable",
                blast_radius="Nothing changes.",
            )

        if operation in READ_ONLY:
            return Preview(
                summary=f"{operation} the current page — reading only",
                affected=0,
                reversible="undoable",
                blast_radius="Nothing changes. The page is read, not altered.",
            )

        if operation == "open":
            self.require(args, "url")
            try:
                url = check_url(str(args["url"]), self.session.settings)
            except NavigationRefused as exc:
                # A refused address is a blocked preview, not an exception: the
                # user gets the reason in the normal flow instead of an error.
                return Preview(
                    summary="Address refused",
                    blocked=exc.message,
                    reversible="undoable",
                    blast_radius="Nothing changes.",
                )
            return Preview(
                summary=f"Open {url}",
                targets=[url],
                affected=1,
                reversible="undoable",
                blast_radius=(
                    f"Loads {url} in Jarvis's own browser. Your signed-in browser "
                    f"is not used, so no site sees you as logged in."
                ),
            )

        # click / fill / submit act on a live page.
        self.require(args, "selector")
        selector = str(args["selector"])
        value = str(args.get("value") or "")
        current = await self.session.current_url()
        if not current:
            return Preview(
                summary=f"{operation} {selector}",
                blocked="No page is open yet. Open a page before interacting with it.",
                reversible="undoable",
                blast_radius="Nothing changes.",
            )

        title = ""
        try:
            title = (await self.session.snapshot()).title
        except Exception:  # a title is nice to have, never required
            title = ""

        # The value is checked as well as the selector: a password typed into a
        # field named "q" is still a password.
        field_reason = sensitivity(selector, value if operation == "fill" else "")
        page_reason = sensitivity(title, current)
        reason = field_reason or page_reason
        where = "field" if field_reason else ("page" if page_reason else "")
        metadata = {"sensitive": reason, "sensitiveWhere": where, "url": current}
        page_name = title or current

        if operation == "submit":
            return Preview(
                summary=f"Submit {selector} on {page_name}",
                targets=[selector],
                affected=1,
                reversible="permanent",
                metadata=metadata,
                blast_radius=(
                    f"This form involves {reason}. Submitting it acts in the real "
                    f"world — money, access or account state can change, and "
                    f"Jarvis cannot undo it."
                    if reason
                    else f"Submits the form on {page_name}. Whatever the site does "
                    f"with it cannot be undone by Jarvis."
                ),
            )

        if operation == "fill":
            return Preview(
                summary=f"Type into {selector} on {page_name}",
                targets=[selector],
                affected=1,
                reversible="undoable",
                metadata=metadata,
                blast_radius=(
                    f"Types what looks like {reason} into {selector}. Nothing "
                    f"leaves the browser until a form is submitted, and the text "
                    f"is not stored."
                    if field_reason
                    else f"Types text into {selector}. Nothing is sent until a form is submitted."
                ),
            )

        return Preview(
            summary=f"Click {selector} on {page_name}",
            targets=[selector],
            affected=1,
            reversible="unknown",
            metadata=metadata,
            blast_radius=(
                f"This control appears to be for {reason}, so clicking it may be "
                f"the action itself rather than a step towards it."
                if reason
                else f"Clicks {selector}. What the site does next is up to the site."
            ),
        )

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        operation = str(args.get("operation", ""))

        if operation == "open":
            self.require(args, "url")
            snapshot = await self.session.goto(str(args["url"]))
            log.info("page opened", url=snapshot.url, title=snapshot.title[:80])
            return ToolResult(
                ok=True,
                summary=f"Opened {snapshot.title or snapshot.url}",
                data={**snapshot.to_dict(), "controlLayer": "L1"},
                changes=[f"Opened {snapshot.url}"],
            )

        if operation in ("read", "links"):
            snapshot = await self.session.snapshot()
            if operation == "links":
                return ToolResult(
                    ok=True,
                    summary=f"{len(snapshot.links)} links on {snapshot.title or snapshot.url}",
                    data={"url": snapshot.url, "links": snapshot.links},
                )
            return ToolResult(
                ok=True,
                summary=(
                    f"Read {snapshot.title or snapshot.url} "
                    f"({len(snapshot.text)} characters"
                    + (", truncated" if snapshot.truncated else "")
                    + ")"
                ),
                data=snapshot.to_dict(),
            )

        if operation == "current":
            url = await self.session.current_url()
            return ToolResult(
                ok=True,
                summary=url or "No page is open",
                data={"url": url},
            )

        if operation == "click":
            self.require(args, "selector")
            url = await self.session.click(str(args["selector"]))
            return ToolResult(
                ok=True,
                summary=f"Clicked {args['selector']}; now at {url}",
                data={"url": url},
                changes=[f"Clicked {args['selector']}"],
            )

        if operation == "fill":
            self.require(args, "selector", "value")
            await self.session.fill(str(args["selector"]), str(args["value"]))
            # The value is deliberately absent from the summary, the data and
            # the log: it may be a password, and this record is kept on disk.
            return ToolResult(
                ok=True,
                summary=f"Typed into {args['selector']}",
                data={"selector": args["selector"]},
                changes=[f"Filled {args['selector']}"],
            )

        if operation == "submit":
            self.require(args, "selector")
            url = await self.session.click(str(args["selector"]))
            log.warning("form submitted", selector=args["selector"], url=url)
            return ToolResult(
                ok=True,
                summary=f"Submitted; now at {url}",
                data={"url": url},
                changes=[f"Submitted {args['selector']}"],
            )

        raise ToolError(f"Unsupported operation {operation!r}.")

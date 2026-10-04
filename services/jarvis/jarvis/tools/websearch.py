"""WebSearchTool — ask a search engine, read the results.

Two decisions worth stating.

**Engines are a fixed set, not a URL template from configuration.** A free-form
"search URL" setting would be the perfect thing for a prompt-injected page to
talk the user into changing: every future search would then go to an attacker's
site, carrying whatever the user asked about. The engine is therefore chosen by
name from this module.

**The query leaves this computer.** That is the whole point of a web search, but
it is still a privacy event, so the preview says which engine will see the words
before anything is sent. This tool is the one place in Jarvis where the user's
own phrasing reaches a third party without a model being involved.

Results are scraped from the rendered page. Search engines change their markup,
so `results` comes back empty rather than wrong when the selectors stop
matching, and the tool says the layout changed instead of claiming no results.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

from jarvis.browser.session import BrowserSession, NavigationRefused, PageSnapshot
from jarvis.governance.risk import Risk
from jarvis.governance.scopes import Scope
from jarvis.tools.base import Preview, Tool, ToolError, ToolInputInvalid, ToolResult, ToolSpec
from jarvis.util.logging import get_logger

log = get_logger(__name__)

MAX_RESULTS = 10
MAX_QUERY_CHARS = 400


@dataclass(frozen=True)
class SearchEngine:
    name: str
    host: str
    #: `{q}` is replaced with the percent-encoded query.
    template: str
    #: CSS for one result, and for the link and snippet inside it.
    result_selector: str
    link_selector: str
    snippet_selector: str
    #: Some engines wrap outbound links in a redirect; this names the query
    #: parameter holding the real destination.
    redirect_param: str = ""

    def url_for(self, query: str) -> str:
        return self.template.replace("{q}", quote_plus(query))


#: The HTML endpoints, deliberately: they work without JavaScript, are far
#: cheaper to render, and carry no personalisation.
ENGINES: dict[str, SearchEngine] = {
    "duckduckgo": SearchEngine(
        name="duckduckgo",
        host="duckduckgo.com",
        template="https://duckduckgo.com/html/?q={q}",
        result_selector="div.result, article[data-testid='result']",
        link_selector="a.result__a, h2 a",
        snippet_selector="a.result__snippet, div.result__snippet, [data-result='snippet']",
        redirect_param="uddg",
    ),
    "bing": SearchEngine(
        name="bing",
        host="www.bing.com",
        template="https://www.bing.com/search?q={q}",
        result_selector="li.b_algo",
        link_selector="h2 a",
        snippet_selector="div.b_caption p, p.b_algoSlug",
    ),
    "startpage": SearchEngine(
        name="startpage",
        host="www.startpage.com",
        template="https://www.startpage.com/sp/search?query={q}",
        result_selector="div.w-gl__result, div.result",
        link_selector="a.w-gl__result-title, a.result-link, h3 a",
        snippet_selector="p.w-gl__description, p.description",
    ),
}

DEFAULT_ENGINE = "duckduckgo"


def unwrap(href: str, param: str) -> str:
    """Pull the real destination out of a search engine's redirect link."""
    if not param or not href:
        return href
    try:
        query = parse_qs(urlparse(href).query)
    except ValueError:
        return href
    target = query.get(param, [""])[0]
    return target or href


def parse_results(snapshot: PageSnapshot, engine: SearchEngine) -> list[dict[str, str]]:
    """Fallback parser: use the page's links when scraping found no results.

    Better a list of real links with a note saying the layout changed than a
    confident "no results found" for a query that had plenty.
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for link in snapshot.links:
        href = unwrap(link.get("href", ""), engine.redirect_param)
        host = (urlparse(href).hostname or "").lower()
        if not href.startswith("http") or host.endswith(engine.host) or href in seen:
            continue
        seen.add(href)
        out.append({"title": link.get("text", "")[:200], "url": href, "snippet": ""})
        if len(out) >= MAX_RESULTS:
            break
    return out


class WebSearchTool(Tool):
    def __init__(self, session: BrowserSession, engine: str = DEFAULT_ENGINE) -> None:
        self.session = session
        self.engine_name = engine if engine in ENGINES else DEFAULT_ENGINE

    @property
    def engine(self) -> SearchEngine:
        return ENGINES[self.engine_name]

    @property
    def spec(self) -> ToolSpec:
        available, detail = self.session.availability()
        return ToolSpec(
            name="websearch",
            description=(
                "Search the web and return result titles, links and snippets. "
                f"Uses {self.engine.name}; the query text is sent to it."
            ),
            scopes=[Scope.BROWSER_USE],
            risk=Risk.LOW,
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "maxLength": MAX_QUERY_CHARS},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS},
                },
            },
            available=available,
            unavailable_reason="" if available else detail,
        )

    def _allowed(self) -> str:
        """Why the engine cannot be reached, or an empty string if it can."""
        settings = self.session.settings
        host = self.engine.host
        if settings.allow_any_host:
            return ""
        if any(host == a or host.endswith(f".{a}") for a in settings.allowed_hosts):
            return ""
        return (
            f"{host} is not in the list of sites Jarvis may visit, so searching "
            f"with {self.engine.name} would be refused. Add it in Privacy "
            f"settings, or pick an engine that is already allowed."
        )

    async def preview(self, args: dict[str, Any]) -> Preview:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolInputInvalid("No search query was given.")
        if len(query) > MAX_QUERY_CHARS:
            raise ToolInputInvalid(
                f"That query is {len(query)} characters; the limit is {MAX_QUERY_CHARS}."
            )

        available, detail = self.session.availability()
        if not available:
            return Preview(
                summary="Web search is unavailable",
                blocked=detail,
                reversible="undoable",
                blast_radius="Nothing changes and nothing is sent.",
            )
        if blocked := self._allowed():
            return Preview(
                summary="Web search is not permitted yet",
                blocked=blocked,
                reversible="undoable",
                blast_radius="Nothing changes and nothing is sent.",
            )

        return Preview(
            summary=f"Search {self.engine.name} for {query!r}",
            targets=[self.engine.host],
            affected=1,
            reversible="undoable",
            metadata={"engine": self.engine.name, "query_chars": len(query)},
            blast_radius=(
                f"Sends these {len(query)} characters to {self.engine.host} and "
                f"reads the results page. Nothing on this computer changes, but "
                f"the search engine sees what you asked."
            ),
        )

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolInputInvalid("No search query was given.")
        limit = max(1, min(int(args.get("limit") or MAX_RESULTS), MAX_RESULTS))
        engine = self.engine

        try:
            snapshot = await self.session.goto(engine.url_for(query))
        except NavigationRefused as exc:
            raise ToolError(f"Could not search: {exc.message}", engine=engine.name) from exc

        results = await self._scrape(engine, limit)
        degraded = False
        if not results:
            # Either there genuinely were no hits, or the markup moved. Links on
            # the page tell the two apart.
            results = parse_results(snapshot, engine)[:limit]
            degraded = bool(results)

        log.info("web search", engine=engine.name, results=len(results), degraded=degraded)
        return ToolResult(
            ok=True,
            summary=(
                f"{len(results)} result(s) from {engine.name} for {query!r}"
                + (
                    " — read from the page's links because the result layout has "
                    "changed, so titles may be rough"
                    if degraded
                    else ""
                )
                if results
                else f"{engine.name} returned nothing for {query!r}"
            ),
            data={
                "engine": engine.name,
                "query": query,
                "results": results,
                "resultsUrl": snapshot.url,
                "layoutChanged": degraded,
            },
        )

    async def _scrape(self, engine: SearchEngine, limit: int) -> list[dict[str, str]]:
        """Read the result blocks out of the rendered page."""
        script = """
        ([resultSel, linkSel, snippetSel, limit]) => {
          const out = [];
          for (const block of document.querySelectorAll(resultSel)) {
            const a = block.querySelector(linkSel);
            if (!a || !a.href) continue;
            const s = block.querySelector(snippetSel);
            out.push({
              title: (a.innerText || a.textContent || '').trim().slice(0, 300),
              url: a.href,
              snippet: (s ? (s.innerText || s.textContent || '') : '').trim().slice(0, 600),
            });
            if (out.length >= limit) break;
          }
          return out;
        }
        """
        raw = await self.session.evaluate(
            script,
            [engine.result_selector, engine.link_selector, engine.snippet_selector, limit],
        )
        results: list[dict[str, str]] = []
        for item in raw or []:
            url = unwrap(str(item.get("url", "")), engine.redirect_param)
            if not url.startswith("http"):
                continue
            results.append(
                {
                    "title": str(item.get("title", "")),
                    "url": url,
                    "snippet": str(item.get("snippet", "")),
                }
            )
        return results

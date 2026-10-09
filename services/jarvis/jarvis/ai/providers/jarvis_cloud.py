"""A relay: ChatGPT, Claude and Gemini without a key on this machine.

The other cloud providers here talk to the vendor directly with a key you
own. This one posts the conversation to the Jarvis web app's `/api/chat`,
which holds the keys and forwards to whichever model the `model` field names.
That is what "no API key needed" means, and it is worth being exact about,
because the trade is not free:

* **The relay sees every prompt.** The vendor would too, but the relay is an
  extra party, and one whose address is a plain string in config.toml. It is
  `is_cloud = True`, so the privacy gate treats it like any other cloud
  provider and `[ai] allow_cloud` has to be on before a single turn leaves.
* **The key is the operator's, so the quota is too.** Rate limits and spend
  land on whoever published the web app, not on the person typing.
* **The model list is the relay's**, not a vendor's. It routes on strings like
  `openai/gpt-6-astra`, so a name that works against OpenAI directly need not
  work here, and vice versa. `health()` asks rather than assuming.

Use the `anthropic`, `openai_compat` or `google` providers instead for a key
you hold and a conversation that reaches the vendor and nobody else.

Streaming is plain text, not SSE: the endpoint writes the reply as it arrives
with no framing, so there is nothing to parse and no usage to read back.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

from jarvis.ai.base import Provider
from jarvis.ai.providers import _http
from jarvis.ai.types import Capabilities, Chunk, CompletionRequest, Usage
from jarvis.config.settings import ProviderSettings
from jarvis.util.errors import ProviderError

#: The published web app. Overridable per provider via `base_url`, because a
#: relay address is deployment configuration, not a property of the code.
DEFAULT_BASE_URL = "https://project--580b3858-eb12-48a6-9bbc-fe03df4d971a.lovable.app"

#: A starting point, and the relay's own naming rather than a vendor's.
DEFAULT_MODEL = "openai/gpt-6-astra"

#: Turns kept when a conversation is longer than this. The relay has no
#: documented context limit, so this is a cap on what one request can carry,
#: not an attempt to fit a particular model's window.
MAX_TURNS = 60


class JarvisCloudProvider(Provider):
    kind = "jarvis_cloud"
    is_cloud = True

    def __init__(self, name: str, settings: ProviderSettings) -> None:
        super().__init__(name, settings)
        self._client: httpx.AsyncClient | None = None

    @property
    def base_url(self) -> str:
        return (self.settings.base_url or DEFAULT_BASE_URL).rstrip("/")

    @property
    def model(self) -> str:
        return self.settings.model or DEFAULT_MODEL

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = _http.client(
                self.base_url,
                self.settings.timeout_seconds,
                {"content-type": "application/json", "accept": "text/plain"},
            )
        return self._client

    def _payload(self, request: CompletionRequest) -> dict[str, object]:
        # The endpoint carries its own system prompt and takes no field for
        # ours, so fold ours into the first user turn. Dropping it would lose
        # the planner and critic instructions, which is the whole of their
        # behaviour, and would read as a model that ignores its brief.
        messages = [{"role": m.role, "content": m.content} for m in request.without_system()]
        system = request.system_prompt()
        if system and messages:
            messages[0] = {**messages[0], "content": f"{system}\n\n{messages[0]['content']}"}
        if not messages:
            messages = [{"role": "user", "content": system or ""}]
        return {"model": self.model, "messages": messages[-MAX_TURNS:]}

    def _reject_non_api_response(self, response: httpx.Response) -> None:
        """Fail loudly when the reply is the web page rather than the endpoint.

        A single-page app serves its HTML shell for any path it does not
        recognise, with status 200. So a web app published without the
        `/api/chat` function answers this request successfully, and streaming
        its markup back as the assistant's words is the worst of the available
        failures. The content type is what separates the two.
        """
        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type.lower():
            raise ProviderError(
                f"{self.base_url} answered /api/chat with a web page, not a reply. "
                "The web app is published but its chat endpoint is not deployed.",
                status=response.status_code,
            )

    def _reject_redirect(self, response: httpx.Response) -> None:
        """A redirect is not followed, and must not read as an empty reply.

        `_http.client` sets `follow_redirects=False` deliberately — a redirect
        can move a request carrying the conversation to another host. But a 3xx
        is not >= 400, so without this it falls through the status check and
        yields no text at all: a silent empty answer.
        """
        if 300 <= response.status_code < 400:
            location = response.headers.get("location", "(no Location header)")
            raise ProviderError(
                f"{self.base_url} redirected /api/chat to {location}, which is not followed "
                "automatically because it would send the conversation elsewhere. "
                "Set base_url to the address it names if that is where the app now lives.",
                status=response.status_code,
            )

    def _refusal(self, status: int) -> str:
        """What a 401 or 403 means when there is no key to be wrong.

        `_http.raise_for_status` reads these as bad credentials and says to
        check the API key in Settings. For every other provider that is the
        right advice; here there is no key field to check, and the actual cause
        is at the other end — an unpublished or private deployment refuses the
        request before it reaches any model. Sending someone to look for a key
        that does not exist is worse than no message at all.
        """
        return (
            f"{self.base_url} refused the request ({status}). The Jarvis web app has to be "
            "published and its /api/chat endpoint publicly reachable; a private or "
            "unpublished deployment answers exactly this way. There is no API key to fix "
            "here — either publish that web app, point base_url at one that is, or use a "
            "provider with a key of your own."
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[Chunk]:
        client = self._ensure_client()
        try:
            async with client.stream("POST", "/api/chat", json=self._payload(request)) as resp:
                self._reject_redirect(resp)
                if resp.status_code in (401, 403):
                    raise ProviderError(self._refusal(resp.status_code), status=resp.status_code)
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    _http.raise_for_status(resp, self.name, body)
                self._reject_non_api_response(resp)

                async for text in resp.aiter_text():
                    if text:
                        yield Chunk(text=text)
        except httpx.HTTPError as exc:
            raise _http.wrap_transport_error(exc, self.name, self.base_url) from exc

        # Usage is left at zero rather than estimated from the reply's length:
        # it is written to the `tokens_out` column beside counts the vendor
        # providers report from the wire, and a guess there is indistinguishable
        # from a measurement. The relay reports none, so none is what is known.
        yield Chunk(done=True, usage=Usage(), stop_reason="stop")

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name=self.name,
            kind=self.kind,
            model=self.model,
            streaming=True,
            tools=False,
            # `self.is_cloud`, not a literal: the privacy gate reads the class
            # attribute and the panel reads this field, and a relay that looked
            # local in one of the two places would be the worst kind of wrong.
            is_cloud=self.is_cloud,
            # Nothing to configure: there is no key to store and the address has
            # a default. Whether the relay answers is a question for `health()`.
            configured=True,
            detail=(
                f"No API key needed — {self.base_url} holds one and relays to {self.model}. "
                "Press Test to check it answers."
            ),
        )

    async def health(self) -> tuple[bool, str]:
        """Ask `/api/chat` itself, with the smallest real request.

        Probing `/` instead would pass for any published web app, including one
        with no chat endpoint at all, which is the failure most worth catching.
        """
        payload = {"model": self.model, "messages": [{"role": "user", "content": "ping"}]}
        try:
            client = self._ensure_client()
            async with client.stream("POST", "/api/chat", json=payload, timeout=20.0) as resp:
                if 300 <= resp.status_code < 400:
                    where = resp.headers.get("location", "elsewhere")
                    return False, f"{self.base_url} redirects /api/chat to {where}."
                if resp.status_code in (401, 403):
                    return False, self._refusal(resp.status_code)
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace").strip()[:200]
                    return False, (
                        f"{self.base_url} rejected the request ({resp.status_code}). "
                        f"{body or 'No detail was returned.'}"
                    )
                if "text/html" in resp.headers.get("content-type", "").lower():
                    return False, (
                        f"{self.base_url} is published, but /api/chat returns the web page "
                        "instead of a reply — the chat endpoint is not deployed."
                    )
                # At least one byte, not merely a 200: a function that is
                # deployed but broken answers with an empty body, and treating
                # that as healthy would make this check unable to fail for the
                # state it exists to detect. The first chunk is enough — the
                # rest is a reply someone is paying for.
                answered = False
                async for piece in resp.aiter_bytes():
                    if piece:
                        answered = True
                        break
                if not answered:
                    return False, (
                        f"{self.base_url} accepted the request but sent nothing back. "
                        "The chat endpoint is reachable and not returning a reply."
                    )
        except httpx.HTTPError as exc:
            return False, f"Could not reach {self.base_url}: {exc}"
        return True, f"{self.base_url} answered for {self.model}."

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

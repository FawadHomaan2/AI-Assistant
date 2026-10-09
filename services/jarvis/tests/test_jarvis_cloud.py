"""The keyless relay provider, against mocked HTTP.

Two things make this adapter worth its own file rather than a few cases in
`test_providers.py`.

The first is that it is the only cloud provider with nothing to configure, so
it is the only one that could reach the network on a fresh install with no key
and no deliberate act. What is pinned here is that it cannot: the privacy gate
has to stop it exactly as it stops the vendor providers.

The second is that its endpoint lives behind a single-page app, which answers
*every* path with its HTML shell and a 200. A web app published without its
chat function is therefore a success as far as HTTP is concerned, and the
markup would be streamed back as the assistant's reply. A plain `GET /` health
check — which is what this adapter arrived with — passes in that state, so it
is a check that cannot fail.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from jarvis.ai import privacy
from jarvis.ai.gateway import REGISTRY, Gateway, build
from jarvis.ai.providers.jarvis_cloud import DEFAULT_BASE_URL, MAX_TURNS, JarvisCloudProvider
from jarvis.ai.types import CompletionRequest, JobClass, Message, PrivacyClass
from jarvis.config.settings import AISettings, ProviderSettings, Settings
from jarvis.util.errors import PrivacyViolation, ProviderError, ProviderUnavailable

BASE = "https://relay.test"
CHAT = f"{BASE}/api/chat"


def _provider(**over: object) -> JarvisCloudProvider:
    settings = ProviderSettings(
        kind="jarvis_cloud", model="openai/gpt-6-astra", base_url=BASE, **over
    )
    return JarvisCloudProvider("jarvis", settings)


def _req(text: str = "hi", system: str | None = "be brief") -> CompletionRequest:
    messages = [Message("user", text)]
    if system is not None:
        messages.insert(0, Message("system", system))
    return CompletionRequest(messages=messages, job=JobClass.CHAT)


async def _collect(provider: JarvisCloudProvider, request: CompletionRequest | None = None):
    text, final = "", None
    async for chunk in provider.stream(request or _req()):
        text += chunk.text
        if chunk.done:
            final = chunk
    return text, final


def _text(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/plain; charset=utf-8"})


def _page() -> httpx.Response:
    """What a single-page app returns for a path it does not serve."""
    return httpx.Response(
        200,
        text="<!doctype html><html><body><div id='root'></div></body></html>",
        headers={"content-type": "text/html; charset=utf-8"},
    )


class TestItIsWiredIn:
    def test_the_kind_builds(self) -> None:
        assert REGISTRY["jarvis_cloud"] is JarvisCloudProvider
        built = build("jarvis", ProviderSettings(kind="jarvis_cloud"))
        assert isinstance(built, JarvisCloudProvider)

    def test_it_declares_itself_cloud(self) -> None:
        # The whole of its privacy handling rests on this one attribute.
        assert JarvisCloudProvider.is_cloud is True
        assert _provider().capabilities().is_cloud is True

    def test_the_default_address_is_the_published_app(self) -> None:
        settings = ProviderSettings(kind="jarvis_cloud")
        assert JarvisCloudProvider("jarvis", settings).base_url == DEFAULT_BASE_URL

    def test_the_address_can_be_pointed_elsewhere(self) -> None:
        """A relay address is deployment configuration, not a constant."""
        assert _provider().base_url == BASE


class TestNothingLeavesWithoutPermission:
    """The gate, not the adapter — but this is the provider that needs it most."""

    def test_a_turn_is_refused_while_cloud_ai_is_off(self) -> None:
        with pytest.raises(PrivacyViolation):
            privacy.check(
                _req(),
                provider_name="jarvis",
                is_cloud=JarvisCloudProvider.is_cloud,
                allow_cloud=False,
                allow_cloud_content=False,
            )

    def test_sensitive_requests_are_refused_even_with_cloud_on(self) -> None:
        request = _req()
        request.privacy = PrivacyClass.SENSITIVE
        with pytest.raises(PrivacyViolation):
            privacy.check(
                request,
                provider_name="jarvis",
                is_cloud=True,
                allow_cloud=True,
                allow_cloud_content=True,
            )

    @respx.mock
    async def test_the_gateway_sends_nothing_on_a_default_install(self) -> None:
        """No key to add, so the switch is the only thing standing in the way.

        `allow_cloud` is False by default; if the relay ever slipped past it,
        a fresh install would start talking to a third party unprompted.
        """
        route = respx.post(CHAT).mock(return_value=_text("hello"))
        settings = Settings(
            ai=AISettings(
                default="jarvis",
                providers={
                    "jarvis": ProviderSettings(kind="jarvis_cloud", base_url=BASE),
                },
            )
        )
        assert settings.ai.allow_cloud is False

        gateway = Gateway(settings)
        with pytest.raises(PrivacyViolation):
            async for _ in gateway.stream(_req()):
                pass
        assert not route.called, "the request must not be made at all, not merely discarded"
        await gateway.aclose()


class TestStreaming:
    @respx.mock
    async def test_plain_text_is_passed_straight_through(self) -> None:
        respx.post(CHAT).mock(return_value=_text("Hello there."))
        text, final = await _collect(_provider())
        assert text == "Hello there."
        assert final is not None
        assert final.stop_reason == "stop"

    @respx.mock
    async def test_the_system_prompt_is_folded_into_the_first_turn(self) -> None:
        """The endpoint takes no system field, and dropping ours is not an option.

        A planner whose instructions were silently discarded reads as a model
        that ignores its brief, which is a far harder bug to find than a
        rejected request.
        """
        route = respx.post(CHAT).mock(return_value=_text("ok"))
        await _collect(_provider(), _req("plan this", system="You are the planner."))

        sent = route.calls[0].request
        body = httpx.Response(200, content=sent.content).json()
        assert all(m["role"] != "system" for m in body["messages"])
        assert body["messages"][0]["content"] == "You are the planner.\n\nplan this"
        assert body["model"] == "openai/gpt-6-astra"

    @respx.mock
    async def test_a_system_only_request_still_carries_the_prompt(self) -> None:
        route = respx.post(CHAT).mock(return_value=_text("ok"))
        request = CompletionRequest(messages=[Message("system", "only this")], job=JobClass.CHAT)
        await _collect(_provider(), request)

        body = httpx.Response(200, content=route.calls[0].request.content).json()
        assert body["messages"] == [{"role": "user", "content": "only this"}]

    @respx.mock
    async def test_a_long_conversation_is_capped(self) -> None:
        route = respx.post(CHAT).mock(return_value=_text("ok"))
        messages = [Message("user", f"turn {i}") for i in range(MAX_TURNS + 20)]
        await _collect(_provider(), CompletionRequest(messages=messages, job=JobClass.CHAT))

        body = httpx.Response(200, content=route.calls[0].request.content).json()
        assert len(body["messages"]) == MAX_TURNS
        # The newest turns, not the oldest: the opposite would answer the start
        # of the conversation and ignore the question just asked.
        assert body["messages"][-1]["content"] == f"turn {MAX_TURNS + 19}"

    @respx.mock
    async def test_usage_is_reported_as_unknown_rather_than_estimated(self) -> None:
        """A guess in `tokens_out` is indistinguishable from a measurement.

        It is written to the same database column the vendor providers fill
        from the wire, so dividing the reply's length by four would put a
        fabricated number beside real ones.
        """
        respx.post(CHAT).mock(return_value=_text("a fairly long reply, several words of it"))
        _text_out, final = await _collect(_provider())
        assert final is not None
        assert final.usage is not None
        assert final.usage.tokens_out == 0
        assert final.usage.tokens_in == 0


class TestFailuresAreExplained:
    @respx.mock
    async def test_the_web_page_is_not_streamed_back_as_a_reply(self) -> None:
        """The failure a published-but-incomplete deployment actually produces.

        The app answers 200 with its HTML shell. Without the content-type
        check, that markup is the assistant's answer.
        """
        respx.post(CHAT).mock(return_value=_page())
        with pytest.raises(ProviderError) as caught:
            await _collect(_provider())
        assert "not deployed" in str(caught.value)
        assert "<html" not in str(caught.value)

    @respx.mock
    async def test_a_redirect_is_not_a_silent_empty_answer(self) -> None:
        """3xx is not >= 400, so the status check alone lets it through.

        Redirects are deliberately not followed — one could move a request
        carrying the conversation to another host — so an unhandled 3xx ends
        the stream with no text and no error.
        """
        respx.post(CHAT).mock(
            return_value=httpx.Response(307, headers={"location": "https://elsewhere.test/api"})
        )
        with pytest.raises(ProviderError) as caught:
            await _collect(_provider())
        assert "elsewhere.test" in str(caught.value)

    @pytest.mark.parametrize("status", [401, 403])
    @respx.mock
    async def test_a_refusal_does_not_send_anyone_looking_for_a_key(self, status: int) -> None:
        """An unpublished deployment answers 403, and there is no key to check.

        `_http.raise_for_status` reads 401 and 403 as bad credentials and says
        to check the API key in Settings. That is right for every other
        provider and actively misleading here: this adapter has no key field,
        so the advice sends someone looking for something that does not exist
        while the real cause — a private or unpublished web app — goes unsaid.
        """
        respx.post(CHAT).mock(return_value=httpx.Response(status, text="Forbidden"))
        with pytest.raises(ProviderError) as caught:
            await _collect(_provider())

        message = str(caught.value)
        assert "published" in message
        assert "no API key to fix here" in message
        assert "Check the API key in Settings" not in message

    @pytest.mark.parametrize("status", [401, 403])
    @respx.mock
    async def test_health_explains_a_refusal_the_same_way(self, status: int) -> None:
        respx.post(CHAT).mock(return_value=httpx.Response(status, text="Forbidden"))
        ok, detail = await _provider().health()
        assert not ok
        assert "published" in detail
        assert "no API key to fix here" in detail

    @respx.mock
    async def test_a_server_error_is_typed_as_unavailable(self) -> None:
        """Routed through `_http.raise_for_status` like every other provider.

        A bare ProviderError for a 500 or a 429 loses the distinction the
        orchestrator uses to decide whether retrying is worth anything.
        """
        respx.post(CHAT).mock(return_value=httpx.Response(503, text="overloaded"))
        with pytest.raises(ProviderUnavailable):
            await _collect(_provider())

    @respx.mock
    async def test_a_rate_limit_is_typed_as_unavailable(self) -> None:
        # The relay's quota belongs to whoever published it, so this is the
        # failure a shared deployment hits first.
        respx.post(CHAT).mock(return_value=httpx.Response(429, text="slow down"))
        with pytest.raises(ProviderUnavailable):
            await _collect(_provider())

    @respx.mock
    async def test_a_missing_endpoint_names_the_likely_cause(self) -> None:
        respx.post(CHAT).mock(return_value=httpx.Response(404, text="not found"))
        with pytest.raises(ProviderError) as caught:
            await _collect(_provider())
        assert "404" in str(caught.value)

    @respx.mock
    async def test_an_unreachable_host_says_so(self) -> None:
        respx.post(CHAT).mock(side_effect=httpx.ConnectError("no route"))
        with pytest.raises(ProviderUnavailable) as caught:
            await _collect(_provider())
        assert BASE in str(caught.value)


class TestHealth:
    @respx.mock
    async def test_a_working_relay_passes(self) -> None:
        respx.post(CHAT).mock(return_value=_text("pong"))
        ok, detail = await _provider().health()
        assert ok
        assert "openai/gpt-6-astra" in detail

    @respx.mock
    async def test_a_published_app_without_the_endpoint_fails(self) -> None:
        """The case a `GET /` probe reports as healthy.

        This is the whole reason the check posts to `/api/chat`: the root of a
        single-page app answers 200 whether or not the chat function exists.
        """
        respx.get(f"{BASE}/").mock(return_value=_page())
        respx.post(CHAT).mock(return_value=_page())
        ok, detail = await _provider().health()
        assert not ok
        assert "not deployed" in detail

    @respx.mock
    async def test_a_200_with_nothing_in_it_does_not_pass(self) -> None:
        """A function that is deployed but broken answers exactly like this.

        Status and content type are both fine, so a check that stopped at
        those would report the relay healthy while it returns no replies —
        unable to fail for one of the two states it exists to detect.
        """
        respx.post(CHAT).mock(
            return_value=httpx.Response(200, text="", headers={"content-type": "text/plain"})
        )
        ok, detail = await _provider().health()
        assert not ok
        assert "sent nothing back" in detail

    @respx.mock
    async def test_a_rejected_model_is_reported_with_the_reason(self) -> None:
        respx.post(CHAT).mock(
            return_value=httpx.Response(400, text="unknown model: openai/gpt-6-astra")
        )
        ok, detail = await _provider().health()
        assert not ok
        assert "unknown model" in detail

    @respx.mock
    async def test_an_unreachable_relay_is_reported_not_raised(self) -> None:
        respx.post(CHAT).mock(side_effect=httpx.ConnectError("no route"))
        ok, detail = await _provider().health()
        assert not ok
        assert BASE in detail

    @respx.mock
    async def test_a_redirect_is_reported(self) -> None:
        respx.post(CHAT).mock(
            return_value=httpx.Response(308, headers={"location": "https://moved.test/api/chat"})
        )
        ok, detail = await _provider().health()
        assert not ok
        assert "moved.test" in detail


class TestWhatItTellsTheInterface:
    def test_it_reports_itself_usable_with_no_key(self) -> None:
        """There is no key and no credential entry, so nothing is unconfigured."""
        caps = _provider().capabilities()
        assert caps.configured is True
        assert caps.streaming is True

    def test_the_detail_names_the_relay_rather_than_claiming_readiness(self) -> None:
        """It has not contacted anything yet, so it cannot report "Ready".

        The panel shows this string verbatim. Saying the relay is ready before
        a single request would be a claim the provider is in no position to
        make, and the "no key" part is the half that reads as reassuring.
        """
        detail = _provider().capabilities().detail
        assert BASE in detail
        assert "No API key needed" in detail
        assert not detail.startswith("Ready")

    def test_it_does_not_claim_tool_calling(self) -> None:
        # The gateway's tool path does not run through this endpoint.
        assert _provider().capabilities().tools is False

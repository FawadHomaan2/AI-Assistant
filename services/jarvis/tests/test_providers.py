"""Provider adapters, against mocked HTTP.

No live API keys exist in CI, so these pin the request shape and the streaming
parse — which is where provider bugs actually live.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from jarvis.ai.providers.anthropic import AnthropicProvider
from jarvis.ai.providers.dev_echo import BANNER, DevEchoProvider
from jarvis.ai.providers.google import GoogleProvider
from jarvis.ai.providers.ollama import OllamaProvider
from jarvis.ai.providers.openai_compat import OpenAICompatProvider, is_local_url
from jarvis.ai.types import CompletionRequest, JobClass, Message
from jarvis.config.settings import ProviderSettings
from jarvis.util.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderNotConfigured,
    ProviderUnavailable,
)


def _req(text: str = "hi") -> CompletionRequest:
    return CompletionRequest(
        messages=[Message("system", "be brief"), Message("user", text)], job=JobClass.CHAT
    )


async def _collect(provider):
    text, final = "", None
    async for chunk in provider.stream(_req()):
        text += chunk.text
        if chunk.done:
            final = chunk
    return text, final


class TestDevEcho:
    async def test_streams_in_pieces_and_is_labelled(self) -> None:
        p = DevEchoProvider("dev_echo", ProviderSettings(kind="dev_echo"))
        p.delay = 0
        chunks = [c async for c in p.stream(_req("Open Chrome"))]
        assert len(chunks) > 5, "must stream incrementally, not in one blob"
        text = "".join(c.text for c in chunks)
        assert BANNER in text, "must identify itself as not a language model"
        assert chunks[-1].done and chunks[-1].usage is not None

    async def test_not_cloud(self) -> None:
        p = DevEchoProvider("dev_echo", ProviderSettings(kind="dev_echo"))
        assert p.capabilities().is_cloud is False


class TestAnthropic:
    def _provider(self) -> AnthropicProvider:
        return AnthropicProvider(
            "anthropic",
            ProviderSettings(
                kind="anthropic",
                model="claude-sonnet-4-5",
                credential="jarvis/anthropic",
                base_url="https://api.anthropic.test",
            ),
        )

    @respx.mock
    async def test_parses_sse_stream(self, monkeypatch) -> None:
        monkeypatch.setattr("jarvis.config.secrets.get", lambda _n: "test-key")
        body = (
            'event: message_start\ndata: {"type":"message_start","message":'
            '{"usage":{"input_tokens":11}}}\n\n'
            'event: content_block_delta\ndata: {"type":"content_block_delta",'
            '"delta":{"type":"text_delta","text":"Hello"}}\n\n'
            'event: content_block_delta\ndata: {"type":"content_block_delta",'
            '"delta":{"type":"text_delta","text":" world"}}\n\n'
            'event: message_delta\ndata: {"type":"message_delta","delta":'
            '{"stop_reason":"end_turn"},"usage":{"output_tokens":7}}\n\n'
        )
        route = respx.post("https://api.anthropic.test/v1/messages").mock(
            return_value=httpx.Response(200, text=body)
        )
        p = self._provider()
        text, final = await _collect(p)

        assert text == "Hello world"
        assert final.usage.tokens_in == 11
        assert final.usage.tokens_out == 7
        assert final.stop_reason == "end_turn"

        sent = route.calls[0].request
        assert sent.headers["x-api-key"] == "test-key"
        assert sent.headers["anthropic-version"]
        body_json = json.loads(sent.content)
        # The system prompt is a top-level field for Anthropic, not a message.
        assert body_json["system"] == "be brief"
        assert [m["role"] for m in body_json["messages"]] == ["user"]
        assert body_json["stream"] is True
        await p.aclose()

    @respx.mock
    async def test_missing_key_is_explained(self, monkeypatch) -> None:
        monkeypatch.setattr("jarvis.config.secrets.get", lambda _n: None)
        with pytest.raises(ProviderNotConfigured, match="No API key stored"):
            await _collect(self._provider())

    @respx.mock
    async def test_401_becomes_auth_error(self, monkeypatch) -> None:
        monkeypatch.setattr("jarvis.config.secrets.get", lambda _n: "bad")
        respx.post("https://api.anthropic.test/v1/messages").mock(
            return_value=httpx.Response(401, json={"error": {"message": "invalid key"}})
        )
        with pytest.raises(ProviderAuthError, match="Check the API key"):
            await _collect(self._provider())

    @respx.mock
    async def test_connection_failure_is_explained(self, monkeypatch) -> None:
        monkeypatch.setattr("jarvis.config.secrets.get", lambda _n: "k")
        respx.post("https://api.anthropic.test/v1/messages").mock(
            side_effect=httpx.ConnectError("refused")
        )
        with pytest.raises(ProviderUnavailable, match="Could not reach"):
            await _collect(self._provider())


class TestOpenAICompat:
    @pytest.mark.parametrize(
        ("url", "local"),
        [
            ("http://127.0.0.1:1234/v1", True),
            ("http://localhost:11434/v1", True),
            ("https://api.openai.com/v1", False),
            ("https://openrouter.ai/api/v1", False),
        ],
    )
    def test_cloud_detection_follows_the_url(self, url: str, local: bool) -> None:
        """Pointing this at localhost must not trip the cloud privacy gate."""
        assert is_local_url(url) is local
        p = OpenAICompatProvider("p", ProviderSettings(kind="openai_compat", base_url=url))
        assert p.is_cloud is not local

    @respx.mock
    async def test_parses_sse_and_usage(self) -> None:
        body = (
            'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"lo"},"finish_reason":null}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
            '"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n'
            "data: [DONE]\n\n"
        )
        respx.post("http://127.0.0.1:1234/v1/chat/completions").mock(
            return_value=httpx.Response(200, text=body)
        )
        p = OpenAICompatProvider(
            "local", ProviderSettings(kind="openai_compat", base_url="http://127.0.0.1:1234/v1")
        )
        text, final = await _collect(p)
        assert text == "Hello"
        assert final.usage.tokens_out == 2
        assert final.stop_reason == "stop"
        await p.aclose()

    @respx.mock
    async def test_local_endpoint_needs_no_key(self) -> None:
        p = OpenAICompatProvider(
            "local", ProviderSettings(kind="openai_compat", base_url="http://127.0.0.1:1234/v1")
        )
        assert p.capabilities().configured is True

    def test_remote_without_key_is_flagged(self, monkeypatch) -> None:
        monkeypatch.setattr("jarvis.config.secrets.has", lambda _n: False)
        p = OpenAICompatProvider(
            "openai", ProviderSettings(kind="openai_compat", base_url="https://api.openai.com/v1")
        )
        caps = p.capabilities()
        assert caps.configured is False
        assert "API key" in caps.detail


class TestOllama:
    @respx.mock
    async def test_parses_ndjson(self) -> None:
        body = (
            '{"message":{"content":"Hi"},"done":false}\n'
            '{"message":{"content":" there"},"done":false}\n'
            '{"done":true,"done_reason":"stop","prompt_eval_count":4,"eval_count":3}\n'
        )
        respx.post("http://127.0.0.1:11434/api/chat").mock(
            return_value=httpx.Response(200, text=body)
        )
        p = OllamaProvider("ollama", ProviderSettings(kind="ollama", model="llama3.2"))
        text, final = await _collect(p)
        assert text == "Hi there"
        assert final.usage.tokens_in == 4
        assert final.stop_reason == "stop"
        await p.aclose()

    @respx.mock
    async def test_health_names_the_missing_model(self) -> None:
        respx.get("http://127.0.0.1:11434/api/tags").mock(
            return_value=httpx.Response(200, json={"models": [{"name": "other:latest"}]})
        )
        p = OllamaProvider("ollama", ProviderSettings(kind="ollama", model="llama3.2"))
        ok, detail = await p.health()
        assert ok is False
        assert "ollama pull llama3.2" in detail
        await p.aclose()

    @respx.mock
    async def test_health_when_not_running(self) -> None:
        respx.get("http://127.0.0.1:11434/api/tags").mock(side_effect=httpx.ConnectError("no"))
        p = OllamaProvider("ollama", ProviderSettings(kind="ollama"))
        ok, detail = await p.health()
        assert ok is False
        assert "Could not reach" in detail
        await p.aclose()

    async def test_is_never_cloud(self) -> None:
        assert OllamaProvider("o", ProviderSettings(kind="ollama")).is_cloud is False


class TestGemini:
    """Gemini's wire format is neither Anthropic's nor OpenAI's, which is the
    whole reason it is a separate adapter. These pin the three differences that
    fail silently rather than loudly."""

    def _provider(self, model: str = "gemini-2.0-flash") -> GoogleProvider:
        return GoogleProvider(
            "gemini",
            ProviderSettings(
                kind="google",
                model=model,
                credential="jarvis/gemini",
                base_url="https://gemini.test",
            ),
        )

    @respx.mock
    async def test_parses_sse_stream(self, monkeypatch) -> None:
        monkeypatch.setattr("jarvis.config.secrets.get", lambda _n: "test-key")
        body = (
            'data: {"candidates":[{"content":{"parts":[{"text":"Hello"}],"role":"model"}}]}\n\n'
            'data: {"candidates":[{"content":{"parts":[{"text":" world"}],"role":"model"},'
            '"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":7,'
            '"candidatesTokenCount":3}}\n\n'
        )
        route = respx.post(
            "https://gemini.test/v1beta/models/gemini-2.0-flash:streamGenerateContent"
        ).mock(return_value=httpx.Response(200, text=body))

        text, final = await _collect(self._provider())

        assert text == "Hello world"
        assert final is not None and final.usage is not None
        assert final.usage.tokens_in == 7
        assert final.usage.tokens_out == 3
        assert final.stop_reason == "STOP"
        # Without ?alt=sse the endpoint returns one JSON array at the end, which
        # looks exactly like a provider that refuses to stream.
        assert route.calls.last.request.url.params["alt"] == "sse"

    @respx.mock
    async def test_the_assistant_role_is_model_not_assistant(self, monkeypatch) -> None:
        """Sending "assistant" is accepted and then ignored, so the whole
        conversation reads as though the user said everything."""
        monkeypatch.setattr("jarvis.config.secrets.get", lambda _n: "test-key")
        route = respx.post(
            "https://gemini.test/v1beta/models/gemini-2.0-flash:streamGenerateContent"
        ).mock(return_value=httpx.Response(200, text='data: {"candidates":[]}\n\n'))

        provider = self._provider()
        request = CompletionRequest(
            messages=[
                Message("system", "be brief"),
                Message("user", "hi"),
                Message("assistant", "hello"),
                Message("user", "again"),
            ]
        )
        async for _ in provider.stream(request):
            pass

        sent = json.loads(route.calls.last.request.content)
        assert [c["role"] for c in sent["contents"]] == ["user", "model", "user"]
        assert "assistant" not in json.dumps(sent["contents"])
        # The system prompt is a separate field, not a message.
        assert sent["systemInstruction"]["parts"][0]["text"] == "be brief"
        assert sent["contents"][0]["parts"][0]["text"] == "hi"

    @respx.mock
    async def test_the_key_goes_in_a_header_not_the_url(self, monkeypatch) -> None:
        """A key in a query string ends up in proxy logs and crash reports."""
        monkeypatch.setattr("jarvis.config.secrets.get", lambda _n: "secret-key")
        route = respx.post(
            "https://gemini.test/v1beta/models/gemini-2.0-flash:streamGenerateContent"
        ).mock(return_value=httpx.Response(200, text='data: {"candidates":[]}\n\n'))

        async for _ in self._provider().stream(_req()):
            pass

        request = route.calls.last.request
        assert request.headers["x-goog-api-key"] == "secret-key"
        assert "secret-key" not in str(request.url)
        assert "key" not in request.url.params

    @respx.mock
    async def test_a_mid_stream_error_is_raised_despite_http_200(self, monkeypatch) -> None:
        monkeypatch.setattr("jarvis.config.secrets.get", lambda _n: "test-key")
        respx.post("https://gemini.test/v1beta/models/gemini-2.0-flash:streamGenerateContent").mock(
            return_value=httpx.Response(
                200,
                text=(
                    'data: {"error":{"message":"quota exceeded","status":"RESOURCE_EXHAUSTED"}}\n\n'
                ),
            )
        )
        with pytest.raises(ProviderError, match="quota exceeded"):
            await _collect(self._provider())

    async def test_no_key_is_refused_before_any_request(self, monkeypatch) -> None:
        monkeypatch.setattr("jarvis.config.secrets.get", lambda _n: None)
        with pytest.raises(ProviderNotConfigured):
            await _collect(self._provider())

    @respx.mock
    async def test_health_names_the_alternatives_when_the_model_is_gone(self, monkeypatch) -> None:
        """Google retires model IDs on its own schedule. A bare 404 is the
        worst possible explanation of that, so health lists what the key has."""
        monkeypatch.setattr("jarvis.config.secrets.get", lambda _n: "test-key")
        monkeypatch.setattr("jarvis.config.secrets.has", lambda _n: True)
        respx.get("https://gemini.test/v1beta/models").mock(
            return_value=httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-9-turbo",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                        {
                            "name": "models/embedding-only",
                            "supportedGenerationMethods": ["embedContent"],
                        },
                    ]
                },
            )
        )
        ok, detail = await self._provider("gemini-retired").health()
        assert not ok
        assert "gemini-retired" in detail
        assert "gemini-9-turbo" in detail
        # Embedding-only models cannot answer a chat turn, so they are not
        # offered as alternatives.
        assert "embedding-only" not in detail

    @respx.mock
    async def test_health_passes_when_the_model_is_listed(self, monkeypatch) -> None:
        monkeypatch.setattr("jarvis.config.secrets.get", lambda _n: "test-key")
        monkeypatch.setattr("jarvis.config.secrets.has", lambda _n: True)
        respx.get("https://gemini.test/v1beta/models").mock(
            return_value=httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-2.0-flash",
                            "supportedGenerationMethods": ["generateContent"],
                        }
                    ]
                },
            )
        )
        ok, detail = await self._provider().health()
        assert ok, detail

    async def test_is_reported_as_cloud(self) -> None:
        caps = self._provider().capabilities()
        assert caps.is_cloud is True
        assert caps.streaming is True
        # Function calling exists in this API but is not wired through the
        # gateway's tool path, and claiming it would offer something inert.
        assert caps.tools is False


class TestWhetherAKeyIsEvenWanted:
    """`needs_key` is what the panel shows an Add key button from.

    It used to infer that from `configured`, with `kind == "dev_echo"` as the
    one exception — so every other keyless provider offered to replace a key
    it never had. Ollama did, and so did the relay, which is cloud, ready and
    takes nothing to type.
    """

    def test_a_vendor_api_always_wants_one(self) -> None:
        """Even with no credential configured — that is when it matters most.

        Deriving this from whether a credential name is set would hide the
        button in exactly the state where someone needs to press it.
        """
        from jarvis.ai.providers.anthropic import AnthropicProvider
        from jarvis.ai.providers.google import GoogleProvider

        for cls in (AnthropicProvider, GoogleProvider):
            caps = cls("p", ProviderSettings(kind="anthropic")).capabilities()
            assert caps.needs_key is True, cls.__name__
            assert caps.configured is False, "and it is not usable without one"

    def test_the_keyless_ones_do_not(self) -> None:
        from jarvis.ai.gateway import build

        for kind in ("dev_echo", "ollama", "jarvis_cloud"):
            caps = build("p", ProviderSettings(kind=kind)).capabilities()
            assert caps.needs_key is False, kind

    def test_an_openai_shaped_endpoint_asks_only_when_it_is_remote(self) -> None:
        """One adapter, two situations: OpenAI needs a key, LM Studio does not."""
        remote = OpenAICompatProvider(
            "openai", ProviderSettings(kind="openai_compat", base_url="https://api.openai.com/v1")
        )
        local = OpenAICompatProvider(
            "lmstudio", ProviderSettings(kind="openai_compat", base_url="http://127.0.0.1:1234/v1")
        )
        assert remote.capabilities().needs_key is True
        assert local.capabilities().needs_key is False

    def test_no_provider_claims_to_be_ready_while_still_wanting_a_key(self) -> None:
        """The pair has to stay coherent however a new adapter sets them."""
        from typing import get_args

        from jarvis.ai.gateway import build
        from jarvis.config.settings import ProviderKind

        for kind in get_args(ProviderKind):
            caps = build("p", ProviderSettings(kind=kind)).capabilities()
            if caps.needs_key:
                assert not caps.configured, f"{kind} reports itself ready with no key stored for it"


class TestEveryAdvertisedKindCanBeBuilt:
    """`ProviderKind` and the gateway registry have to agree.

    `llama_cpp` was an accepted kind with nothing behind it: the config
    validated, then building the provider raised "Unknown provider kind", which
    reads as a broken install rather than as a typo.
    """

    def test_registry_covers_every_provider_kind(self) -> None:
        from typing import get_args

        from jarvis.ai.gateway import REGISTRY
        from jarvis.config.settings import ProviderKind

        advertised = set(get_args(ProviderKind))
        assert advertised <= set(REGISTRY), (
            f"accepted by config but not buildable: {sorted(advertised - set(REGISTRY))}"
        )

    def test_every_kind_builds(self) -> None:
        from typing import get_args

        from jarvis.ai.gateway import build
        from jarvis.config.settings import ProviderKind

        for kind in get_args(ProviderKind):
            provider = build(kind, ProviderSettings(kind=kind))
            assert provider.capabilities().kind


class TestTheShippedPresets:
    """Every commented provider block in the default config must work.

    A preset that does not parse, or names a `kind` that cannot be built, is
    worse than no preset: it is copied verbatim from a file that looks
    authoritative and then fails somewhere else entirely.
    """

    @staticmethod
    def _uncommented() -> str:
        """The default config with every commented-out line enabled."""
        import re

        from jarvis.config.settings import DEFAULT_CONFIG_TOML

        out = []
        for line in DEFAULT_CONFIG_TOML.splitlines():
            # Only lines that are a commented-out setting or table header, not
            # prose comments.
            if re.match(r"^#\s*(\[|[a-z_]+\s*=)", line):
                out.append(re.sub(r"^#\s?", "", line))
            else:
                out.append(line)
        return "\n".join(out)

    def test_every_preset_parses(self) -> None:
        import tomllib

        from jarvis.config.settings import Settings

        settings = Settings(**tomllib.loads(self._uncommented()))
        # Named rather than counted, so adding a preset without a test is caught.
        assert {
            "anthropic",
            "openai",
            "gemini",
            "openrouter",
            "groq",
            "ollama",
            "local",
            "dev_echo",
        } <= set(settings.ai.providers)

    def test_every_preset_builds_a_working_provider(self) -> None:
        import tomllib

        from jarvis.ai.gateway import build
        from jarvis.config.settings import Settings

        settings = Settings(**tomllib.loads(self._uncommented()))
        for name, cfg in settings.ai.providers.items():
            provider = build(name, cfg)
            caps = provider.capabilities()
            assert caps.kind == cfg.kind, name
            if not caps.is_cloud:
                continue

            # A cloud preset that needs a key must report itself unconfigured
            # rather than ready, since none is stored — that is what makes the
            # interface offer to add one instead of failing on first use.
            if cfg.credential:
                assert not caps.configured, f"{name} claims to be configured with no key"
                assert "key" in caps.detail.lower(), name
                continue

            # A relay holds the key at the other end, so there is nothing to
            # add and nothing to be unconfigured about. Its obligation is the
            # opposite one: not to read as private just because it is free.
            # `allow_cloud` still gates it — see the two tests below, which key
            # off `is_cloud` and so cover this preset too.
            assert caps.configured, f"{name} has no key to add, so it cannot be unconfigured"
            assert not caps.detail.lower().startswith("ready"), (
                f"{name} has contacted nothing yet and cannot claim to be ready"
            )

    def test_the_cloud_presets_stay_switched_off(self) -> None:
        """Pasting a key into the config must not be enough to start sending
        conversations off the machine; `allow_cloud` is a separate decision."""
        import tomllib

        from jarvis.config.settings import Settings

        settings = Settings(**tomllib.loads(self._uncommented()))
        assert settings.ai.allow_cloud is False
        assert settings.ai.allow_cloud_content is False
        assert settings.ai.default == "dev_echo"

    async def test_every_cloud_preset_is_refused_while_allow_cloud_is_false(self) -> None:
        """The privacy gate keys off `is_cloud`, so a new cloud adapter is
        covered the moment it declares itself one. Asserted rather than assumed,
        because the cost of being wrong is a conversation leaving the machine.

        Every cloud preset in the shipped config, not one named here: a preset
        needing no key has nothing else standing in the way, so this switch is
        the only thing between a fresh install and a third party.
        """
        import tomllib

        from jarvis.ai.gateway import Gateway, build
        from jarvis.ai.types import CompletionRequest, Message
        from jarvis.config.settings import Settings
        from jarvis.util.errors import JarvisError

        data = tomllib.loads(self._uncommented())
        base = Settings(**data)
        cloud = [n for n, c in base.ai.providers.items() if build(n, c).capabilities().is_cloud]
        assert cloud, "the config should ship some cloud presets, or this test checks nothing"
        # Derived from `is_cloud`, so a provider that stopped declaring itself
        # one would drop out of the list rather than fail. Named here because
        # the relay is the preset with nothing else in its way.
        assert "jarvis_cloud" in {base.ai.providers[n].kind for n in cloud}

        for name in cloud:
            settings = Settings(**{**data, "ai": {**data["ai"], "default": name}})
            assert settings.ai.allow_cloud is False

            gateway = Gateway(settings)
            with pytest.raises(JarvisError):
                request = CompletionRequest(messages=[Message("user", "hello")])
                async for _ in gateway.stream(request):
                    pass
            await gateway.aclose()

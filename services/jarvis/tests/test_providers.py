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
from jarvis.ai.providers.ollama import OllamaProvider
from jarvis.ai.providers.openai_compat import OpenAICompatProvider, is_local_url
from jarvis.ai.types import CompletionRequest, JobClass, Message
from jarvis.config.settings import ProviderSettings
from jarvis.util.errors import ProviderAuthError, ProviderNotConfigured, ProviderUnavailable


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

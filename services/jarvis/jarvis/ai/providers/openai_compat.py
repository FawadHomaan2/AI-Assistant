"""OpenAI-compatible chat completions.

One adapter covers LM Studio, vLLM, llama.cpp's server, OpenRouter, Groq, Azure
and OpenAI itself — they differ only by base_url and whether a key is needed.

`is_cloud` is decided from the URL rather than hardcoded: pointing this at
127.0.0.1 is a local provider and must not be blocked by the cloud switch.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import httpx

from jarvis.ai.base import Provider
from jarvis.ai.providers import _http
from jarvis.ai.types import Capabilities, Chunk, CompletionRequest, Usage
from jarvis.config import secrets
from jarvis.util.errors import ProviderError
from jarvis.util.logging import get_logger

log = get_logger(__name__)

# Hostnames recognised as local. "0.0.0.0" appears because a user may type it
# as a base_url; nothing here binds a socket.
LOCAL_HOSTS = frozenset(
    {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}  # noqa: S104
)


def is_local_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in LOCAL_HOSTS


class OpenAICompatProvider(Provider):
    kind = "openai_compat"

    def __init__(self, name, settings) -> None:  # type: ignore[no-untyped-def]
        super().__init__(name, settings)
        self._client: httpx.AsyncClient | None = None

    @property
    def base_url(self) -> str:
        return self.settings.base_url or "https://api.openai.com/v1"

    @property
    def is_cloud(self) -> bool:  # type: ignore[override]
        return not is_local_url(self.base_url)

    @property
    def model(self) -> str:
        return self.settings.model or "gpt-4o-mini"

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"content-type": "application/json"}
            # A local endpoint usually needs no key; a missing one is not an error.
            if self.settings.credential and (key := secrets.get(self.settings.credential)):
                headers["authorization"] = f"Bearer {key}"
            self._client = _http.client(self.base_url, self.settings.timeout_seconds, headers)
        return self._client

    def _payload(self, request: CompletionRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": request.max_tokens or self.settings.max_tokens,
            "temperature": (
                request.temperature
                if request.temperature is not None
                else self.settings.temperature
            ),
        }
        if request.stop:
            body["stop"] = request.stop
        return body

    async def stream(self, request: CompletionRequest) -> AsyncIterator[Chunk]:
        client = self._ensure_client()
        usage = Usage()
        stop_reason: str | None = None
        try:
            async with client.stream(
                "POST", "/chat/completions", json=self._payload(request)
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    _http.raise_for_status(resp, self.name, body)

                async for _event, data in _http.sse_lines(resp):
                    if not data or data == "[DONE]":
                        continue
                    parsed = _http.parse_json(data, self.name)

                    if err := parsed.get("error"):
                        raise ProviderError(
                            f"{self.name} returned an error: "
                            f"{err.get('message', 'unknown') if isinstance(err, dict) else err}"
                        )

                    # Some servers send a usage-only final frame with no choices.
                    if meta := parsed.get("usage"):
                        usage.tokens_in = int(meta.get("prompt_tokens", 0) or 0)
                        usage.tokens_out = int(meta.get("completion_tokens", 0) or 0)

                    for choice in parsed.get("choices", []) or []:
                        delta = choice.get("delta") or {}
                        if text := delta.get("content"):
                            yield Chunk(text=text)
                        if reason := choice.get("finish_reason"):
                            stop_reason = reason
        except httpx.HTTPError as exc:
            raise _http.wrap_transport_error(exc, self.name, self.base_url) from exc

        yield Chunk(done=True, usage=usage, stop_reason=stop_reason)

    def capabilities(self) -> Capabilities:
        needs_key = self.is_cloud
        has_key = bool(self.settings.credential) and secrets.has(self.settings.credential)
        configured = has_key or not needs_key
        return Capabilities(
            name=self.name,
            kind=self.kind,
            model=self.model,
            streaming=True,
            tools=True,
            is_cloud=self.is_cloud,
            configured=configured,
            detail=(
                "Ready."
                if configured
                else "This endpoint is remote and has no API key stored. Add one in Settings."
            ),
        )

    async def health(self) -> tuple[bool, str]:
        try:
            client = self._ensure_client()
            resp = await client.get("/models", timeout=8.0)
            if resp.status_code >= 400:
                return False, f"{self.base_url}/models returned {resp.status_code}."
            return True, f"Reachable at {self.base_url}."
        except httpx.HTTPError as exc:
            return False, str(_http.wrap_transport_error(exc, self.name, self.base_url).message)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

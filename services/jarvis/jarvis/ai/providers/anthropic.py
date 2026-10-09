"""Anthropic Messages API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from jarvis.ai.base import Provider
from jarvis.ai.providers import _http
from jarvis.ai.types import Capabilities, Chunk, CompletionRequest, Usage
from jarvis.config import secrets
from jarvis.util.errors import ProviderError, ProviderNotConfigured
from jarvis.util.logging import get_logger

log = get_logger(__name__)

DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
# Current as of this writing. Model IDs carry no date suffix — "claude-opus-5-5",
# never "claude-opus-5-5-20260401". Override per provider in config.toml.
DEFAULT_MODEL = "claude-opus-5-5"


class AnthropicProvider(Provider):
    kind = "anthropic"
    is_cloud = True

    def __init__(self, name, settings) -> None:  # type: ignore[no-untyped-def]
        super().__init__(name, settings)
        self._client: httpx.AsyncClient | None = None

    @property
    def model(self) -> str:
        return self.settings.model or DEFAULT_MODEL

    @property
    def base_url(self) -> str:
        return self.settings.base_url or DEFAULT_BASE_URL

    def _api_key(self) -> str:
        key = secrets.get(self.settings.credential) if self.settings.credential else None
        if not key:
            raise ProviderNotConfigured(
                f"No API key stored for provider {self.name!r}. Add one in Settings; "
                "it is saved to the Windows Credential Manager, not to a config file.",
                credential_name=self.settings.credential,
            )
        return key

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = _http.client(
                self.base_url,
                self.settings.timeout_seconds,
                {
                    "x-api-key": self._api_key(),
                    "anthropic-version": API_VERSION,
                    "content-type": "application/json",
                },
            )
        return self._client

    def _payload(self, request: CompletionRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens or self.settings.max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in request.without_system()],
            "stream": True,
        }
        if (system := request.system_prompt()) is not None:
            body["system"] = system
        temperature = (
            request.temperature if request.temperature is not None else self.settings.temperature
        )
        body["temperature"] = temperature
        if request.stop:
            body["stop_sequences"] = request.stop
        return body

    async def stream(self, request: CompletionRequest) -> AsyncIterator[Chunk]:
        client = self._ensure_client()
        usage = Usage()
        stop_reason: str | None = None
        try:
            async with client.stream("POST", "/v1/messages", json=self._payload(request)) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    _http.raise_for_status(resp, "Anthropic", body)

                async for event, data in _http.sse_lines(resp):
                    if not data:
                        continue
                    parsed = _http.parse_json(data, "Anthropic")
                    etype = parsed.get("type", event)

                    if etype == "content_block_delta":
                        delta = parsed.get("delta", {})
                        if text := delta.get("text"):
                            yield Chunk(text=text)
                    elif etype == "message_start":
                        meta = parsed.get("message", {}).get("usage", {})
                        usage.tokens_in = int(meta.get("input_tokens", 0) or 0)
                    elif etype == "message_delta":
                        stop_reason = parsed.get("delta", {}).get("stop_reason") or stop_reason
                        meta = parsed.get("usage", {})
                        usage.tokens_out = int(meta.get("output_tokens", 0) or 0)
                    elif etype == "error":
                        err = parsed.get("error", {})
                        raise ProviderError(
                            f"Anthropic returned an error: {err.get('message', 'unknown')}",
                            error_type=err.get("type"),
                        )
        except httpx.HTTPError as exc:
            raise _http.wrap_transport_error(exc, "Anthropic", self.base_url) from exc

        yield Chunk(done=True, usage=usage, stop_reason=stop_reason)

    def capabilities(self) -> Capabilities:
        configured = bool(self.settings.credential) and secrets.has(self.settings.credential)
        return Capabilities(
            name=self.name,
            kind=self.kind,
            model=self.model,
            streaming=True,
            tools=True,
            is_cloud=True,
            configured=configured,
            # Always: this is a vendor API and there is no keyless mode.
            # Deriving it from whether a credential is configured would hide
            # the Add key button exactly when no key has been set up yet.
            needs_key=True,
            detail=("Ready." if configured else "No API key stored. Add one in Settings."),
        )

    async def health(self) -> tuple[bool, str]:
        if not secrets.has(self.settings.credential):
            return False, "No API key stored."
        return True, "API key present; reachability is checked on first use."

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

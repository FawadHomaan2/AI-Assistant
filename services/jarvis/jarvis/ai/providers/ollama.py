"""Ollama. Local by definition, and the easiest path to a working local model."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from jarvis.ai.base import Provider
from jarvis.ai.providers import _http
from jarvis.ai.types import Capabilities, Chunk, CompletionRequest, Usage
from jarvis.util.errors import ProviderError
from jarvis.util.logging import get_logger

log = get_logger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:11434"


class OllamaProvider(Provider):
    kind = "ollama"
    is_cloud = False

    def __init__(self, name, settings) -> None:  # type: ignore[no-untyped-def]
        super().__init__(name, settings)
        self._client: httpx.AsyncClient | None = None

    @property
    def base_url(self) -> str:
        return self.settings.base_url or DEFAULT_BASE_URL

    @property
    def model(self) -> str:
        return self.settings.model or "llama3.2"

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = _http.client(
                self.base_url, self.settings.timeout_seconds, {"content-type": "application/json"}
            )
        return self._client

    async def stream(self, request: CompletionRequest) -> AsyncIterator[Chunk]:
        client = self._ensure_client()
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "stream": True,
            "options": {
                "temperature": (
                    request.temperature
                    if request.temperature is not None
                    else self.settings.temperature
                ),
                "num_predict": request.max_tokens or self.settings.max_tokens,
            },
        }
        if request.stop:
            body["options"]["stop"] = request.stop

        usage = Usage()
        stop_reason: str | None = None
        try:
            async with client.stream("POST", "/api/chat", json=body) as resp:
                if resp.status_code >= 400:
                    text = (await resp.aread()).decode("utf-8", "replace")
                    _http.raise_for_status(resp, "Ollama", text)

                # Ollama streams newline-delimited JSON, not SSE.
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    parsed = _http.parse_json(line, "Ollama")
                    if err := parsed.get("error"):
                        raise ProviderError(f"Ollama returned an error: {err}")
                    if content := (parsed.get("message") or {}).get("content"):
                        yield Chunk(text=str(content))
                    if parsed.get("done"):
                        usage.tokens_in = int(parsed.get("prompt_eval_count", 0) or 0)
                        usage.tokens_out = int(parsed.get("eval_count", 0) or 0)
                        reason = parsed.get("done_reason")
                        stop_reason = str(reason) if reason else "stop"
        except httpx.HTTPError as exc:
            raise _http.wrap_transport_error(exc, "Ollama", self.base_url) from exc

        yield Chunk(done=True, usage=usage, stop_reason=stop_reason)

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name=self.name,
            kind=self.kind,
            model=self.model,
            streaming=True,
            tools=True,
            is_cloud=False,
            configured=True,
            detail=f"Local Ollama at {self.base_url}.",
        )

    async def health(self) -> tuple[bool, str]:
        try:
            client = self._ensure_client()
            resp = await client.get("/api/tags", timeout=5.0)
            if resp.status_code >= 400:
                return False, f"Ollama responded {resp.status_code}."
            names = [m.get("name", "") for m in resp.json().get("models", [])]
            if self.model not in names and f"{self.model}:latest" not in names:
                return False, (
                    f"Ollama is running, but model {self.model!r} is not pulled. "
                    f"Run: ollama pull {self.model}"
                )
            return True, f"Ollama ready with {self.model}."
        except httpx.HTTPError as exc:
            return False, str(_http.wrap_transport_error(exc, "Ollama", self.base_url).message)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

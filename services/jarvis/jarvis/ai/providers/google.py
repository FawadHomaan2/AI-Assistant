"""Google Gemini, via the Generative Language API.

The only provider here whose wire format is neither Anthropic's nor OpenAI's,
which is why it needs its own module rather than a `base_url` pointed at the
openai_compat adapter. Three differences matter:

* the assistant role is **`model`**, not `assistant`;
* messages are `contents[].parts[].text`, and the system prompt is a separate
  `systemInstruction` rather than a message;
* streaming needs `?alt=sse`. Without it the endpoint returns a single JSON
  array at the end, which looks exactly like a provider that refuses to stream.

The key goes in the `x-goog-api-key` header rather than the `?key=` query
parameter Google's docs favour. Both work; a key in a URL ends up in proxy logs,
crash reports and anything that records a request line.

Model IDs are **not** guessed at call time: Google renames and retires them on
its own schedule, so `models()` lists what this key can actually reach and
`health()` reports whether the configured model is among them. A default that
has quietly been retired otherwise surfaces as an opaque 404.
"""

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

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"

#: A sensible starting point, not an assertion about Google's current line-up.
#: Override per provider in config.toml; `health()` checks it really exists.
DEFAULT_MODEL = "gemini-2.0-flash"


class GoogleProvider(Provider):
    kind = "google"
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
                    "x-goog-api-key": self._api_key(),
                    "content-type": "application/json",
                },
            )
        return self._client

    def _payload(self, request: CompletionRequest) -> dict[str, Any]:
        # Gemini calls the assistant "model". Sending "assistant" is accepted by
        # the validator and then ignored, so the conversation silently reads as
        # though the user said everything.
        contents = [
            {
                "role": "model" if message.role == "assistant" else "user",
                "parts": [{"text": message.content}],
            }
            for message in request.without_system()
        ]

        temperature = (
            request.temperature if request.temperature is not None else self.settings.temperature
        )
        generation: dict[str, Any] = {
            "maxOutputTokens": request.max_tokens or self.settings.max_tokens,
            "temperature": temperature,
        }
        if request.stop:
            generation["stopSequences"] = request.stop

        body: dict[str, Any] = {"contents": contents, "generationConfig": generation}
        if (system := request.system_prompt()) is not None:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        return body

    async def stream(self, request: CompletionRequest) -> AsyncIterator[Chunk]:
        client = self._ensure_client()
        usage = Usage()
        stop_reason: str | None = None
        url = f"/v1beta/models/{self.model}:streamGenerateContent"

        try:
            async with client.stream(
                "POST", url, params={"alt": "sse"}, json=self._payload(request)
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    _http.raise_for_status(resp, "Gemini", body)

                async for _event, data in _http.sse_lines(resp):
                    if not data:
                        continue
                    parsed = _http.parse_json(data, "Gemini")

                    # Errors arrive with HTTP 200 once the stream has started,
                    # so the status check above cannot catch them.
                    if error := parsed.get("error"):
                        raise ProviderError(
                            f"Gemini returned an error: {error.get('message', 'unknown')}",
                            error_type=str(error.get("status") or error.get("code") or ""),
                        )

                    for candidate in parsed.get("candidates") or []:
                        for part in (candidate.get("content") or {}).get("parts") or []:
                            if text := part.get("text"):
                                yield Chunk(text=text)
                        if reason := candidate.get("finishReason"):
                            stop_reason = str(reason)

                    # Repeated on every chunk with running totals, so the last
                    # one seen is the final count.
                    if meta := parsed.get("usageMetadata"):
                        usage.tokens_in = int(meta.get("promptTokenCount", 0) or 0)
                        usage.tokens_out = int(meta.get("candidatesTokenCount", 0) or 0)
        except httpx.HTTPError as exc:
            raise _http.wrap_transport_error(exc, "Gemini", self.base_url) from exc

        yield Chunk(done=True, usage=usage, stop_reason=stop_reason)

    async def models(self) -> list[str]:
        """Model names this key can reach, newest naming included.

        Exists because the default above will age. Asking Google beats guessing.
        """
        client = self._ensure_client()
        try:
            resp = await client.get("/v1beta/models", timeout=10.0)
            if resp.status_code >= 400:
                _http.raise_for_status(resp, "Gemini", resp.text)
            payload = resp.json()
        except httpx.HTTPError as exc:
            raise _http.wrap_transport_error(exc, "Gemini", self.base_url) from exc

        names: list[str] = []
        for entry in payload.get("models") or []:
            name = str(entry.get("name", ""))
            # Returned as "models/gemini-...", but requests take the bare id.
            trimmed = name.removeprefix("models/")
            if trimmed and "generateContent" in (entry.get("supportedGenerationMethods") or []):
                names.append(trimmed)
        return sorted(names)

    def capabilities(self) -> Capabilities:
        configured = bool(self.settings.credential) and secrets.has(self.settings.credential)
        return Capabilities(
            name=self.name,
            kind=self.kind,
            model=self.model,
            streaming=True,
            # Function calling exists in this API but is not wired through the
            # gateway's tool path yet, and claiming it would make the interface
            # offer something that does nothing.
            tools=False,
            is_cloud=True,
            configured=configured,
            detail=("Ready." if configured else "No API key stored. Add one in Settings."),
        )

    async def health(self) -> tuple[bool, str]:
        if not secrets.has(self.settings.credential):
            return False, "No API key stored."
        try:
            available = await self.models()
        except ProviderError as exc:
            return False, exc.message
        if not available:
            return False, "The key works, but no model on it supports generateContent."
        if self.model not in available:
            # The common failure, and the one a bare 404 explains worst.
            return False, (
                f"{self.model!r} is not available to this key. "
                f"Try one of: {', '.join(available[:6])}."
            )
        return True, f"{self.model} is available ({len(available)} models on this key)."

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

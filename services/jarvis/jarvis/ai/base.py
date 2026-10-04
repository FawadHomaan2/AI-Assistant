"""Provider interface.

One abstract class, four implementations. Anything OpenAI-shaped (LM Studio,
vLLM, llama.cpp's server, OpenRouter, Azure, Groq) works through the
openai_compat adapter with a different base_url — adding a provider is one file.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator

from jarvis.ai.types import Capabilities, Chunk, CompletionRequest
from jarvis.config.settings import ProviderSettings


class Provider(abc.ABC):
    """A language-model backend."""

    kind: str = "abstract"
    is_cloud: bool = False

    def __init__(self, name: str, settings: ProviderSettings) -> None:
        self.name = name
        self.settings = settings

    @abc.abstractmethod
    def stream(self, request: CompletionRequest) -> AsyncIterator[Chunk]:
        """Yield deltas, then exactly one chunk with `done=True`.

        Declared as a plain method returning an async iterator rather than as
        `async def`, so implementations can be async generators without the
        abstract body needing an unreachable `yield`.
        """

    async def complete(self, request: CompletionRequest) -> str:
        """Collect a stream into one string. Default is fine for every provider."""
        parts: list[str] = []
        async for chunk in self.stream(request):
            parts.append(chunk.text)
        return "".join(parts)

    @abc.abstractmethod
    def capabilities(self) -> Capabilities:
        """Describe this provider, including whether it is usable right now."""

    async def health(self) -> tuple[bool, str]:
        """Cheap reachability probe. (ok, human-readable detail)."""
        return True, "no health check implemented for this provider"

    async def aclose(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Release connections. Providers holding none need not override this."""

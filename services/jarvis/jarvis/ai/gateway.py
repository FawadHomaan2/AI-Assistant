"""Provider gateway.

Single entry point the agents use. It:
  * builds providers from settings and caches them
  * routes a job class to the configured provider
  * runs the egress check and sanitiser before anything leaves the machine
  * translates any failure into a typed, explainable error
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

from jarvis.ai import privacy
from jarvis.ai.base import Provider
from jarvis.ai.providers.anthropic import AnthropicProvider
from jarvis.ai.providers.dev_echo import DevEchoProvider
from jarvis.ai.providers.google import GoogleProvider
from jarvis.ai.providers.ollama import OllamaProvider
from jarvis.ai.providers.openai_compat import OpenAICompatProvider
from jarvis.ai.types import Capabilities, Chunk, CompletionRequest, JobClass
from jarvis.config.settings import ProviderSettings, Settings
from jarvis.util.errors import ConfigError, JarvisError, ProviderError
from jarvis.util.logging import get_logger

log = get_logger(__name__)

#: Adding a provider is a single entry here plus one module.
REGISTRY: dict[str, type[Provider]] = {
    "dev_echo": DevEchoProvider,
    "anthropic": AnthropicProvider,
    "openai_compat": OpenAICompatProvider,
    "ollama": OllamaProvider,
    "google": GoogleProvider,
    # llama.cpp's server speaks the OpenAI API, so it is the same adapter with a
    # local base_url. It was already an accepted `kind` with nothing behind it,
    # which failed at build time with "Unknown provider kind".
    "llama_cpp": OpenAICompatProvider,
}


def build(name: str, settings: ProviderSettings) -> Provider:
    cls = REGISTRY.get(settings.kind)
    if cls is None:
        raise ConfigError(
            f"Unknown provider kind {settings.kind!r} for {name!r}. Supported: {sorted(REGISTRY)}."
        )
    return cls(name, settings)


class Gateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cache: dict[str, Provider] = {}

    def provider_for(self, job: JobClass) -> Provider:
        name, cfg = self.settings.provider_for_job(job.value)
        if name not in self._cache:
            self._cache[name] = build(name, cfg)
        return self._cache[name]

    def provider_by_name(self, name: str) -> Provider:
        resolved, cfg = self.settings.provider(name)
        if resolved not in self._cache:
            self._cache[resolved] = build(resolved, cfg)
        return self._cache[resolved]

    async def stream(
        self, request: CompletionRequest, *, provider_name: str | None = None
    ) -> AsyncIterator[Chunk]:
        """Stream a completion, after the privacy gate approves it."""
        provider = (
            self.provider_by_name(provider_name)
            if provider_name
            else self.provider_for(request.job)
        )

        # Raises PrivacyViolation rather than silently degrading.
        privacy.check(
            request,
            provider_name=provider.name,
            is_cloud=provider.is_cloud,
            allow_cloud=self.settings.ai.allow_cloud,
            allow_cloud_content=self.settings.ai.allow_cloud_content,
        )

        outbound, removed = privacy.sanitise(request)
        if removed:
            log.info(
                "redacted before sending to provider",
                provider=provider.name,
                chars_removed=removed,
            )
        if provider.is_cloud:
            # Record the fact of egress; the Privacy dashboard reads this.
            log.info(
                "sending to cloud provider",
                provider=provider.name,
                job=request.job.value,
                privacy_class=request.privacy.value,
                message_count=len(outbound.messages),
            )

        started = time.monotonic()
        try:
            async for chunk in provider.stream(outbound):
                yield chunk
        except JarvisError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"{provider.name} failed unexpectedly: {exc}", provider=provider.name
            ) from exc
        finally:
            log.debug(
                "provider stream finished",
                provider=provider.name,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

    def capabilities(self) -> list[Capabilities]:
        out: list[Capabilities] = []
        for name in self.settings.ai.providers:
            try:
                out.append(self.provider_by_name(name).capabilities())
            except JarvisError as exc:
                out.append(
                    Capabilities(
                        name=name,
                        kind=self.settings.ai.providers[name].kind,
                        model=self.settings.ai.providers[name].model,
                        configured=False,
                        detail=exc.message,
                    )
                )
        return out

    async def health(self, name: str | None = None) -> dict[str, tuple[bool, str]]:
        names = [name] if name else list(self.settings.ai.providers)
        results: dict[str, tuple[bool, str]] = {}
        for item in names:
            try:
                results[item] = await self.provider_by_name(item).health()
            except JarvisError as exc:
                results[item] = (False, exc.message)
        return results

    async def reload(self, settings: Settings) -> None:
        """Adopt changed settings, dropping every built provider.

        The cache has to go, not just the settings: a provider is constructed
        once and bakes its API key into an httpx client's headers, so a key
        stored a moment ago would not be used until the next launch. Clearing
        the cache is what makes "paste a key, press Test" work.
        """
        await self.aclose()
        self.settings = settings

    async def aclose(self) -> None:
        for provider in self._cache.values():
            await provider.aclose()
        self._cache.clear()

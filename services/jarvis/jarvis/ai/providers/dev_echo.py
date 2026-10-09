"""Development echo provider.

NOT a language model. It streams back a description of what it received, so the
transport, streaming, persistence and agent plumbing can be exercised end to end
without downloading a model or configuring an API key.

It exists because the alternative — shipping a provider that fabricates
plausible-sounding answers — would make the system look like it works when it
does not. Every surface that uses it says what it is.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from jarvis.ai.base import Provider
from jarvis.ai.types import Capabilities, Chunk, CompletionRequest, Usage

BANNER = "[dev echo — not a language model]"


class DevEchoProvider(Provider):
    kind = "dev_echo"
    is_cloud = False

    #: Seconds between chunks, so streaming is visibly incremental in the UI.
    delay = 0.02

    async def stream(self, request: CompletionRequest) -> AsyncIterator[Chunk]:
        user = next(
            (m.content for m in reversed(request.messages) if m.role == "user"),
            "",
        )
        reply = (
            f"{BANNER}\n\n"
            f"No model is configured, so there is nothing to answer with. "
            f"This confirms the full path is working: your message reached the "
            f"Jarvis core, was persisted, routed as job '{request.job.value}', and "
            f"is streaming back to the interface.\n\n"
            f"You said: {user.strip()[:400]}\n\n"
            f"Configure a provider in Settings — a local model via Ollama or an "
            f"OpenAI-compatible endpoint, or a cloud API key — to get real answers."
        )

        # Word-by-word, to mirror how a real provider streams.
        words = reply.split(" ")
        for i, word in enumerate(words):
            yield Chunk(text=word if i == 0 else f" {word}")
            if self.delay:
                await asyncio.sleep(self.delay)

        yield Chunk(
            done=True,
            usage=Usage(
                tokens_in=sum(len(m.content.split()) for m in request.messages),
                tokens_out=len(words),
            ),
            stop_reason="end_turn",
        )

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name=self.name,
            kind=self.kind,
            model="dev-echo",
            streaming=True,
            tools=False,
            is_cloud=False,
            configured=True,
            needs_key=False,
            detail="Development echo. Reflects input back; it is not a language model.",
        )

    async def health(self) -> tuple[bool, str]:
        return True, "echo provider always available"

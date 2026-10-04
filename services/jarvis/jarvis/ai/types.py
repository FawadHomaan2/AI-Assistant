"""Shared AI types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

Role = Literal["system", "user", "assistant"]


class PrivacyClass(StrEnum):
    """What kind of data a payload contains, which decides whether it may leave.

    Ordered least to most sensitive. SENSITIVE may never reach a cloud provider
    — enforced by raising, not by filtering, because a filter can be mis-tuned
    and a type error cannot.
    """

    PUBLIC = "public"
    METADATA = "metadata"
    CONTENT = "content"
    SENSITIVE = "sensitive"


class JobClass(StrEnum):
    """What the model is being asked to do. Used for per-job provider routing."""

    ROUTER = "router"
    CHAT = "chat"
    PLANNER = "planner"
    CRITIC = "critic"
    SUMMARY = "summary"
    EMBEDDING = "embedding"


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass
class CompletionRequest:
    messages: list[Message]
    job: JobClass = JobClass.CHAT
    privacy: PrivacyClass = PrivacyClass.METADATA
    max_tokens: int | None = None
    temperature: float | None = None
    stop: list[str] = field(default_factory=list)

    def system_prompt(self) -> str | None:
        for m in self.messages:
            if m.role == "system":
                return m.content
        return None

    def without_system(self) -> list[Message]:
        return [m for m in self.messages if m.role != "system"]


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass
class Chunk:
    """One streamed piece.

    `text` is the delta. `done` marks the final chunk, which carries usage and
    the stop reason.
    """

    text: str = ""
    done: bool = False
    usage: Usage | None = None
    stop_reason: str | None = None


@dataclass
class Capabilities:
    name: str
    kind: str
    model: str
    streaming: bool = True
    tools: bool = False
    embeddings: bool = False
    # Whether using this provider sends data off the machine.
    is_cloud: bool = False
    # False when the provider needs a credential it does not have, with `detail`
    # explaining what to do about it.
    configured: bool = True
    detail: str = ""

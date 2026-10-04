"""Voice pipeline types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class VoiceState(StrEnum):
    """What the microphone is doing. Always visible in the interface — there is
    no state in which audio is captured without an on-screen indicator."""

    OFF = "off"
    UNAVAILABLE = "unavailable"
    LISTENING = "listening"  # waiting for the wake word
    RECORDING = "recording"  # capturing an utterance
    THINKING = "thinking"  # the agent is working
    SPEAKING = "speaking"  # text-to-speech is playing


@dataclass
class AudioFormat:
    sample_rate: int = 16_000
    channels: int = 1
    sample_width: int = 2  # 16-bit PCM

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.channels * self.sample_width


@dataclass
class Transcript:
    text: str
    confidence: float = 0.0
    language: str = "en"
    duration_seconds: float = 0.0
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "confidence": round(self.confidence, 3),
            "language": self.language,
            "durationSeconds": round(self.duration_seconds, 2),
            "model": self.model,
        }


@dataclass
class WakeDetection:
    word: str
    confidence: float
    at_seconds: float


@dataclass
class ComponentStatus:
    """Whether one part of the pipeline is usable, and what it needs if not."""

    name: str
    available: bool
    detail: str
    model: str = ""
    #: Download size, when a model is missing and could be fetched.
    download_mb: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": self.available,
            "detail": self.detail,
            "model": self.model,
            "downloadMb": self.download_mb,
        }


@dataclass
class PipelineStatus:
    components: list[ComponentStatus] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return all(c.available for c in self.components)

    @property
    def missing(self) -> list[ComponentStatus]:
        return [c for c in self.components if not c.available]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "components": [c.to_dict() for c in self.components],
            "missing": [c.name for c in self.missing],
        }

"""Text to speech.

Piper runs locally and sounds good enough to listen to all day. Windows SAPI5 is
the fallback: always present, less pleasant, but it means voice output works
before anything is downloaded.

Speech is synthesised **sentence by sentence** and played as it is produced, so
the assistant starts talking while the rest of the answer is still arriving.
Waiting for a full paragraph makes every reply feel slow.
"""

from __future__ import annotations

import abc
import re
import sys
from pathlib import Path

from jarvis.config import paths
from jarvis.util.errors import JarvisError
from jarvis.util.logging import get_logger
from jarvis.voice.types import ComponentStatus

log = get_logger(__name__)

DEFAULT_VOICE = "en_US-lessac-medium"
VOICE_MB = 63

#: Split on sentence ends, keeping the punctuation. Abbreviations would need a
#: real tokeniser; this is deliberately simple and errs toward longer chunks,
#: because splitting mid-sentence sounds worse than a slightly late start.
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


def split_sentences(text: str, *, max_chars: int = 240) -> list[str]:
    """Break text into speakable chunks."""
    cleaned = " ".join(text.split())
    if not cleaned:
        return []
    chunks: list[str] = []
    for sentence in _SENTENCE.split(cleaned):
        if len(sentence) <= max_chars:
            chunks.append(sentence)
            continue
        # A very long sentence is split at commas rather than mid-phrase.
        current = ""
        for part in sentence.split(", "):
            candidate = f"{current}, {part}" if current else part
            if len(candidate) > max_chars and current:
                chunks.append(current)
                current = part
            else:
                current = candidate
        if current:
            chunks.append(current)
    return chunks


class TextToSpeech(abc.ABC):
    @abc.abstractmethod
    def status(self) -> ComponentStatus: ...

    @abc.abstractmethod
    def synthesise(self, text: str) -> bytes:
        """Return WAV audio for one chunk of text."""


class PiperTextToSpeech(TextToSpeech):
    def __init__(self, voice: str = DEFAULT_VOICE) -> None:
        self.voice = voice

    @property
    def voice_path(self) -> Path:
        return paths.models_dir() / "piper" / f"{self.voice}.onnx"

    def status(self) -> ComponentStatus:
        try:
            import piper  # noqa: F401
        except ImportError:
            return ComponentStatus(
                "text-to-speech",
                False,
                "The 'piper-tts' package is not installed. Install the 'voice' extra.",
                model=self.voice,
                download_mb=VOICE_MB,
            )
        if not self.voice_path.exists():
            return ComponentStatus(
                "text-to-speech",
                False,
                f"The {self.voice} voice has not been downloaded yet ({VOICE_MB} MB).",
                model=self.voice,
                download_mb=VOICE_MB,
            )
        return ComponentStatus(
            "text-to-speech", True, f"Local Piper voice {self.voice}.", model=self.voice
        )

    def synthesise(self, text: str) -> bytes:
        import io
        import wave

        try:
            from piper import PiperVoice
        except ImportError as exc:
            raise JarvisError("Text-to-speech needs the 'voice' extra.") from exc

        if not self.voice_path.exists():
            raise JarvisError(f"The {self.voice} voice has not been downloaded.")

        voice = PiperVoice.load(str(self.voice_path))
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            voice.synthesize(text, wav)
        return buffer.getvalue()


def _on_windows() -> bool:
    """Read through a function so mypy keeps both branches type-checked."""
    return sys.platform == "win32"


class Sapi5TextToSpeech(TextToSpeech):
    """Windows' built-in voice. Always available, so speech works immediately."""

    def status(self) -> ComponentStatus:
        if not _on_windows():
            return ComponentStatus("text-to-speech", False, "SAPI5 is only available on Windows.")
        return ComponentStatus(
            "text-to-speech",
            True,
            "Windows' built-in voice. Install a Piper voice for better quality.",
            model="sapi5",
        )

    def synthesise(self, text: str) -> bytes:
        raise JarvisError(
            "SAPI5 speaks directly rather than returning audio; the desktop shell drives it."
        )


class UnavailableTextToSpeech(TextToSpeech):
    def __init__(self, reason: str) -> None:
        self.reason = reason

    def status(self) -> ComponentStatus:
        return ComponentStatus("text-to-speech", False, self.reason)

    def synthesise(self, text: str) -> bytes:
        del text
        raise JarvisError(self.reason)

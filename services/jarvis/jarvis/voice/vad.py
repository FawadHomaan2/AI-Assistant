"""Voice activity detection — where an utterance starts and stops.

Energy-based with a hangover, which is simple, dependency-free and good enough
for a desktop microphone in a quiet room. WebRTC's VAD is better in noise and is
used when `webrtcvad` is installed.

The ring buffer matters more than the detector: it keeps a second and a half of
audio from *before* speech was detected, so the first word of "Jarvis, open
Chrome" is never clipped. Without it the wake word eats the beginning of every
command.
"""

from __future__ import annotations

import array
import math
from collections import deque
from dataclasses import dataclass

from jarvis.voice.types import AudioFormat


@dataclass
class VadSettings:
    #: RMS below this is treated as silence. Tuned for a typical headset.
    energy_threshold: float = 300.0
    #: Speech must persist this long before an utterance starts, which stops a
    #: cough or a door closing from triggering one.
    speech_onset_ms: int = 120
    #: Silence this long ends the utterance. Too short cuts people off
    #: mid-sentence; too long makes the assistant feel sluggish.
    silence_hangover_ms: int = 700
    #: Nothing is transcribed beyond this, so a stuck microphone cannot record
    #: indefinitely.
    max_utterance_seconds: float = 30.0
    #: Audio kept from before detection, so the first word survives.
    preroll_ms: int = 1_500


def frame_energy(frame: bytes) -> float:
    """RMS amplitude of a 16-bit PCM frame."""
    if not frame:
        return 0.0
    samples = array.array("h")
    samples.frombytes(frame[: len(frame) - (len(frame) % 2)])
    if not samples:
        return 0.0
    total = sum(float(s) * float(s) for s in samples)
    return math.sqrt(total / len(samples))


class RingBuffer:
    """Fixed-duration audio history."""

    def __init__(self, fmt: AudioFormat, milliseconds: int) -> None:
        self.fmt = fmt
        self.capacity = int(fmt.bytes_per_second * milliseconds / 1000)
        self._frames: deque[bytes] = deque()
        self._size = 0

    def push(self, frame: bytes) -> None:
        self._frames.append(frame)
        self._size += len(frame)
        while self._size > self.capacity and self._frames:
            self._size -= len(self._frames.popleft())

    def drain(self) -> bytes:
        data = b"".join(self._frames)
        self._frames.clear()
        self._size = 0
        return data

    def __len__(self) -> int:
        return self._size


class VoiceActivityDetector:
    """Decides when an utterance begins and ends."""

    def __init__(self, fmt: AudioFormat | None = None, settings: VadSettings | None = None) -> None:
        self.fmt = fmt or AudioFormat()
        self.settings = settings or VadSettings()
        self.preroll = RingBuffer(self.fmt, self.settings.preroll_ms)
        self._speaking = False
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._utterance: list[bytes] = []
        self._utterance_ms = 0.0

    @property
    def speaking(self) -> bool:
        return self._speaking

    def _frame_ms(self, frame: bytes) -> float:
        return len(frame) / self.fmt.bytes_per_second * 1000

    def reset(self) -> None:
        self._speaking = False
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._utterance.clear()
        self._utterance_ms = 0.0

    def push(self, frame: bytes) -> bytes | None:
        """Feed one frame. Returns the complete utterance when speech ends."""
        duration = self._frame_ms(frame)
        loud = frame_energy(frame) >= self.settings.energy_threshold

        if not self._speaking:
            self.preroll.push(frame)
            if loud:
                self._speech_ms += duration
                if self._speech_ms >= self.settings.speech_onset_ms:
                    # Start with the pre-roll so the first word is included.
                    self._speaking = True
                    preroll = self.preroll.drain()
                    self._utterance = [preroll]
                    # Measured, not assumed: the buffer may not be full yet, and
                    # overcounting here would trip the maximum-length cap early.
                    self._utterance_ms = self._frame_ms(preroll)
                    self._silence_ms = 0.0
            else:
                self._speech_ms = 0.0
            return None

        self._utterance.append(frame)
        self._utterance_ms += duration

        if loud:
            self._silence_ms = 0.0
        else:
            self._silence_ms += duration
            if self._silence_ms >= self.settings.silence_hangover_ms:
                return self._finish()

        if self._utterance_ms >= self.settings.max_utterance_seconds * 1000:
            return self._finish()
        return None

    def _finish(self) -> bytes:
        audio = b"".join(self._utterance)
        self.reset()
        return audio

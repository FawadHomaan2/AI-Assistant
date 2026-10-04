"""The voice pipeline.

    microphone → wake word (or push-to-talk) → VAD → speech-to-text
              → the agent → text-to-speech → speaker
                                  ↑
                             barge-in: the microphone stays open while
                             speaking, so you can interrupt mid-sentence

Three commitments this implements rather than merely describes:

**Barge-in is mandatory.** An assistant you cannot interrupt is unusable. The
microphone stays live during playback and sustained speech cancels the current
utterance immediately.

**The microphone state is always visible.** Every transition goes through
`_set_state`, which notifies the interface. There is no path that captures audio
without the indicator changing.

**Degradation is explicit.** If a model is missing the pipeline says which one
and how big it is. It never silently falls back or pretends to be listening.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from jarvis.util.errors import JarvisError
from jarvis.util.logging import get_logger
from jarvis.voice.stt import SpeechToText
from jarvis.voice.tts import TextToSpeech, split_sentences
from jarvis.voice.types import (
    AudioFormat,
    ComponentStatus,
    PipelineStatus,
    Transcript,
    VoiceState,
)
from jarvis.voice.vad import VoiceActivityDetector
from jarvis.voice.wake import WakeWordDetector

log = get_logger(__name__)

#: While speaking, this much continuous speech from the user cancels playback.
#: Long enough not to trigger on the assistant's own audio leaking back through
#: the microphone; short enough to feel immediate.
BARGE_IN_MS = 350


@dataclass
class VoiceSettings:
    enabled: bool = False
    wake_word_enabled: bool = True
    push_to_talk: bool = True
    speak_responses: bool = True


@dataclass
class PipelineEvent:
    type: str
    data: dict[str, object] = field(default_factory=dict)


class VoicePipeline:
    """Drives one microphone through to one spoken answer."""

    def __init__(
        self,
        stt: SpeechToText,
        tts: TextToSpeech,
        wake: WakeWordDetector,
        *,
        fmt: AudioFormat | None = None,
        settings: VoiceSettings | None = None,
        on_event: Callable[[PipelineEvent], Awaitable[None]] | None = None,
    ) -> None:
        self.stt = stt
        self.tts = tts
        self.wake = wake
        self.fmt = fmt or AudioFormat()
        self.settings = settings or VoiceSettings()
        self.vad = VoiceActivityDetector(self.fmt)
        self._on_event = on_event
        self._state = VoiceState.OFF
        self._speaking_task: asyncio.Task[None] | None = None
        self._barge_in_ms = 0.0

    # ── state, always visible ────────────────────────────────────────────
    @property
    def state(self) -> VoiceState:
        return self._state

    async def _set_state(self, state: VoiceState) -> None:
        """The single place the microphone state changes.

        Routing every transition through here is what guarantees the interface
        indicator cannot drift out of sync with what the microphone is doing.
        """
        if state == self._state:
            return
        self._state = state
        log.debug("voice state", state=state.value)
        await self.emit(PipelineEvent("voice.state", {"state": state.value}))

    def set_event_sink(self, sink: Callable[[PipelineEvent], Awaitable[None]] | None) -> None:
        """Direct pipeline events at a connected interface.

        The pipeline is built once at start-up but the interface comes and goes,
        so the sink is installed when a WebSocket connects and cleared when it
        leaves — the same arrangement the consent broker uses for prompts.
        Clearing matters: emitting into a closed socket raises inside whatever
        happened to be driving the state machine.
        """
        self._on_event = sink

    async def emit(self, event: PipelineEvent) -> None:
        """Publish a pipeline event.

        Public because capture publishes through it too: a frame rejected by the
        microphone belongs on the same stream the interface already watches, not
        on a second one it would have to learn about.
        """
        if self._on_event is not None:
            await self._on_event(event)

    # ── readiness ────────────────────────────────────────────────────────
    def status(self) -> PipelineStatus:
        components: list[ComponentStatus] = [self.stt.status(), self.tts.status()]
        if self.settings.wake_word_enabled:
            components.append(self.wake.status())
        else:
            components.append(
                ComponentStatus("wake-word", True, "Disabled; push-to-talk is used instead.")
            )
        return PipelineStatus(components)

    def unavailable_reason(self) -> str:
        """One sentence naming what is missing, or empty when ready."""
        status = self.status()
        if status.ready:
            return ""
        missing = status.missing
        parts = [f"{c.name} ({c.detail})" for c in missing]
        total = sum(c.download_mb for c in missing)
        return (
            "Voice is not ready: "
            + "; ".join(parts)
            + (f" About {total} MB would need downloading." if total else "")
        )

    # ── capture ──────────────────────────────────────────────────────────
    async def start_listening(self) -> None:
        """Begin waiting for the wake word."""
        if (reason := self.unavailable_reason()) and self.settings.wake_word_enabled:
            await self._set_state(VoiceState.UNAVAILABLE)
            raise JarvisError(reason)
        self.vad.reset()
        self.wake.reset()
        await self._set_state(VoiceState.LISTENING)

    async def start_recording(self) -> None:
        """Push-to-talk: skip the wake word and capture immediately."""
        self.vad.reset()
        await self._set_state(VoiceState.RECORDING)

    async def stop(self) -> None:
        await self.cancel_speech()
        self.vad.reset()
        await self._set_state(VoiceState.OFF)

    async def push_audio(self, frame: bytes) -> Transcript | None:
        """Feed one frame of microphone audio.

        Returns a transcript once a complete utterance has been captured and
        recognised. Everything else returns None.
        """
        # Barge-in: while the assistant is speaking, sustained speech from the
        # user cancels it. Checked first so an interruption is never missed.
        if self._state is VoiceState.SPEAKING:
            if await self._check_barge_in(frame):
                await self.start_recording()
            return None

        if self._state is VoiceState.LISTENING:
            if self.wake.push(frame) is not None:
                await self.emit(PipelineEvent("voice.wake", {"word": self.wake.status().model}))
                await self.start_recording()
            return None

        if self._state is not VoiceState.RECORDING:
            return None

        utterance = self.vad.push(frame)
        if utterance is None:
            return None

        await self._set_state(VoiceState.THINKING)
        try:
            transcript = self.stt.transcribe(utterance, self.fmt)
        except JarvisError:
            await self._set_state(VoiceState.LISTENING)
            raise

        if not transcript.text.strip():
            # Silence or noise. Say nothing rather than sending an empty turn.
            log.info("empty transcript, returning to listening")
            await self._set_state(
                VoiceState.LISTENING if self.settings.wake_word_enabled else VoiceState.OFF
            )
            return None

        await self.emit(PipelineEvent("voice.transcript", transcript.to_dict()))
        return transcript

    async def _check_barge_in(self, frame: bytes) -> bool:
        from jarvis.voice.vad import frame_energy

        if frame_energy(frame) < self.vad.settings.energy_threshold:
            self._barge_in_ms = 0.0
            return False
        self._barge_in_ms += len(frame) / self.fmt.bytes_per_second * 1000
        if self._barge_in_ms < BARGE_IN_MS:
            return False
        log.info("barge-in: user interrupted")
        self._barge_in_ms = 0.0
        await self.cancel_speech()
        await self.emit(PipelineEvent("voice.interrupted", {}))
        return True

    # ── playback ─────────────────────────────────────────────────────────
    async def speak(self, text: str) -> None:
        """Speak an answer, one sentence at a time so it starts sooner."""
        if not self.settings.speak_responses or not text.strip():
            return
        status = self.tts.status()
        if not status.available:
            await self.emit(
                PipelineEvent(
                    "voice.unavailable", {"component": "text-to-speech", "detail": status.detail}
                )
            )
            return

        await self._set_state(VoiceState.SPEAKING)
        self._barge_in_ms = 0.0
        chunks = split_sentences(text)

        async def play() -> None:
            for chunk in chunks:
                audio = await asyncio.to_thread(self.tts.synthesise, chunk)
                await self.emit(PipelineEvent("voice.audio", {"text": chunk, "bytes": len(audio)}))

        self._speaking_task = asyncio.create_task(play())
        try:
            await self._speaking_task
        except asyncio.CancelledError:
            log.info("speech cancelled")
        finally:
            self._speaking_task = None
            if self._state is VoiceState.SPEAKING:
                await self._set_state(
                    VoiceState.LISTENING if self.settings.wake_word_enabled else VoiceState.OFF
                )

    async def cancel_speech(self) -> None:
        """Stop talking immediately. Used by barge-in and the emergency stop."""
        task = self._speaking_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._speaking_task = None

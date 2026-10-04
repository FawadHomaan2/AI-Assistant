"""Microphone capture — the half of the voice pipeline that was missing.

`VoicePipeline.push_audio` has always known what to do with a frame of audio.
Nothing ever gave it one: no module imported `sounddevice`, and no code path
read a microphone. The pipeline was complete and unreachable, which is why
`docs/PHASES.md` recorded Phase 6 as "done (pipeline); audio capture pending".
This is that missing piece.

Audio arrives on a device callback thread and is consumed by the asyncio loop,
so the two are joined by a bounded queue:

    device callback thread          asyncio loop
    ──────────────────────          ────────────
    RawInputStream callback   →   queue   →   push_audio → wake word → VAD → STT

**The queue is bounded and drops the oldest frame when full.** Inference that
falls behind real time must not grow a queue without limit; a dropped frame
costs 80 ms of audio, while an unbounded queue eventually costs the process.
Drops are counted and reported rather than hidden, because a steady drop rate
means this machine cannot run the wake word in real time and the user needs to
be told that rather than left wondering why it misses them.

**The source is injectable.** `SoundDeviceSource` is the real microphone;
`FrameListSource` replays fixed frames. This is not only for tests — it is the
reason the pipeline can be exercised at all on a machine with no audio device,
which is every machine this repository has been developed on.

**Nothing here decides whether listening is allowed.** Capture is started by the
transport layer only after the microphone scope is granted, and the pipeline's
state machine is what the interface displays. This module does not touch either.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from jarvis.util.errors import JarvisError
from jarvis.util.logging import get_logger
from jarvis.voice.pipeline import PipelineEvent, VoicePipeline
from jarvis.voice.types import AudioFormat, Transcript

log = get_logger(__name__)


class CaptureError(JarvisError):
    """Capture could not start, with a reason fit to show the user."""

    code = "jarvis.voice.capture"
    http_status = 503


#: 80 ms at 16 kHz. openWakeWord scores exactly this many samples per call, so
#: reading the device in the same size avoids re-chunking audio on the way in.
FRAME_SAMPLES = 1280

#: About two seconds of audio. Deep enough to ride out a garbage-collection
#: pause or a slow STT call, shallow enough that a backlog is noticed quickly.
QUEUE_FRAMES = 25

#: Consecutive device read failures tolerated before capture gives up. A
#: microphone being unplugged mid-session is the common cause.
MAX_READ_ERRORS = 5


class AudioSource(abc.ABC):
    """One microphone, or something standing in for one."""

    @abc.abstractmethod
    def availability(self) -> tuple[bool, str]:
        """Whether this source can be opened, and if not, what is missing."""

    @abc.abstractmethod
    def open(self, fmt: AudioFormat, on_frame: Callable[[bytes], None]) -> None:
        """Start delivering frames of `FRAME_SAMPLES` samples to `on_frame`.

        `on_frame` may be called from another thread.
        """

    @abc.abstractmethod
    def close(self) -> None:
        """Stop delivering frames. Must be safe to call when not open."""


class SoundDeviceSource(AudioSource):
    """The real microphone, via PortAudio through `sounddevice`."""

    def __init__(self, device: int | str | None = None) -> None:
        self.device = device
        self._stream: object | None = None

    def availability(self) -> tuple[bool, str]:
        try:
            import sounddevice
        except ImportError:
            return False, (
                "The 'sounddevice' package is not installed, so Jarvis cannot "
                "open a microphone. Install the 'voice' extra."
            )
        except OSError as exc:
            # `import sounddevice` loads PortAudio, and raises OSError rather
            # than ImportError when the shared library is missing. Catching only
            # ImportError meant this propagated out of a status check and took
            # /voice/status with it, on every Linux machine without
            # libportaudio2 — which includes a stock CI runner.
            return False, (
                f"The audio library could not be loaded ({exc}). On Linux install "
                f"libportaudio2; on Windows this ships with the installer."
            )
        try:
            inputs = [d for d in sounddevice.query_devices() if d.get("max_input_channels", 0) > 0]
        except Exception as exc:
            # PortAudio raises for a missing or broken audio subsystem, which is
            # normal in a container and on a machine with no sound card.
            return False, f"No audio system Jarvis can use: {exc}"
        if not inputs:
            return False, "No microphone is connected."
        return True, f"{len(inputs)} input device(s) available."

    def open(self, fmt: AudioFormat, on_frame: Callable[[bytes], None]) -> None:
        try:
            import sounddevice
        except (ImportError, OSError) as exc:
            raise CaptureError(f"The audio library could not be loaded: {exc}") from exc

        # `indata` is a CFFI buffer from PortAudio. `collections.abc.Buffer`
        # would type it, but that only exists from Python 3.12 and the core
        # targets 3.11.
        def callback(indata: Any, frames: int, time: object, status: object) -> None:
            del frames, time
            if status:
                # Overflows are reported, not silently swallowed: a stream that
                # constantly overflows is losing audio the user cannot hear
                # being lost.
                log.warning("audio input status", status=str(status))
            on_frame(bytes(indata))

        stream = sounddevice.RawInputStream(
            samplerate=fmt.sample_rate,
            channels=fmt.channels,
            dtype="int16",
            blocksize=FRAME_SAMPLES,
            device=self.device,
            callback=callback,
        )
        stream.start()
        self._stream = stream
        log.info("microphone opened", rate=fmt.sample_rate, block=FRAME_SAMPLES)

    def close(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        with contextlib.suppress(Exception):
            stream.stop()  # type: ignore[attr-defined]
            stream.close()  # type: ignore[attr-defined]
        log.info("microphone closed")


class FrameListSource(AudioSource):
    """Replays a fixed list of frames, then stops.

    Used to drive the pipeline where no microphone exists — in tests, and on any
    developer machine without an audio device.
    """

    def __init__(self, frames: Sequence[bytes], *, reason: str = "") -> None:
        self.frames = list(frames)
        self.reason = reason
        self._thread: object | None = None
        self._stop = False

    def availability(self) -> tuple[bool, str]:
        if self.reason:
            return False, self.reason
        return True, f"{len(self.frames)} recorded frame(s)."

    def open(self, fmt: AudioFormat, on_frame: Callable[[bytes], None]) -> None:
        del fmt
        self._stop = False
        for frame in self.frames:
            if self._stop:
                return
            on_frame(frame)

    def close(self) -> None:
        self._stop = True


class UnavailableSource(AudioSource):
    """No microphone. Push-to-talk still works, and says so."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def availability(self) -> tuple[bool, str]:
        return False, self.reason

    def open(self, fmt: AudioFormat, on_frame: Callable[[bytes], None]) -> None:
        del fmt, on_frame
        raise RuntimeError(self.reason)

    def close(self) -> None:
        return None


def default_source() -> AudioSource:
    """A real microphone when one can be opened, otherwise one that explains."""
    source = SoundDeviceSource()
    ok, reason = source.availability()
    return source if ok else UnavailableSource(reason)


class MicrophoneCapture:
    """Feeds a microphone into a `VoicePipeline` until told to stop."""

    def __init__(
        self,
        pipeline: VoicePipeline,
        source: AudioSource | None = None,
        *,
        on_transcript: Callable[[Transcript], Awaitable[None]] | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.source = source if source is not None else default_source()
        self._on_transcript = on_transcript
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=QUEUE_FRAMES)
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.dropped = 0
        self.frames = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def set_transcript_handler(
        self, handler: Callable[[Transcript], Awaitable[None]] | None
    ) -> None:
        """Set what happens to a finished utterance.

        Installed by the transport when an interface connects, because running a
        turn means streaming its events somewhere, and that somewhere is the
        connection. With no handler a transcript is recognised and dropped,
        which is the right behaviour for a capture running with nobody attached.
        """
        self._on_transcript = handler

    def availability(self) -> tuple[bool, str]:
        return self.source.availability()

    # ── the thread boundary ──────────────────────────────────────────────
    def _offer(self, frame: bytes) -> None:
        """Called from the device thread. Must not block and must not raise."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._enqueue, frame)

    def _enqueue(self, frame: bytes) -> None:
        """Called on the loop thread. Drops the oldest frame when full."""
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            self.dropped += 1
            if self.dropped % QUEUE_FRAMES == 1:
                # Logged on the first drop and periodically after, not every
                # frame: a machine that cannot keep up would flood the log.
                log.warning("audio frames dropped", total=self.dropped)
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(frame)

    # ── lifecycle ────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Open the microphone and begin waiting for the wake word."""
        if self.running:
            return
        ok, reason = self.source.availability()
        if not ok:
            raise CaptureError(reason)

        self._loop = asyncio.get_running_loop()
        self.dropped = 0
        self.frames = 0
        # Listening state first: the indicator must be on before any audio is
        # captured, never after.
        await self.pipeline.start_listening()
        self._task = asyncio.create_task(self._consume())
        try:
            await asyncio.to_thread(self.source.open, self.pipeline.fmt, self._offer)
        except Exception as exc:
            await self.stop()
            raise CaptureError(f"Could not open the microphone: {exc}") from exc

    async def stop(self) -> None:
        task, self._task = self._task, None
        with contextlib.suppress(Exception):
            self.source.close()
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        with contextlib.suppress(Exception):
            await self.pipeline.stop()
        while not self._queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()

    # ── the consumer ─────────────────────────────────────────────────────
    async def _consume(self) -> None:
        errors = 0
        while True:
            frame = await self._queue.get()
            self.frames += 1
            try:
                transcript = await self.pipeline.push_audio(frame)
                errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # One bad frame, or a model that failed to load, must not kill
                # capture outright — but a source failing repeatedly is not
                # going to recover by being asked again.
                errors += 1
                log.warning("frame rejected", error=str(exc), consecutive=errors)
                await self.pipeline.emit(PipelineEvent("voice.error", {"detail": str(exc)}))
                if errors >= MAX_READ_ERRORS:
                    log.error("giving up on capture", consecutive=errors)
                    return
                continue

            if transcript is not None and self._on_transcript is not None:
                await self._on_transcript(transcript)

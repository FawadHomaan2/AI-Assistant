"""Microphone capture — the piece that connects a device to the pipeline.

The pipeline's own logic is covered in `test_voice.py`. What is covered here is
the part that was missing entirely until now: pulling frames off a device
callback thread, surviving a device that fails, and never growing a queue
without limit. None of it needs a microphone, which is the point of the
injectable source.
"""

from __future__ import annotations

import array
import asyncio

import pytest

from jarvis.voice.capture import (
    QUEUE_FRAMES,
    AudioSource,
    CaptureError,
    FrameListSource,
    MicrophoneCapture,
    SoundDeviceSource,
    UnavailableSource,
    default_source,
)
from jarvis.voice.types import AudioFormat, Transcript, VoiceState
from tests.test_voice import FakeStt, FakeWake, build

FMT = AudioFormat()


def frame(amplitude: int = 0, samples: int = 1280) -> bytes:
    return array.array("h", [amplitude] * samples).tobytes()


# ── sources ──────────────────────────────────────────────────────────────
class TestSources:
    def test_frame_list_source_reports_its_frames(self) -> None:
        source = FrameListSource([frame(), frame()])
        ok, detail = source.availability()
        assert ok
        assert "2 recorded frame(s)" in detail

    def test_frame_list_source_delivers_every_frame(self) -> None:
        got: list[bytes] = []
        source = FrameListSource([frame(1), frame(2), frame(3)])
        source.open(FMT, got.append)
        assert len(got) == 3

    def test_closing_stops_delivery_midway(self) -> None:
        got: list[bytes] = []
        source = FrameListSource([frame()] * 10)

        def on_frame(f: bytes) -> None:
            got.append(f)
            if len(got) == 3:
                source.close()

        source.open(FMT, on_frame)
        # The stop flag is checked at the top of each iteration, so the frame
        # that triggered the close is the last one delivered.
        assert len(got) == 3

    def test_unavailable_source_explains_and_refuses(self) -> None:
        source = UnavailableSource("no microphone is connected")
        ok, detail = source.availability()
        assert not ok
        assert detail == "no microphone is connected"
        with pytest.raises(RuntimeError, match="no microphone"):
            source.open(FMT, lambda _f: None)

    def test_a_missing_audio_library_is_reported_not_raised(self) -> None:
        """`import sounddevice` loads PortAudio and raises OSError, not
        ImportError, when the shared library is absent. Catching only
        ImportError let that escape a status check and take /voice/status with
        it on any Linux box without libportaudio2."""
        import builtins

        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kw: object) -> object:
            if name == "sounddevice":
                raise OSError("PortAudio library not found")
            return real_import(name, *args, **kw)  # type: ignore[arg-type]

        source = SoundDeviceSource()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(builtins, "__import__", fake_import)
            ok, detail = source.availability()
        assert not ok
        assert "PortAudio" in detail
        assert "libportaudio2" in detail

    def test_default_source_never_raises_on_a_machine_with_no_audio(self) -> None:
        # This container has no audio device. The point is that asking for a
        # source still returns one that explains itself, rather than throwing
        # during start-up.
        source = default_source()
        ok, detail = source.availability()
        assert isinstance(ok, bool)
        assert detail
        if not ok:
            assert isinstance(source, UnavailableSource)


# ── starting and stopping ────────────────────────────────────────────────
class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_refuses_without_a_microphone_and_says_why(self) -> None:
        pipeline, _events = build()
        capture = MicrophoneCapture(pipeline, UnavailableSource("the microphone is unplugged"))
        with pytest.raises(CaptureError, match="unplugged"):
            await capture.start()
        assert not capture.running

    @pytest.mark.asyncio
    async def test_listening_state_is_set_before_any_audio_is_captured(self) -> None:
        """The indicator must be on before the first frame, never after."""
        seen: list[str] = []
        pipeline, _events = build(wake=FakeWake())

        class Recording(AudioSource):
            def availability(self) -> tuple[bool, str]:
                return True, "ok"

            def open(self, fmt: AudioFormat, on_frame) -> None:  # type: ignore[no-untyped-def]
                del fmt
                seen.append(pipeline.state.value)
                on_frame(frame())

            def close(self) -> None:
                return None

        capture = MicrophoneCapture(pipeline, Recording())
        await capture.start()
        await capture.stop()
        assert seen == [VoiceState.LISTENING.value]

    @pytest.mark.asyncio
    async def test_stop_is_idempotent_and_leaves_the_pipeline_off(self) -> None:
        pipeline, _events = build()
        capture = MicrophoneCapture(pipeline, FrameListSource([frame()]))
        await capture.start()
        await capture.stop()
        await capture.stop()
        assert not capture.running
        assert pipeline.state is VoiceState.OFF


# ── the queue ────────────────────────────────────────────────────────────
class TestBackpressure:
    @pytest.mark.asyncio
    async def test_a_full_queue_drops_the_oldest_frame_and_counts_it(self) -> None:
        """A machine too slow for real-time inference must not grow a queue.

        Dropping is the designed behaviour; what matters is that it is counted,
        so a steady drop rate can be reported instead of silently losing audio.
        """
        pipeline, _events = build()
        capture = MicrophoneCapture(pipeline, FrameListSource([]))
        capture._loop = asyncio.get_running_loop()

        for i in range(QUEUE_FRAMES + 5):
            capture._enqueue(frame(i))

        assert capture.dropped == 5
        assert capture._queue.qsize() == QUEUE_FRAMES

    @pytest.mark.asyncio
    async def test_frames_offered_after_close_are_discarded(self) -> None:
        pipeline, _events = build()
        capture = MicrophoneCapture(pipeline, FrameListSource([]))
        # No loop assigned: a device thread still delivering after shutdown must
        # not raise into PortAudio's callback.
        capture._offer(frame())
        assert capture._queue.qsize() == 0


# ── failure ──────────────────────────────────────────────────────────────
class TestFailure:
    @pytest.mark.asyncio
    async def test_a_transcript_is_handed_on(self) -> None:
        got: list[Transcript] = []

        async def on_transcript(t: Transcript) -> None:
            got.append(t)

        # Wake fires on the first frame, then speech then silence closes the
        # utterance, which is what produces a transcript.
        loud = array.array("h", [8000] * 1280).tobytes()
        quiet = array.array("h", [2] * 1280).tobytes()
        frames = [loud] + [loud] * 30 + [quiet] * 40

        pipeline, _events = build(stt=FakeStt("open chrome"), wake=FakeWake(fire_after=1))
        capture = MicrophoneCapture(pipeline, FrameListSource(frames), on_transcript=on_transcript)
        await capture.start()
        for _ in range(200):
            if got:
                break
            await asyncio.sleep(0.005)
        await capture.stop()
        assert got, "expected a transcript once an utterance completed"
        assert got[0].text == "open chrome"

    @pytest.mark.asyncio
    async def test_capture_survives_one_bad_frame(self) -> None:
        pipeline, events = build()
        calls = {"n": 0}
        original = pipeline.push_audio

        async def flaky(f: bytes) -> Transcript | None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("decoder hiccup")
            return await original(f)

        pipeline.push_audio = flaky  # type: ignore[method-assign]
        capture = MicrophoneCapture(pipeline, FrameListSource([frame()] * 4))
        await capture.start()
        for _ in range(100):
            if calls["n"] >= 3:
                break
            await asyncio.sleep(0.005)
        await capture.stop()

        assert calls["n"] >= 3, "capture should have carried on after one failure"
        assert any(e.type == "voice.error" for e in events)

    @pytest.mark.asyncio
    async def test_capture_gives_up_when_every_frame_fails(self) -> None:
        """A source that keeps failing will not recover by being asked again."""
        pipeline, _events = build()

        async def always_fails(f: bytes) -> Transcript | None:
            del f
            raise RuntimeError("the model is gone")

        pipeline.push_audio = always_fails  # type: ignore[method-assign]
        capture = MicrophoneCapture(pipeline, FrameListSource([frame()] * 50))
        await capture.start()
        task = capture._task
        assert task is not None
        await asyncio.wait_for(task, timeout=5)
        assert task.done()
        await capture.stop()

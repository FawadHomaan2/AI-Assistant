"""Phase 6: the voice pipeline.

Audio capture and the models need hardware and downloads, so what is verified
here is the logic that decides *when* to listen, record, transcribe and stop —
which is where the behaviour people notice lives.
"""

from __future__ import annotations

import array
import math

import pytest

from jarvis.util.errors import JarvisError
from jarvis.voice.pipeline import BARGE_IN_MS, PipelineEvent, VoicePipeline, VoiceSettings
from jarvis.voice.stt import MODELS, SpeechToText, WhisperSpeechToText
from jarvis.voice.tts import TextToSpeech, split_sentences
from jarvis.voice.types import AudioFormat, ComponentStatus, Transcript, VoiceState
from jarvis.voice.vad import VoiceActivityDetector, frame_energy
from jarvis.voice.wake import BUILT_IN, DEFAULT_WAKE_WORD, OpenWakeWordDetector, WakeWordDetector

FMT = AudioFormat()
FRAME_MS = 20
FRAME_SAMPLES = int(FMT.sample_rate * FRAME_MS / 1000)


def tone(amplitude: int) -> bytes:
    """One frame of a 440 Hz sine at the given amplitude."""
    return array.array(
        "h",
        [
            int(amplitude * math.sin(2 * math.pi * 440 * i / FMT.sample_rate))
            for i in range(FRAME_SAMPLES)
        ],
    ).tobytes()


SILENCE = tone(5)
SPEECH = tone(8000)


# ── fakes ────────────────────────────────────────────────────────────────
class FakeStt(SpeechToText):
    def __init__(self, text: str = "open chrome", available: bool = True) -> None:
        self.text = text
        self.available = available
        self.calls = 0

    def status(self) -> ComponentStatus:
        return ComponentStatus("speech-to-text", self.available, "fake")

    def transcribe(self, audio: bytes, fmt: AudioFormat) -> Transcript:
        self.calls += 1
        return Transcript(text=self.text, duration_seconds=len(audio) / fmt.bytes_per_second)


class FakeTts(TextToSpeech):
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.spoken: list[str] = []

    def status(self) -> ComponentStatus:
        return ComponentStatus("text-to-speech", self.available, "fake")

    def synthesise(self, text: str) -> bytes:
        self.spoken.append(text)
        return b"\x00" * 100


class FakeWake(WakeWordDetector):
    def __init__(self, fire_after: int | None = None) -> None:
        self.fire_after = fire_after
        self.frames = 0

    def status(self) -> ComponentStatus:
        return ComponentStatus("wake-word", True, "fake", model="hey_jarvis")

    def push(self, frame):  # type: ignore[no-untyped-def]
        from jarvis.voice.types import WakeDetection

        self.frames += 1
        if self.fire_after is not None and self.frames >= self.fire_after:
            self.frames = 0
            return WakeDetection("hey_jarvis", 0.9, 0.0)
        return None

    def reset(self) -> None:
        self.frames = 0


def build(**kw) -> tuple[VoicePipeline, list[PipelineEvent]]:
    events: list[PipelineEvent] = []

    async def record(event: PipelineEvent) -> None:
        events.append(event)

    pipeline = VoicePipeline(
        kw.pop("stt", FakeStt()),
        kw.pop("tts", FakeTts()),
        kw.pop("wake", FakeWake()),
        settings=kw.pop("settings", VoiceSettings(enabled=True)),
        on_event=record,
    )
    return pipeline, events


# ── VAD ──────────────────────────────────────────────────────────────────
class TestVoiceActivityDetection:
    def test_energy_separates_speech_from_silence(self) -> None:
        assert frame_energy(SILENCE) < 100
        assert frame_energy(SPEECH) > 1000
        assert frame_energy(b"") == 0.0

    def test_an_utterance_ends_after_the_hangover(self) -> None:
        vad = VoiceActivityDetector(FMT)
        for _ in range(50):
            assert vad.push(SPEECH) is None
        assert vad.speaking

        result = None
        for _ in range(60):
            result = vad.push(SILENCE)
            if result:
                break
        assert result is not None
        assert not vad.speaking

    # Without the pre-roll the wake word eats the first word of every command.
    def test_the_preroll_keeps_audio_from_before_detection(self) -> None:
        vad = VoiceActivityDetector(FMT)
        for _ in range(30):  # 600ms of lead-in
            vad.push(SILENCE)
        for _ in range(50):
            vad.push(SPEECH)
        utterance = None
        for _ in range(60):
            utterance = vad.push(SILENCE)
            if utterance:
                break
        assert utterance is not None
        seconds = len(utterance) / FMT.bytes_per_second
        assert seconds > 1.0, "the captured audio must include the lead-in"

    def test_a_brief_noise_does_not_start_an_utterance(self) -> None:
        """A cough or a door closing should not trigger recording."""
        vad = VoiceActivityDetector(FMT)
        for _ in range(2):  # 40ms, below the 120ms onset
            vad.push(SPEECH)
        for _ in range(5):
            vad.push(SILENCE)
        assert not vad.speaking

    def test_a_stuck_microphone_cannot_record_forever(self) -> None:
        vad = VoiceActivityDetector(FMT)
        vad.settings.max_utterance_seconds = 0.5
        result = None
        for _ in range(200):
            result = vad.push(SPEECH)
            if result:
                break
        assert result is not None, "the maximum length must end the utterance"

    def test_the_ring_buffer_is_bounded(self) -> None:
        from jarvis.voice.vad import RingBuffer

        buffer = RingBuffer(FMT, milliseconds=100)
        for _ in range(100):
            buffer.push(SPEECH)
        assert len(buffer) <= buffer.capacity


# ── sentence splitting ───────────────────────────────────────────────────
class TestSpeechChunking:
    """Speech starts before the full answer exists, so it must chunk."""

    def test_splits_on_sentence_ends(self) -> None:
        assert split_sentences("One. Two! Three?") == ["One.", "Two!", "Three?"]

    def test_a_long_sentence_is_split_at_commas(self) -> None:
        text = "A clause here, another clause there, a third one, and a fourth."
        chunks = split_sentences(text, max_chars=30)
        assert len(chunks) > 1
        assert all(len(c) <= 45 for c in chunks)

    def test_empty_text_produces_nothing(self) -> None:
        assert split_sentences("") == []
        assert split_sentences("   ") == []

    def test_whitespace_is_normalised(self) -> None:
        assert split_sentences("Hello    there.\n\nAgain.") == ["Hello there.", "Again."]


# ── pipeline ─────────────────────────────────────────────────────────────
class TestPipelineStates:
    """Every transition is observable; the indicator can never drift."""

    async def test_state_changes_are_announced(self) -> None:
        pipeline, events = build()
        await pipeline.start_listening()
        assert pipeline.state is VoiceState.LISTENING
        assert any(e.type == "voice.state" for e in events)

    async def test_the_wake_word_starts_recording(self) -> None:
        pipeline, events = build(wake=FakeWake(fire_after=3))
        await pipeline.start_listening()
        for _ in range(3):
            await pipeline.push_audio(SILENCE)
        assert pipeline.state is VoiceState.RECORDING
        assert any(e.type == "voice.wake" for e in events)

    async def test_push_to_talk_skips_the_wake_word(self) -> None:
        pipeline, _ = build()
        await pipeline.start_recording()
        assert pipeline.state is VoiceState.RECORDING

    async def test_a_complete_utterance_is_transcribed(self) -> None:
        stt = FakeStt("open chrome")
        pipeline, events = build(stt=stt)
        await pipeline.start_recording()
        for _ in range(50):
            await pipeline.push_audio(SPEECH)
        transcript = None
        for _ in range(60):
            transcript = await pipeline.push_audio(SILENCE)
            if transcript:
                break
        assert transcript is not None
        assert transcript.text == "open chrome"
        assert stt.calls == 1
        assert any(e.type == "voice.transcript" for e in events)

    # Silence must not become an empty turn sent to the agent.
    async def test_an_empty_transcript_does_not_produce_a_turn(self) -> None:
        pipeline, _ = build(stt=FakeStt(""))
        await pipeline.start_recording()
        for _ in range(50):
            await pipeline.push_audio(SPEECH)
        result = None
        for _ in range(60):
            result = await pipeline.push_audio(SILENCE)
            if result:
                break
        assert result is None
        assert pipeline.state is VoiceState.LISTENING

    async def test_audio_is_ignored_when_off(self) -> None:
        pipeline, _ = build()
        assert pipeline.state is VoiceState.OFF
        assert await pipeline.push_audio(SPEECH) is None


class TestBargeIn:
    """An assistant you cannot interrupt is unusable."""

    async def test_sustained_speech_cancels_playback(self) -> None:
        pipeline, events = build()
        await pipeline._set_state(VoiceState.SPEAKING)

        frames = int(BARGE_IN_MS / FRAME_MS) + 2
        for _ in range(frames):
            await pipeline.push_audio(SPEECH)

        assert pipeline.state is VoiceState.RECORDING
        assert any(e.type == "voice.interrupted" for e in events)

    async def test_a_short_burst_does_not_interrupt(self) -> None:
        """The assistant's own audio leaking back must not cancel it."""
        pipeline, events = build()
        await pipeline._set_state(VoiceState.SPEAKING)
        for _ in range(2):  # 40ms, far below the threshold
            await pipeline.push_audio(SPEECH)
        assert pipeline.state is VoiceState.SPEAKING
        assert not any(e.type == "voice.interrupted" for e in events)

    async def test_silence_resets_the_barge_in_counter(self) -> None:
        pipeline, _ = build()
        await pipeline._set_state(VoiceState.SPEAKING)
        for _ in range(int(BARGE_IN_MS / FRAME_MS) - 2):
            await pipeline.push_audio(SPEECH)
        await pipeline.push_audio(SILENCE)
        for _ in range(3):
            await pipeline.push_audio(SPEECH)
        assert pipeline.state is VoiceState.SPEAKING


class TestSpeaking:
    async def test_speech_is_chunked_so_it_starts_sooner(self) -> None:
        tts = FakeTts()
        pipeline, _ = build(tts=tts)
        await pipeline.speak("First sentence. Second sentence. Third one.")
        assert len(tts.spoken) == 3

    async def test_missing_tts_is_reported_not_silently_skipped(self) -> None:
        pipeline, events = build(tts=FakeTts(available=False))
        await pipeline.speak("Hello")
        assert any(e.type == "voice.unavailable" for e in events)

    async def test_speaking_can_be_cancelled(self) -> None:
        pipeline, _ = build()
        await pipeline._set_state(VoiceState.SPEAKING)
        await pipeline.cancel_speech()
        assert pipeline._speaking_task is None

    async def test_responses_are_not_spoken_when_turned_off(self) -> None:
        tts = FakeTts()
        pipeline, _ = build(tts=tts, settings=VoiceSettings(speak_responses=False))
        await pipeline.speak("Hello there.")
        assert tts.spoken == []


class TestAvailability:
    """Degradation is explicit: never a silent fallback, never a pretence."""

    async def test_a_missing_component_names_itself_and_its_size(self) -> None:
        class Missing(SpeechToText):
            def status(self) -> ComponentStatus:
                return ComponentStatus(
                    "speech-to-text", False, "model not downloaded", download_mb=74
                )

            def transcribe(self, audio: bytes, fmt: AudioFormat) -> Transcript:
                raise JarvisError("unavailable")

        pipeline, _ = build(stt=Missing())
        reason = pipeline.unavailable_reason()
        assert "speech-to-text" in reason
        assert "74 MB" in reason

    async def test_listening_refuses_when_a_component_is_missing(self) -> None:
        class Missing(SpeechToText):
            def status(self) -> ComponentStatus:
                return ComponentStatus("speech-to-text", False, "not installed")

            def transcribe(self, audio: bytes, fmt: AudioFormat) -> Transcript:
                raise JarvisError("unavailable")

        pipeline, _ = build(stt=Missing())
        with pytest.raises(JarvisError, match="not ready"):
            await pipeline.start_listening()
        assert pipeline.state is VoiceState.UNAVAILABLE

    def test_a_ready_pipeline_reports_no_reason(self) -> None:
        pipeline, _ = build()
        assert pipeline.unavailable_reason() == ""

    # Push-to-talk needs nothing, so it must stay usable without a wake model.
    def test_disabling_the_wake_word_removes_it_from_readiness(self) -> None:
        pipeline, _ = build(settings=VoiceSettings(wake_word_enabled=False))
        wake = next(c for c in pipeline.status().components if c.name == "wake-word")
        assert wake.available
        assert "push-to-talk" in wake.detail


class TestModelReporting:
    def test_whisper_reports_what_is_missing_and_how_big(self) -> None:
        status = WhisperSpeechToText().status()
        if not status.available:
            assert status.download_mb > 0
            assert status.detail

    def test_every_model_size_is_described(self) -> None:
        for name, choice in MODELS.items():
            assert choice.size_mb > 0, name
            assert choice.detail, name

    def test_the_default_wake_word_is_pretrained(self) -> None:
        """The reason the assistant is called Jarvis: no training needed."""
        assert DEFAULT_WAKE_WORD in BUILT_IN

    def test_a_custom_wake_word_is_refused_with_an_explanation(self) -> None:
        status = OpenWakeWordDetector(word="hey_computer").status()
        assert not status.available
        assert status.detail

"""The wake word, against the real models and real speech.

Everything else in the voice tests uses a fake detector, which is right for
testing *when* the pipeline listens but says nothing about whether saying
"Jarvis" actually wakes it. This module downloads the three openWakeWord graphs
through Jarvis's own verifying fetcher, synthesises the phrase with Piper, and
asserts the detector fires on it and not on other speech.

It exists because of a bug that every fake would have missed. Loading a model by
file path makes openWakeWord key its scores by the file's stem
("hey_jarvis_v0.1"), not by the wake word ("hey_jarvis"). The detector looked up
the wake word, got nothing, and scored 0.0 forever: it would have run, consumed
CPU and never once woken up. A fake detector reports whatever it is told to, so
only the real model can catch that.

Skipped unless the voice extra is installed. CI runs it as its own job, the way
the browser gate does, because the download and the inference are too slow to
belong in the main suite.

**What this does not prove:** that a real microphone in a real room hears you.
This is clean synthesised speech resampled to 16 kHz, with no background noise,
no reverb and no distance from the microphone. It establishes that the models,
the paths, the score key, the threshold and the pipeline transition are all
correct. Whether it hears *you* is answered by using it.
"""

from __future__ import annotations

import array
from pathlib import Path

import pytest

openwakeword = pytest.importorskip("openwakeword", reason="needs the voice extra")

from jarvis.config import models  # noqa: E402
from jarvis.voice.capture import FrameListSource, MicrophoneCapture  # noqa: E402
from jarvis.voice.types import VoiceState  # noqa: E402
from jarvis.voice.wake import MODEL_KEYS, OpenWakeWordDetector  # noqa: E402
from tests.test_voice import FakeStt, FakeTts  # noqa: E402

FRAME_SAMPLES = 1280
PIPER_VOICE = "en_US-lessac-medium"
PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium"


@pytest.fixture(scope="module")
def _downloaded(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Fetch the three models once, through Jarvis's own verifying downloader.

    Using `models.fetch` rather than curl is deliberate: it means this test also
    covers the checksums recorded in the catalogue. A wrong hash fails here.
    """
    import os

    from jarvis.config import paths

    data = tmp_path_factory.mktemp("voice-data")
    before = os.environ.get("JARVIS_DATA_DIR")
    os.environ["JARVIS_DATA_DIR"] = str(data)
    paths.reset_cache()
    try:
        models.fetch("openwakeword-hey-jarvis")
    except Exception as exc:  # pragma: no cover — network
        pytest.skip(f"could not fetch the wake word models: {exc}")
    finally:
        if before is None:
            os.environ.pop("JARVIS_DATA_DIR", None)
        else:
            os.environ["JARVIS_DATA_DIR"] = before
        paths.reset_cache()
    return data


@pytest.fixture
def wake_models(_downloaded: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point this test's data directory at the models downloaded once above.

    conftest gives every test a fresh `JARVIS_DATA_DIR`, which is right — but it
    would mean re-downloading 4 MB per test, so the directory is redirected here
    instead. Function-scoped so it runs after that autouse fixture.
    """
    from jarvis.config import paths

    monkeypatch.setenv("JARVIS_DATA_DIR", str(_downloaded))
    paths.reset_cache()
    yield _downloaded
    paths.reset_cache()


def _resample_to_16k(samples: array.array[int], rate: int) -> array.array[int]:
    if rate == 16_000:
        return samples
    out = array.array("h")
    n = int(len(samples) * 16_000 / rate)
    for i in range(n):
        # Linear interpolation. Good enough to carry a wake word; this is a
        # test fixture, not the capture path.
        pos = i * rate / 16_000
        lo = int(pos)
        hi = min(lo + 1, len(samples) - 1)
        frac = pos - lo
        out.append(int(samples[lo] * (1 - frac) + samples[hi] * frac))
    return out


@pytest.fixture(scope="module")
def speech(tmp_path_factory: pytest.TempPathFactory) -> dict[str, bytes]:
    """Synthesise the wake word and a control phrase, as 16 kHz mono PCM."""
    pytest.importorskip("piper", reason="needs piper-tts to synthesise test speech")
    import urllib.request

    from piper import PiperVoice

    out = tmp_path_factory.mktemp("piper")
    onnx, cfg = out / "voice.onnx", out / "voice.onnx.json"
    try:
        urllib.request.urlretrieve(f"{PIPER_BASE}/{PIPER_VOICE}.onnx", onnx)  # noqa: S310
        urllib.request.urlretrieve(f"{PIPER_BASE}/{PIPER_VOICE}.onnx.json", cfg)  # noqa: S310
    except Exception as exc:  # pragma: no cover — network
        pytest.skip(f"could not fetch the Piper voice: {exc}")

    voice = PiperVoice.load(str(onnx), config_path=str(cfg))
    rate = voice.config.sample_rate

    def say(text: str) -> bytes:
        chunks = array.array("h")
        for chunk in voice.synthesize(text):
            raw = getattr(chunk, "audio_int16_bytes", None)
            if raw is None:
                data = array.array("h")
                data.frombytes(bytes(chunk.audio_int16_array))  # type: ignore[attr-defined]
                chunks.extend(data)
            else:
                data = array.array("h")
                data.frombytes(raw)
                chunks.extend(data)
        return _resample_to_16k(chunks, rate).tobytes()

    return {"wake": say("hey jarvis"), "control": say("what is the weather like today")}


def _frames(pcm: bytes, *, pad_ms: int = 500) -> list[bytes]:
    """Split PCM into detector-sized frames, with silence either side.

    The model scores a ring buffer, so it needs context before and after the
    phrase — which a microphone would always be providing.
    """
    pad = b"\x00\x00" * int(16_000 * pad_ms / 1000)
    audio = pad + pcm + pad
    step = FRAME_SAMPLES * 2
    return [audio[i : i + step] for i in range(0, len(audio) - step, step)]


class TestRealModels:
    def test_all_three_models_are_fetched_and_verified(self, wake_models: Path) -> None:
        for key in MODEL_KEYS:
            spec = models.BY_KEY[key]
            ok, message = models.verify_installed(spec)
            assert ok, message
            assert "no recorded checksum" not in message, (
                f"{key} has no checksum, so the download was not actually verified"
            )

    def test_status_is_available_once_downloaded(self, wake_models: Path) -> None:
        status = OpenWakeWordDetector().status()
        assert status.available, status.detail

    def test_score_key_is_the_file_stem_not_the_wake_word(self, wake_models: Path) -> None:
        """Regression: looking up the wake word returns nothing, forever.

        If this ever reverts to `scores.get("hey_jarvis")`, detection silently
        stops working while everything still appears to run.
        """
        detector = OpenWakeWordDetector()
        detector._load()
        assert detector._score_key == "hey_jarvis_v0.1"
        assert detector._score_key != detector.word

    def test_silence_does_not_wake_it(self, wake_models: Path) -> None:
        detector = OpenWakeWordDetector()
        quiet = b"\x00\x00" * FRAME_SAMPLES
        assert all(detector.push(quiet) is None for _ in range(40))


class TestRealSpeech:
    def test_the_wake_word_is_detected(self, wake_models: Path, speech: dict[str, bytes]) -> None:
        detector = OpenWakeWordDetector()
        hits = [d for f in _frames(speech["wake"]) if (d := detector.push(f)) is not None]
        assert hits, "synthesised 'hey jarvis' did not fire the detector"
        assert hits[0].confidence >= detector.threshold

    def test_other_speech_does_not_wake_it(
        self, wake_models: Path, speech: dict[str, bytes]
    ) -> None:
        """A false wake is worse than a missed one, so this matters as much."""
        detector = OpenWakeWordDetector()
        hits = [d for f in _frames(speech["control"]) if (d := detector.push(f)) is not None]
        assert not hits, f"an unrelated phrase woke the detector: {hits}"

    @pytest.mark.asyncio
    async def test_capture_drives_the_pipeline_from_listening_to_recording(
        self, wake_models: Path, speech: dict[str, bytes]
    ) -> None:
        """The whole path: frames in, real detection, state machine advances."""
        from jarvis.voice.pipeline import PipelineEvent, VoicePipeline, VoiceSettings

        events: list[PipelineEvent] = []

        async def record(event: PipelineEvent) -> None:
            events.append(event)

        pipeline = VoicePipeline(
            FakeStt("open chrome"),
            FakeTts(),
            OpenWakeWordDetector(),
            settings=VoiceSettings(enabled=True),
            on_event=record,
        )
        capture = MicrophoneCapture(pipeline, FrameListSource(_frames(speech["wake"])))
        await capture.start()

        import asyncio

        for _ in range(400):
            if any(e.type == "voice.wake" for e in events):
                break
            await asyncio.sleep(0.005)
        await capture.stop()

        assert any(e.type == "voice.wake" for e in events), (
            f"no wake event; saw {[e.type for e in events]}"
        )
        assert any(
            e.type == "voice.state" and e.data.get("state") == VoiceState.RECORDING.value
            for e in events
        ), "the pipeline never moved from listening to recording"

"""Speech to text.

Local by default: `faster-whisper` runs entirely on this machine, so no audio
leaves it. A cloud provider can be configured, but it is opt-in and the
interface says when it is active — "the microphone is streaming to a server"
should never be a surprise.

The model is not bundled. It is downloaded on first use with consent, and until
then this reports exactly what is missing and how big it is.
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass
from pathlib import Path

from jarvis.config import paths
from jarvis.util.errors import JarvisError
from jarvis.util.logging import get_logger
from jarvis.voice.types import AudioFormat, ComponentStatus, Transcript

log = get_logger(__name__)


@dataclass(frozen=True)
class ModelChoice:
    """A Whisper size, with the trade-off stated so the choice is informed."""

    size_mb: int
    detail: str


MODELS: dict[str, ModelChoice] = {
    "tiny.en": ModelChoice(39, "Fastest, least accurate. Fine for short commands."),
    "base.en": ModelChoice(74, "Good balance; the default."),
    "small.en": ModelChoice(244, "Noticeably better on accents and noise."),
    "medium.en": ModelChoice(769, "Best accuracy; needs a capable machine."),
}
DEFAULT_MODEL = "base.en"


class SpeechToText(abc.ABC):
    @abc.abstractmethod
    def status(self) -> ComponentStatus: ...

    @abc.abstractmethod
    def transcribe(self, audio: bytes, fmt: AudioFormat) -> Transcript: ...

    def prepare(self) -> ComponentStatus:
        """Make this component ready, downloading if that is what it takes.

        Separate from `transcribe` to break a deadlock. `status` reported the
        model missing, the pipeline refused to start listening while anything
        was missing, and the model was only ever downloaded by `transcribe` —
        which could not run until listening had started. There was no way
        through the interface to ever get the model.
        """
        return self.status()


class WhisperSpeechToText(SpeechToText):
    """faster-whisper, running locally."""

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self.model_name = model if model in MODELS else DEFAULT_MODEL
        self._model: object | None = None

    @property
    def model_dir(self) -> Path:
        return paths.models_dir() / "whisper"

    def status(self) -> ComponentStatus:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return ComponentStatus(
                name="speech-to-text",
                available=False,
                detail=(
                    "The 'faster-whisper' package is not installed. Install the "
                    "'voice' extra to enable speech recognition."
                ),
                model=self.model_name,
                download_mb=MODELS[self.model_name].size_mb,
            )

        # The model directory is populated on first download.
        if not self.model_dir.exists() or not any(self.model_dir.iterdir()):
            return ComponentStatus(
                name="speech-to-text",
                available=False,
                detail=(
                    f"The {self.model_name} speech model has not been downloaded "
                    f"yet ({MODELS[self.model_name].size_mb} MB). "
                    f"{MODELS[self.model_name].detail}"
                ),
                model=self.model_name,
                download_mb=MODELS[self.model_name].size_mb,
            )

        return ComponentStatus(
            name="speech-to-text",
            available=True,
            detail=f"Local {self.model_name}; audio never leaves this computer.",
            model=self.model_name,
        )

    def _load(self) -> object:
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise JarvisError(
                "Speech recognition needs the 'voice' extra to be installed."
            ) from exc

        self.model_dir.mkdir(parents=True, exist_ok=True)
        log.info("loading speech model", model=self.model_name)
        # int8 on CPU: a desktop assistant should not need a GPU to hear you.
        self._model = WhisperModel(
            self.model_name,
            device="cpu",
            compute_type="int8",
            download_root=str(self.model_dir),
        )
        return self._model

    def prepare(self) -> ComponentStatus:
        """Download the weights by loading the model once.

        Unlike every other model, this download is not checksummed by Jarvis:
        faster-whisper resolves and fetches its own weights through
        huggingface_hub, which verifies them against the hashes the Hub
        publishes. Jarvis does not get to see the file first, so adding an entry
        to the catalogue with a URL would mean two competing download paths.
        `jarvis/config/models.py` says so where the entry is defined.
        """
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return self.status()
        log.info("preparing speech model", model=self.model_name)
        self._load()
        return self.status()

    def transcribe(self, audio: bytes, fmt: AudioFormat) -> Transcript:
        import numpy as np

        started = time.monotonic()
        model = self._load()
        # Whisper wants float32 in [-1, 1]; the capture layer gives 16-bit PCM.
        samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
        segments, info = model.transcribe(  # type: ignore[attr-defined]
            samples, language="en", vad_filter=False, beam_size=1
        )
        parts = list(segments)
        text = " ".join(s.text.strip() for s in parts).strip()
        confidence = (
            sum(getattr(s, "avg_logprob", 0.0) for s in parts) / len(parts) if parts else 0.0
        )
        log.info(
            "transcribed",
            characters=len(text),
            audio_seconds=round(len(audio) / fmt.bytes_per_second, 2),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        return Transcript(
            text=text,
            # avg_logprob is negative; map it to something a person can read.
            confidence=max(0.0, min(1.0, 1.0 + confidence)),
            language=getattr(info, "language", "en"),
            duration_seconds=len(audio) / fmt.bytes_per_second,
            model=self.model_name,
        )


class UnavailableSpeechToText(SpeechToText):
    """Used when no engine is configured. Never returns an empty transcript."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def status(self) -> ComponentStatus:
        return ComponentStatus("speech-to-text", False, self.reason)

    def transcribe(self, audio: bytes, fmt: AudioFormat) -> Transcript:
        del audio, fmt
        raise JarvisError(self.reason)

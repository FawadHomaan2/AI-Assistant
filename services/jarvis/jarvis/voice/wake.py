"""Wake word detection.

openWakeWord ships a pretrained `hey_jarvis` model — which is why the assistant
is called Jarvis. No training data to collect, no custom model to build, and the
accuracy comes from a model trained on far more speakers than we could gather.

Detection is three small ONNX graphs running locally: audio becomes a
mel-spectrogram, the spectrogram becomes a speech embedding, and the embedding
is scored for "hey jarvis". Together they are about 3.5 MB. The microphone is
never streamed to a server to listen for a wake word.

All three are loaded from Jarvis's own models directory by explicit path, not
by name. openWakeWord resolves a bare name against its own package resources,
which in a PyInstaller one-file build is a temporary extraction directory that
does not survive — and which the verifying downloader in `jarvis.config.models`
never writes to anyway.
"""

from __future__ import annotations

import abc
import contextlib
from pathlib import Path
from typing import Any

from jarvis.config import paths
from jarvis.util.errors import JarvisError
from jarvis.util.logging import get_logger
from jarvis.voice.types import ComponentStatus, WakeDetection

log = get_logger(__name__)

DEFAULT_WAKE_WORD = "hey_jarvis"

#: The three files together, rounded up. Reported to the user before any
#: download, so it is the real figure rather than a guess.
WAKE_MODEL_MB = 4

#: Catalogue keys in `jarvis.config.models` that the detector needs on disk.
#: Asking for the first fetches all three, because it declares the other two as
#: requirements.
MODEL_KEYS = (
    "openwakeword-hey-jarvis",
    "openwakeword-melspectrogram",
    "openwakeword-embedding",
)

#: Pretrained models openWakeWord provides. Anything else needs training.
BUILT_IN = ("hey_jarvis", "alexa", "hey_mycroft", "hey_rhasspy")

#: Above this a detection fires. Lower catches more but false-triggers on
#: television and conversation; this is deliberately conservative because a
#: false wake is more annoying than a missed one — push-to-talk always works.
DEFAULT_THRESHOLD = 0.5


class WakeWordDetector(abc.ABC):
    @abc.abstractmethod
    def status(self) -> ComponentStatus: ...

    @abc.abstractmethod
    def push(self, frame: bytes) -> WakeDetection | None: ...

    @abc.abstractmethod
    def reset(self) -> None: ...


class OpenWakeWordDetector(WakeWordDetector):
    def __init__(self, word: str = DEFAULT_WAKE_WORD, threshold: float = DEFAULT_THRESHOLD) -> None:
        self.word = word
        self.threshold = threshold
        self._model: object | None = None
        self._score_key = ""
        self._elapsed = 0.0

    @property
    def model_dir(self) -> Path:
        return paths.models_dir() / "openwakeword"

    def status(self) -> ComponentStatus:
        try:
            import openwakeword  # noqa: F401
        except ImportError:
            return ComponentStatus(
                "wake-word",
                False,
                "The 'openwakeword' package is not installed. Install the 'voice' "
                "extra, or use push-to-talk, which needs nothing.",
                model=self.word,
                download_mb=WAKE_MODEL_MB,
            )
        if self.word not in BUILT_IN:
            return ComponentStatus(
                "wake-word",
                False,
                f"{self.word!r} is not one of the pretrained models "
                f"({', '.join(BUILT_IN)}). A custom wake word needs a trained model.",
                model=self.word,
            )
        missing = [spec.name for spec in self._specs() if not spec.installed()]
        if missing:
            # Name what is missing. "The model has not been downloaded" when two
            # of three files are present sends you looking in the wrong place.
            return ComponentStatus(
                "wake-word",
                False,
                f"Not downloaded yet ({WAKE_MODEL_MB} MB): {', '.join(missing)}.",
                model=self.word,
                download_mb=WAKE_MODEL_MB,
            )
        return ComponentStatus(
            "wake-word",
            True,
            f"Listening locally for {self.word.replace('_', ' ')}.",
            model=self.word,
        )

    @staticmethod
    def _specs() -> list[Any]:
        from jarvis.config import models

        return [models.BY_KEY[key] for key in MODEL_KEYS]

    def _load(self) -> object:
        if self._model is not None:
            return self._model
        try:
            from openwakeword.model import Model
        except ImportError as exc:
            raise JarvisError("Wake-word detection needs the 'voice' extra.") from exc

        wake, melspec, embedding = (spec.path() for spec in self._specs())
        for path in (wake, melspec, embedding):
            if not path.is_file():
                raise JarvisError(
                    f"The wake word needs {path.name}, which is not downloaded. "
                    f"Download it from Settings, or use push-to-talk."
                )

        self._model = Model(
            wakeword_models=[str(wake)],
            inference_framework="onnx",
            melspec_model_path=str(melspec),
            embedding_model_path=str(embedding),
        )
        # Loading by path makes openWakeWord key its scores by the file's stem
        # ("hey_jarvis_v0.1"), not by the wake word ("hey_jarvis"). Reading the
        # key off the loaded model is what stops `push` from looking up a name
        # that is never there and so never detecting anything.
        keys = getattr(self._model, "models", None) or [wake.stem]
        self._score_key = next(iter(keys), wake.stem)
        log.info("wake word model loaded", score_key=self._score_key)
        return self._model

    def push(self, frame: bytes) -> WakeDetection | None:
        import numpy as np

        model = self._load()
        samples = np.frombuffer(frame, dtype=np.int16)
        scores = model.predict(samples)  # type: ignore[attr-defined]
        self._elapsed += len(samples) / 16_000
        score = float(scores.get(self._score_key, 0.0))
        if score >= self.threshold:
            log.info("wake word detected", word=self.word, confidence=round(score, 3))
            self.reset()
            return WakeDetection(self.word, score, self._elapsed)
        return None

    def reset(self) -> None:
        if self._model is not None:
            with contextlib.suppress(Exception):
                self._model.reset()  # type: ignore[attr-defined]


class UnavailableWakeWord(WakeWordDetector):
    """No wake word. Push-to-talk still works, which is the dependable path."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def status(self) -> ComponentStatus:
        return ComponentStatus("wake-word", False, self.reason)

    def push(self, frame: bytes) -> WakeDetection | None:
        del frame
        return None

    def reset(self) -> None:
        return None

"""Wake word detection.

openWakeWord ships a pretrained `hey_jarvis` model — which is why the assistant
is called Jarvis. No training data to collect, no custom model to build, and the
accuracy comes from a model trained on far more speakers than we could gather.

Detection is a ~15 MB ONNX graph running locally on a ring buffer. The
microphone is never streamed to a server to listen for a wake word.
"""

from __future__ import annotations

import abc
import contextlib
from pathlib import Path

from jarvis.config import paths
from jarvis.util.errors import JarvisError
from jarvis.util.logging import get_logger
from jarvis.voice.types import ComponentStatus, WakeDetection

log = get_logger(__name__)

DEFAULT_WAKE_WORD = "hey_jarvis"
WAKE_MODEL_MB = 15

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
        if not self.model_dir.exists() or not any(self.model_dir.glob("*.onnx")):
            return ComponentStatus(
                "wake-word",
                False,
                f"The {self.word} model has not been downloaded yet ({WAKE_MODEL_MB} MB).",
                model=self.word,
                download_mb=WAKE_MODEL_MB,
            )
        return ComponentStatus(
            "wake-word",
            True,
            f"Listening locally for {self.word.replace('_', ' ')}.",
            model=self.word,
        )

    def _load(self) -> object:
        if self._model is not None:
            return self._model
        try:
            from openwakeword.model import Model
        except ImportError as exc:
            raise JarvisError("Wake-word detection needs the 'voice' extra.") from exc
        self._model = Model(wakeword_models=[self.word], inference_framework="onnx")
        return self._model

    def push(self, frame: bytes) -> WakeDetection | None:
        import numpy as np

        model = self._load()
        samples = np.frombuffer(frame, dtype=np.int16)
        scores = model.predict(samples)  # type: ignore[attr-defined]
        self._elapsed += len(samples) / 16_000
        score = float(scores.get(self.word, 0.0))
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

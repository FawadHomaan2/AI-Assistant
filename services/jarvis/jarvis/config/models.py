"""Downloadable models, and what it costs to have them.

The installer deliberately does **not** bundle these. Voice alone is ~130 MB
and semantic memory another ~90 MB, and most of it is for features a given user
may never turn on. Shipping them would roughly quintuple an installer that is
otherwise about 50 MB, for capabilities that each already report themselves
unavailable with a reason.

So they are fetched on demand, and three rules apply to every fetch:

**The user is told the size before anything is downloaded.** A progress bar
that appears without warning on a metered connection is a bug.

**Every file is verified against a SHA-256 recorded here.** A model is code in
every sense that matters — it is loaded and executed by an inference runtime —
so a corrupted or substituted download is a code-execution problem, not a
quality problem. A mismatch deletes the file and refuses.

**A failed download leaves nothing behind.** Downloads go to a `.part` file and
are renamed only after verification, so an interrupted fetch cannot leave a
truncated model that then fails mysteriously at load time.

A checksum is recorded only once it has been verified against an actual
download. An empty one means the entry is **not fetchable**: Jarvis refuses
rather than downloading something it cannot check, and says so.

The three openWakeWord models carry real checksums — each file was downloaded
and hashed, so the wake word is fetchable. Whisper, Piper and MiniLM are still
empty and therefore still refused; filling them in needs the real files, and
docs/PACKAGING.md says how. A plausible-looking hash that nobody ever checked
would be worse than an empty one, because it would look finished.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarvis.config import paths
from jarvis.util.errors import JarvisError
from jarvis.util.logging import get_logger

log = get_logger(__name__)

#: Read in chunks: a 500 MB model must not be held in memory to be hashed.
CHUNK = 1024 * 256

#: openWakeWord's pinned release. Pinned to a tag, not to `latest`, so the
#: recorded checksums keep matching what the URL serves.
_OWW_RELEASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"

#: Where the detector looks. `jarvis.voice.wake` reads this same folder, and the
#: two disagreeing is how a model lands on disk somewhere nothing reads it.
_OWW_FOLDER = "openwakeword"

#: Piper's voices, pinned to a revision for the same reason as above.
_PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium"


class ModelError(JarvisError):
    code = "jarvis.model.failed"
    http_status = 502


class ChecksumMismatch(ModelError):
    code = "jarvis.model.checksum"
    http_status = 502


@dataclass(frozen=True)
class ModelSpec:
    key: str
    name: str
    #: What stops working without it, in the user's terms.
    enables: str
    filename: str
    url: str
    size_mb: int
    #: SHA-256 of the file. Empty means "not verified", which means "not
    #: downloadable" — see the module docstring.
    sha256: str = ""
    #: Subfolder under the models directory.
    folder: str = ""
    #: Other catalogue keys this model cannot run without. openWakeWord needs a
    #: mel-spectrogram front end and a speech-embedding model in addition to the
    #: wake word itself, and a wake word file on its own does nothing.
    requires: tuple[str, ...] = ()

    @property
    def fetchable(self) -> bool:
        return bool(self.url and self.sha256)

    def path(self) -> Path:
        base = paths.data_dir() / "models"
        return (base / self.folder / self.filename) if self.folder else base / self.filename

    def installed(self) -> bool:
        return self.path().is_file()

    def to_dict(self) -> dict[str, Any]:
        present = self.installed()
        return {
            "key": self.key,
            "name": self.name,
            "enables": self.enables,
            "sizeMb": self.size_mb,
            "installed": present,
            "fetchable": self.fetchable,
            "path": str(self.path()),
            "reason": (
                ""
                if present or self.fetchable
                else (
                    "Jarvis has no verified checksum for this file, so it will not "
                    "download it. Install it by hand, or see docs/PACKAGING.md."
                )
            ),
        }


#: Everything Jarvis can use but does not ship. Sizes are approximate and are
#: shown before any download starts.
CATALOGUE: tuple[ModelSpec, ...] = (
    # Whisper is the one entry Jarvis does not fetch itself. faster-whisper
    # resolves and downloads its own weights through huggingface_hub on first
    # load, into `models/whisper` — so a catalogue URL here would be a second,
    # competing download path, and the `model.bin` this used to name was in a
    # folder (`whisper-base-en`) that nothing ever read or wrote.
    #
    # It stays listed because the interface should say the model exists, how big
    # it is and whether it is present. `url` is empty, so `fetchable` is false
    # and nothing tries: `POST /voice/stt/prepare` triggers the real download.
    ModelSpec(
        key="whisper-base-en",
        name="Whisper base.en",
        enables="Understanding what you say",
        filename="model.bin",
        url="",
        size_mb=74,
        folder="whisper",
    ),
    # Piper needs its JSON config beside the model: it carries the sample rate
    # and the phoneme map, and loading without it fails in a way that reads
    # like a corrupt model.
    ModelSpec(
        key="piper-en-us",
        name="Piper en_US voice",
        enables="Speaking replies out loud",
        filename="en_US-lessac-medium.onnx",
        url=f"{_PIPER_BASE}/en_US-lessac-medium.onnx?download=true",
        sha256="5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f",
        size_mb=61,
        folder="piper",
        requires=("piper-en-us-config",),
    ),
    ModelSpec(
        key="piper-en-us-config",
        name="Piper en_US voice config",
        enables="Telling Piper the voice's sample rate and phonemes",
        filename="en_US-lessac-medium.onnx.json",
        url=f"{_PIPER_BASE}/en_US-lessac-medium.onnx.json?download=true",
        sha256="efe19c417bed055f2d69908248c6ba650fa135bc868b0e6abb3da181dab690a0",
        size_mb=1,
        folder="piper",
    ),
    # The wake word is three ONNX graphs: audio becomes a mel-spectrogram,
    # the spectrogram becomes a speech embedding, and the embedding is scored
    # for "hey jarvis". All three are small, and all three are required —
    # `requires` below is what makes asking for one fetch the set.
    #
    # The checksums are of the actual files served by these URLs, hashed after
    # downloading them. They are not placeholders.
    ModelSpec(
        key="openwakeword-hey-jarvis",
        name="openWakeWord hey_jarvis",
        enables='Waking up when you say "Jarvis"',
        filename="hey_jarvis_v0.1.onnx",
        url=f"{_OWW_RELEASE}/hey_jarvis_v0.1.onnx",
        sha256="94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb",
        size_mb=1,
        folder=_OWW_FOLDER,
        requires=("openwakeword-melspectrogram", "openwakeword-embedding"),
    ),
    ModelSpec(
        key="openwakeword-melspectrogram",
        name="openWakeWord mel-spectrogram",
        enables="Turning microphone audio into features the wake word can score",
        filename="melspectrogram.onnx",
        url=f"{_OWW_RELEASE}/melspectrogram.onnx",
        sha256="ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f",
        size_mb=1,
        folder=_OWW_FOLDER,
    ),
    ModelSpec(
        key="openwakeword-embedding",
        name="openWakeWord speech embedding",
        enables="Turning those features into a speech embedding",
        filename="embedding_model.onnx",
        url=f"{_OWW_RELEASE}/embedding_model.onnx",
        sha256="70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f",
        size_mb=1,
        folder=_OWW_FOLDER,
    ),
    ModelSpec(
        key="minilm-l6-v2",
        name="all-MiniLM-L6-v2",
        enables="Searching memory by meaning rather than by shared words",
        filename="model.onnx",
        url="",
        size_mb=90,
        folder="minilm",
    ),
)

BY_KEY: dict[str, ModelSpec] = {m.key: m for m in CATALOGUE}


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def catalogue() -> list[dict[str, Any]]:
    return [m.to_dict() for m in CATALOGUE]


def summary() -> dict[str, Any]:
    installed = [m for m in CATALOGUE if m.installed()]
    missing = [m for m in CATALOGUE if not m.installed()]
    return {
        "models": catalogue(),
        "installedCount": len(installed),
        "totalCount": len(CATALOGUE),
        "missingMb": sum(m.size_mb for m in missing),
        "directory": str(paths.data_dir() / "models"),
        "bundled": False,
        "note": (
            "Models are downloaded when you ask for them, not bundled, because "
            "they are larger than the rest of Jarvis put together and most "
            "installs need only some of them."
        ),
    }


def verify_installed(spec: ModelSpec) -> tuple[bool, str]:
    """Check a model already on disk against its recorded checksum."""
    path = spec.path()
    if not path.is_file():
        return False, f"{spec.name} is not installed."
    if not spec.sha256:
        return True, (
            f"{spec.name} is present. Jarvis has no recorded checksum for it, so "
            f"it cannot confirm the file is the one it expects."
        )
    actual = sha256_of(path)
    if actual != spec.sha256:
        return False, (
            f"{spec.name} on disk does not match its expected checksum. It may be "
            f"corrupted or may not be the file Jarvis expects. Delete {path} and "
            f"fetch it again."
        )
    return True, f"{spec.name} is installed and verified."


def _https_opener(url: str) -> Any:  # pragma: no cover — needs the network
    """Open an https URL. Separated so `fetch` can be tested without a network."""
    import urllib.request

    if not url.lower().startswith("https://"):
        raise ModelError("Only https URLs are fetched.")
    return urllib.request.urlopen(url, timeout=60)  # noqa: S310 — scheme checked above


def fetch(key: str, opener: Any = None) -> Path:
    """Download a model, verify it, and only then put it in place.

    `opener` exists so the download path can be tested without a network: it is
    called with the URL and must return a file-like object. The verification
    below is what actually matters, and it runs either way.
    """
    spec = BY_KEY.get(key)
    if spec is None:
        raise ModelError(f"There is no model called {key!r}.")
    # Dependencies first. A wake word file with no mel-spectrogram model beside
    # it loads and then fails at the first frame, which is a worse failure than
    # refusing up front.
    for required in spec.requires:
        fetch(required, opener=opener)
    if spec.installed():
        return spec.path()
    if not spec.fetchable:
        raise ModelError(
            f"Jarvis will not download {spec.name}: it has no verified checksum "
            f"for the file, so it could not tell a good download from a bad one. "
            f"See docs/PACKAGING.md for installing it by hand."
        )

    destination = spec.path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")

    if not spec.url.lower().startswith("https://"):
        # The URLs come from the constant above, but the check is here anyway:
        # a `file:` or `ftp:` URL reaching urlopen is a very different operation
        # from a download, and this is the only place that would notice.
        raise ModelError(f"{spec.name} has a URL that is not https, so Jarvis will not fetch it.")

    log.info("model download starting", model=spec.key, mb=spec.size_mb)
    try:
        download = opener if opener is not None else _https_opener
        with download(spec.url) as response, partial.open("wb") as handle:
            shutil.copyfileobj(response, handle, CHUNK)
    except Exception as exc:
        partial.unlink(missing_ok=True)
        raise ModelError(f"Could not download {spec.name}: {exc}") from exc

    actual = sha256_of(partial)
    if actual != spec.sha256:
        # A model is loaded and executed by an inference runtime, so a file that
        # is not the one expected is a code-execution problem. It does not stay
        # on disk, and it certainly does not get used.
        partial.unlink(missing_ok=True)
        raise ChecksumMismatch(
            f"{spec.name} downloaded, but its checksum does not match what Jarvis "
            f"expects. The file has been deleted and nothing was loaded."
        )

    partial.replace(destination)
    log.info("model installed", model=spec.key, path=str(destination))
    return destination


def remove(key: str) -> bool:
    spec = BY_KEY.get(key)
    if spec is None or not spec.installed():
        return False
    spec.path().unlink(missing_ok=True)
    return True

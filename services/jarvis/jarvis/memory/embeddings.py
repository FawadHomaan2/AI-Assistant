"""Turning text into vectors, locally.

Embeddings never leave this machine (ARCHITECTURE §8: "Embeddings — local
MiniLM/BGE ONNX, **never** cloud"). Sending them out would be as revealing as
sending the text: they are derived from it and a near-neighbour search over a
corpus recovers most of the meaning.

Two backends, and the difference between them is stated rather than hidden:

  **MiniLM** is a real sentence embedder. It understands that "use dark mode"
  and "I prefer a dark theme" mean the same thing despite sharing one word. It
  needs a ~90 MB model download and `onnxruntime`, so it is often not present.

  **Lexical** is a hashed bag-of-words. It is not semantic and does not pretend
  to be: it matches on shared words, so it finds "dark theme" from "dark mode"
  and misses it from "night colours". It needs nothing, works offline, and is
  deterministic — which is also what makes it testable.

The store records which backend produced each vector, because vectors from
different models are points in unrelated coordinate systems. Comparing them
produces confident nonsense, so a backend change invalidates the old vectors
instead of quietly mixing the two.
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
from typing import Protocol

from jarvis.util.logging import get_logger

log = get_logger(__name__)

#: Small enough to keep thousands of rows cheap, wide enough that unrelated
#: words rarely collide into the same bucket.
LEXICAL_DIMENSION = 512

MINILM_DIMENSION = 384
MINILM_MODEL = "all-MiniLM-L6-v2"
MINILM_DOWNLOAD_MB = 90

_TOKEN = re.compile(r"[a-z0-9]+")

#: Words that appear in nearly every sentence carry no signal, and leaving them
#: in makes everything look slightly similar to everything else.
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by",
    "do", "does", "for", "from", "had", "has", "have", "he", "her", "his",
    "i", "if", "in", "is", "it", "its", "me", "my", "of", "on",
    "or", "our", "she", "so", "that", "the", "their", "them", "then", "there",
    "these", "they", "this", "to", "too", "us", "was", "we", "were", "what",
    "when", "where", "which", "who", "will", "with", "would", "you", "your",
})  # fmt: skip


def tokenise(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS and len(t) > 1]


def pack(vector: list[float]) -> bytes:
    """Store as little-endian float32: compact, and portable across machines."""
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    """Similarity of two vectors, 0 when either is empty or they differ in size."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return 0.0 if norm == 0 else max(-1.0, min(1.0, dot / norm))


class Embedder(Protocol):
    name: str
    dimension: int

    def available(self) -> tuple[bool, str]: ...

    def embed(self, text: str) -> list[float]: ...


class LexicalEmbedder:
    """Hashed bag-of-words. Keyword matching, expressed as a vector.

    Honest about its limits: it finds memories that share words with the query
    and misses ones that mean the same thing in different words. That is worth
    having — most recall is "what did I say about the Downloads folder" — but it
    is not semantic search and the interface says so.
    """

    name = "lexical-v1"
    dimension = LEXICAL_DIMENSION

    def available(self) -> tuple[bool, str]:
        return True, "Word matching. Works offline; finds shared words, not meaning."

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        tokens = tokenise(text)
        if not tokens:
            return vector
        for token in tokens:
            # Blake2b rather than hash(): Python's is salted per process, so the
            # same word would land in a different bucket after a restart and
            # every stored vector would stop matching.
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            bucket = int.from_bytes(digest, "big") % self.dimension
            # The sign spreads collisions out instead of letting unrelated words
            # pile up additively in one bucket.
            vector[bucket] += 1.0 if digest[0] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector] if norm else vector


class MiniLMEmbedder:
    """Real sentence embeddings, when the model has been downloaded.

    Not implemented as a silent fallback to something weaker: if the model is
    missing, `available()` says so and names the download, and the caller
    chooses. A fallback that quietly changes what "similar" means would make
    recall mysteriously worse with no way to tell why.
    """

    name = f"minilm-{MINILM_MODEL}"
    dimension = MINILM_DIMENSION

    def __init__(self, model_path: str = "") -> None:
        self.model_path = model_path
        self._session: object | None = None

    def available(self) -> tuple[bool, str]:
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            return False, (
                f"Semantic memory search needs onnxruntime and the "
                f"{MINILM_MODEL} model (~{MINILM_DOWNLOAD_MB} MB). Neither is "
                f"installed, so memory is searched by word instead."
            )
        if not self.model_path:
            return False, (
                f"The {MINILM_MODEL} model (~{MINILM_DOWNLOAD_MB} MB) has not been downloaded yet."
            )
        return True, f"{MINILM_MODEL} loaded."

    def embed(self, text: str) -> list[float]:
        ok, detail = self.available()
        if not ok:
            raise RuntimeError(detail)
        # The ONNX session, tokeniser and mean-pooling go here once the model
        # ships. Raising is deliberate: a stub that returned zeros would make
        # every memory equally similar to every query, and the failure would
        # look like bad recall rather than a missing model.
        raise NotImplementedError(
            f"{MINILM_MODEL} inference is not wired up yet. Memory search uses "
            f"word matching until it is."
        )


def best_available(prefer_semantic: bool = True) -> Embedder:
    """The strongest embedder this installation can actually run."""
    if prefer_semantic:
        minilm = MiniLMEmbedder()
        ok, detail = minilm.available()
        if ok:
            return minilm
        log.debug("semantic embeddings unavailable", reason=detail)
    return LexicalEmbedder()

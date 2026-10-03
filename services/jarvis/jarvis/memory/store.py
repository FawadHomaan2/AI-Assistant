"""The memory store.

Three durable tiers (ARCHITECTURE §8). Working memory is the turn itself and is
never written down.

  **episodic**   — what happened: a session's actions, kept for recall
  **semantic**   — what is true about you: preferences, aliases, habits
  **procedural** — what works: plans that succeeded, and failures with a cause

Two rules decide whether something is written, and they are the reason this is a
memory rather than a log of guesses.

**Promotion needs evidence.** Something Jarvis merely *noticed* starts as a
candidate and is not acted on. It becomes active after three consistent
observations, or immediately if you stated it outright. One-off behaviour never
becomes a durable belief — open a PDF in Chrome once and Jarvis should not
decide that is your preference.

**A contradiction resets the count rather than averaging it.** If you have used
Acrobat three times and then use Chrome, the honest conclusion is "I no longer
know", not "0.5 confidence in Acrobat". The count starts again from the new
behaviour.

Nothing here is ever certain: confidence is capped below 1.0 even for something
you said, because people change their minds and the dashboard must always offer
a way to correct it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from jarvis.db.engine import Database
from jarvis.db.repositories import new_id
from jarvis.memory.embeddings import Embedder, LexicalEmbedder, cosine, pack, unpack
from jarvis.util.logging import get_logger
from jarvis.util.redaction import redact_text

log = get_logger(__name__)

TIERS = ("episodic", "semantic", "procedural")

#: Consistent observations before something Jarvis noticed is acted on.
PROMOTION_THRESHOLD = 3

#: Confidence for something you said outright, and for a first observation.
STATED_CONFIDENCE = 0.9
OBSERVED_CONFIDENCE = 0.3
#: Each further consistent observation adds this, up to the cap.
OBSERVATION_STEP = 0.2
#: Never 1.0. A memory you cannot correct is a bug, not a feature.
MAX_CONFIDENCE = 0.95

#: Below this, a memory is not worth putting in front of the model.
MIN_USEFUL_CONFIDENCE = 0.25

#: How well a phrase must match before "forget that ..." deletes it. Deletion
#: is irreversible, so this is deliberately stricter than ordinary recall:
#: "I had nothing matching that" is a far better failure than deleting the
#: wrong memory silently.
FORGET_MIN_RELEVANCE = 0.3

#: The instruction part of "forget that I prefer dark mode".
_FORGET_PREFIX = re.compile(
    r"^\s*(?:please\s+)?(?:forget|stop remembering|don'?t remember|unlearn)"
    r"\s*(?:that|about|my|the)?\s*",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Memory:
    id: str
    tier: str
    key: str
    value: Any
    confidence: float = OBSERVED_CONFIDENCE
    observation_count: int = 1
    source: str = "observed"
    source_turn_id: str | None = None
    session_id: str | None = None
    created_at: str = ""
    updated_at: str = ""
    last_used_at: str | None = None
    use_count: int = 0
    pinned: bool = False
    status: str = "candidate"
    #: Set by a search, not stored. `relevance` is how well the text matched;
    #: `score` is the ranking, which also weighs confidence and pinning.
    #: They are separate because confidence must break ties between matches,
    #: never turn a non-match into one — a confidently-held belief about
    #: something else is still not an answer to this query.
    relevance: float = 0.0
    score: float = 0.0

    @property
    def active(self) -> bool:
        return self.status == "active"

    def text(self) -> str:
        """The form that gets embedded and searched: key plus value."""
        value = self.value if isinstance(self.value, str) else json.dumps(self.value)
        return f"{self.key.replace('.', ' ').replace('_', ' ')} {value}"

    def sentence(self) -> str:
        """One line, for showing the user and for the model's context."""
        value = self.value if isinstance(self.value, str) else json.dumps(self.value)
        return f"{self.key}: {value}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tier": self.tier,
            "key": self.key,
            "value": self.value,
            "confidence": round(self.confidence, 2),
            "observationCount": self.observation_count,
            "source": self.source,
            "sourceTurnId": self.source_turn_id,
            "sessionId": self.session_id,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "lastUsedAt": self.last_used_at,
            "useCount": self.use_count,
            "pinned": self.pinned,
            "status": self.status,
            "relevance": round(self.relevance, 3),
            "score": round(self.score, 3),
            "sentence": self.sentence(),
        }


def _row_to_memory(row: sqlite3.Row) -> Memory:
    return Memory(
        id=row["id"],
        tier=row["tier"],
        key=row["key"],
        value=json.loads(row["value_json"]),
        confidence=row["confidence"],
        observation_count=row["observation_count"],
        source=row["source"],
        source_turn_id=row["source_turn_id"],
        session_id=row["session_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_used_at=row["last_used_at"],
        use_count=row["use_count"],
        pinned=bool(row["pinned"]),
        status=row["status"],
    )


@dataclass
class RememberResult:
    memory: Memory
    created: bool
    promoted: bool
    contradicted: bool

    def explain(self) -> str:
        """What actually happened, in a sentence the user could be shown."""
        if self.contradicted:
            return (
                f"That contradicts what I had for {self.memory.key}, so I have "
                f"started counting again from this."
            )
        if self.promoted:
            return f"I'll remember that: {self.memory.sentence()}"
        if self.created:
            return f"Noted, but I won't act on it yet — {self.memory.key} seen once."
        return (
            f"Seen {self.memory.observation_count} times now "
            f"({PROMOTION_THRESHOLD} needed before I act on it)."
        )


class MemoryStore:
    """CRUD and retrieval over durable memory.

    Everything written passes the redaction filter first. A memory is one of the
    few things that is both long-lived and derived from free text, so an API key
    pasted into a sentence would otherwise sit on disk indefinitely.
    """

    def __init__(self, db: Database, embedder: Embedder | None = None) -> None:
        self.db = db
        self.embedder: Embedder = embedder or LexicalEmbedder()

    # ── writing ──────────────────────────────────────────────────────────
    def remember(
        self,
        tier: str,
        key: str,
        value: Any,
        *,
        source: str = "observed",
        session_id: str | None = None,
        turn_id: str | None = None,
    ) -> RememberResult:
        """Record an observation or a statement. Returns what changed and why."""
        if tier not in TIERS:
            raise ValueError(f"unknown tier {tier!r}; expected one of {TIERS}")
        if source not in ("stated", "observed"):
            raise ValueError(f"unknown source {source!r}")

        key = key.strip().lower()
        if not key:
            raise ValueError("a memory needs a key")
        value = redact_text(value) if isinstance(value, str) else value

        existing = self.get(tier, key)
        now = _now()

        if existing is None:
            memory = Memory(
                id=new_id("mem"),
                tier=tier,
                key=key,
                value=value,
                source=source,
                confidence=STATED_CONFIDENCE if source == "stated" else OBSERVED_CONFIDENCE,
                observation_count=1,
                source_turn_id=turn_id,
                session_id=session_id,
                created_at=now,
                updated_at=now,
                # Something you said is acted on at once; something merely
                # noticed waits for corroboration.
                status="active" if source == "stated" else "candidate",
            )
            self._insert(memory)
            self._index(memory)
            log.info("memory created", key=key, tier=tier, source=source, status=memory.status)
            return RememberResult(memory, created=True, promoted=memory.active, contradicted=False)

        same = existing.value == value
        was_active = existing.active

        if not same:
            # A contradiction. Start again from the new value rather than
            # averaging two incompatible beliefs into a confident middle.
            existing.value = value
            existing.observation_count = 1
            existing.confidence = STATED_CONFIDENCE if source == "stated" else OBSERVED_CONFIDENCE
            existing.source = source
            existing.status = "active" if source == "stated" else "candidate"
        else:
            existing.observation_count += 1
            if source == "stated":
                existing.source = "stated"
                existing.confidence = max(existing.confidence, STATED_CONFIDENCE)
                existing.status = "active"
            else:
                existing.confidence = min(MAX_CONFIDENCE, existing.confidence + OBSERVATION_STEP)
                if existing.observation_count >= PROMOTION_THRESHOLD:
                    existing.status = "active"

        existing.updated_at = now
        existing.session_id = session_id or existing.session_id
        existing.source_turn_id = turn_id or existing.source_turn_id
        self._update(existing)
        if not same:
            self._index(existing)

        log.info(
            "memory updated",
            key=key,
            tier=tier,
            count=existing.observation_count,
            status=existing.status,
            contradicted=not same,
        )
        return RememberResult(
            existing,
            created=False,
            promoted=existing.active and not was_active,
            contradicted=not same,
        )

    def set_value(self, memory_id: str, value: Any) -> Memory | None:
        """Correct a memory by hand, from the Privacy dashboard.

        An edit is treated as a statement: you have said what is true, so it
        becomes active at once rather than waiting to be observed again.
        """
        memory = self.by_id(memory_id)
        if memory is None:
            return None
        memory.value = redact_text(value) if isinstance(value, str) else value
        memory.source = "stated"
        memory.confidence = STATED_CONFIDENCE
        memory.status = "active"
        memory.updated_at = _now()
        self._update(memory)
        self._index(memory)
        return memory

    def pin(self, memory_id: str, pinned: bool = True) -> Memory | None:
        memory = self.by_id(memory_id)
        if memory is None:
            return None
        memory.pinned = pinned
        memory.updated_at = _now()
        self._update(memory)
        return memory

    def touch(self, memory_id: str) -> None:
        """Record that a memory was actually used, so stale ones are visible."""
        self.db.execute(
            "UPDATE memory SET use_count = use_count + 1, last_used_at = ? WHERE id = ?",
            (_now(), memory_id),
        )

    # ── forgetting ───────────────────────────────────────────────────────
    def forget(self, memory_id: str) -> bool:
        """Delete outright. "Forget" must mean gone, not hidden."""
        before = self.count()
        self.db.execute("DELETE FROM memory WHERE id = ?", (memory_id,))
        return self.count() < before

    def forget_key(self, tier: str, key: str) -> bool:
        before = self.count()
        self.db.execute(
            "DELETE FROM memory WHERE tier = ? AND key = ?", (tier, key.strip().lower())
        )
        return self.count() < before

    def forget_matching(self, text: str) -> list[Memory]:
        """Delete what a phrase refers to, returning what went.

        Used by "forget that I prefer dark mode". The caller shows the list, so
        a wrong match is visible rather than silent.
        """
        # Strip the "forget that ..." wrapper: those words are the instruction,
        # not the thing being described, and leaving them in dilutes the match.
        needle = _FORGET_PREFIX.sub("", text).strip() or text
        hits = self.search(
            needle, limit=5, include_candidates=True, min_relevance=FORGET_MIN_RELEVANCE
        )
        for memory in hits:
            self.forget(memory.id)
        return hits

    def clear(self, tier: str | None = None) -> int:
        n = self.count(tier)
        if tier:
            self.db.execute("DELETE FROM memory WHERE tier = ?", (tier,))
        else:
            self.db.execute("DELETE FROM memory")
        return n

    # ── reading ──────────────────────────────────────────────────────────
    def by_id(self, memory_id: str) -> Memory | None:
        row = self.db.query_one("SELECT * FROM memory WHERE id = ?", (memory_id,))
        return _row_to_memory(row) if row else None

    def get(self, tier: str, key: str) -> Memory | None:
        row = self.db.query_one(
            "SELECT * FROM memory WHERE tier = ? AND key = ?", (tier, key.strip().lower())
        )
        return _row_to_memory(row) if row else None

    def list_all(
        self,
        *,
        tier: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[Memory]:
        sql = "SELECT * FROM memory WHERE 1=1"
        params: list[Any] = []
        if tier:
            sql += " AND tier = ?"
            params.append(tier)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY pinned DESC, confidence DESC, updated_at DESC LIMIT ?"
        params.append(limit)
        return [_row_to_memory(r) for r in self.db.query(sql, tuple(params))]

    def count(self, tier: str | None = None) -> int:
        if tier:
            row = self.db.query_one("SELECT COUNT(*) AS n FROM memory WHERE tier = ?", (tier,))
        else:
            row = self.db.query_one("SELECT COUNT(*) AS n FROM memory")
        return int(row["n"]) if row else 0

    def stats(self) -> dict[str, Any]:
        rows = self.db.query("SELECT tier, status, COUNT(*) AS n FROM memory GROUP BY tier, status")
        by_tier: dict[str, dict[str, int]] = {t: {"active": 0, "candidate": 0} for t in TIERS}
        for row in rows:
            by_tier.setdefault(row["tier"], {})[row["status"]] = int(row["n"])
        available, detail = self.embedder.available()
        return {
            "total": self.count(),
            "byTier": by_tier,
            "embedder": self.embedder.name,
            "embedderDetail": detail,
            "semantic": available and not self.embedder.name.startswith("lexical"),
            "promotionThreshold": PROMOTION_THRESHOLD,
        }

    # ── retrieval ────────────────────────────────────────────────────────
    def search(
        self,
        query: str,
        *,
        tiers: tuple[str, ...] = TIERS,
        limit: int = 5,
        include_candidates: bool = False,
        min_relevance: float = 0.0,
    ) -> list[Memory]:
        """Rank memories against a query.

        A memory must *match* to be returned at all: either its key is named,
        or its text shares words with the query. Confidence and pinning then
        order the matches. Letting confidence contribute to the match itself
        would return a firmly-held belief about something else for any query,
        which is how a memory system starts answering questions nobody asked.

        Recency deliberately does not feature. A preference stated last month
        is not less true than one stated today, and ranking by recency would
        make Jarvis forget things simply because the user was quiet.
        """
        wanted = ",".join("?" * len(tiers))
        sql = f"SELECT * FROM memory WHERE tier IN ({wanted})"  # noqa: S608 — placeholders
        params: list[Any] = list(tiers)
        if not include_candidates:
            sql += " AND status = 'active'"
        rows = self.db.query(sql, tuple(params))
        if not rows:
            return []

        query_vector = self.embedder.embed(query)
        vectors = self._vectors()
        needle = query.strip().lower()

        scored: list[Memory] = []
        for row in rows:
            memory = _row_to_memory(row)
            stored = vectors.get(memory.id)
            similarity = max(0.0, cosine(query_vector, stored)) if stored else 0.0
            exact = 1.0 if needle and (needle in memory.key or memory.key in needle) else 0.0
            memory.relevance = max(exact, similarity)
            memory.score = (
                memory.relevance * 0.7 + memory.confidence * 0.2 + (0.1 if memory.pinned else 0.0)
            )
            if memory.relevance > min_relevance:
                scored.append(memory)

        scored.sort(key=lambda m: m.score, reverse=True)
        return scored[:limit]

    def recall_for(self, text: str, *, limit: int = 5) -> list[Memory]:
        """Memories worth putting in front of the model for this message.

        Candidates are excluded on purpose: something seen once is not a belief,
        and feeding it to the model would make it behave as though it were.
        """
        hits = [
            m
            for m in self.search(text, limit=limit * 2)
            if m.confidence >= MIN_USEFUL_CONFIDENCE and m.score >= 0.2
        ]
        for memory in hits[:limit]:
            self.touch(memory.id)
        return hits[:limit]

    # ── internals ────────────────────────────────────────────────────────
    def _insert(self, m: Memory) -> None:
        self.db.execute(
            "INSERT INTO memory (id, tier, key, value_json, confidence, observation_count,"
            " source, source_turn_id, session_id, created_at, updated_at, pinned, status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                m.id,
                m.tier,
                m.key,
                json.dumps(m.value),
                m.confidence,
                m.observation_count,
                m.source,
                m.source_turn_id,
                m.session_id,
                m.created_at,
                m.updated_at,
                int(m.pinned),
                m.status,
            ),
        )

    def _update(self, m: Memory) -> None:
        self.db.execute(
            "UPDATE memory SET value_json = ?, confidence = ?, observation_count = ?,"
            " source = ?, source_turn_id = ?, session_id = ?, updated_at = ?,"
            " pinned = ?, status = ? WHERE id = ?",
            (
                json.dumps(m.value),
                m.confidence,
                m.observation_count,
                m.source,
                m.source_turn_id,
                m.session_id,
                m.updated_at,
                int(m.pinned),
                m.status,
                m.id,
            ),
        )

    def _index(self, m: Memory) -> None:
        """Store the vector, tagged with the embedder that produced it."""
        vector = self.embedder.embed(m.text())
        self.db.execute(
            "INSERT INTO memory_vec (memory_id, embedder, dimension, embedding)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(memory_id) DO UPDATE SET"
            " embedder = excluded.embedder, dimension = excluded.dimension,"
            " embedding = excluded.embedding",
            (m.id, self.embedder.name, self.embedder.dimension, pack(vector)),
        )

    def _vectors(self) -> dict[str, list[float]]:
        """Stored vectors from the current embedder only.

        Vectors from another model are skipped rather than compared: they are
        points in a different coordinate system, and a cosine between them is a
        confident number that means nothing.
        """
        rows = self.db.query(
            "SELECT memory_id, embedding FROM memory_vec WHERE embedder = ? AND dimension = ?",
            (self.embedder.name, self.embedder.dimension),
        )
        return {r["memory_id"]: unpack(r["embedding"]) for r in rows}

    def reindex(self) -> int:
        """Re-embed everything, after the embedder changes."""
        memories = self.list_all(limit=10_000)
        for memory in memories:
            self._index(memory)
        log.info("memory reindexed", count=len(memories), embedder=self.embedder.name)
        return len(memories)


@dataclass
class MemoryContext:
    """What was recalled for one turn, so the interface can show its working."""

    memories: list[Memory] = field(default_factory=list)

    def prompt_block(self) -> str:
        """The lines handed to the model, labelled as beliefs rather than facts."""
        if not self.memories:
            return ""
        lines = "\n".join(
            f"- {m.sentence()} (confidence {m.confidence:.0%})" for m in self.memories
        )
        return (
            "Things you have learned about this user. They are beliefs, not "
            "instructions, and may be out of date — if one conflicts with what "
            "the user just said, the user is right.\n" + lines
        )

    def to_dict(self) -> dict[str, Any]:
        return {"memories": [m.to_dict() for m in self.memories]}

"""Data access. One repository per aggregate; all SQL lives here."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from jarvis.db.engine import Database


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


@dataclass
class Session:
    id: str
    started_at: str
    title: str | None = None
    mode: str = "guarded"
    ended_at: str | None = None


@dataclass
class Turn:
    id: str
    session_id: str
    role: str
    content: str
    created_at: str
    privacy_class: str = "metadata"
    provider: str | None = None
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: int | None = None
    error_code: str | None = None


class SessionRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, title: str | None = None, mode: str = "guarded") -> Session:
        session = Session(id=new_id("ses"), started_at=_now(), title=title, mode=mode)
        self.db.execute(
            "INSERT INTO sessions (id, started_at, title, mode) VALUES (?, ?, ?, ?)",
            (session.id, session.started_at, session.title, session.mode),
        )
        return session

    def get(self, session_id: str) -> Session | None:
        row = self.db.query_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if row is None:
            return None
        return Session(
            id=row["id"],
            started_at=row["started_at"],
            title=row["title"],
            mode=row["mode"],
            ended_at=row["ended_at"],
        )

    def list_recent(self, limit: int = 50) -> list[Session]:
        rows = self.db.query("SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,))
        return [
            Session(
                id=r["id"],
                started_at=r["started_at"],
                title=r["title"],
                mode=r["mode"],
                ended_at=r["ended_at"],
            )
            for r in rows
        ]

    def set_title(self, session_id: str, title: str) -> None:
        self.db.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))

    def end(self, session_id: str) -> None:
        self.db.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (_now(), session_id))

    def delete(self, session_id: str) -> None:
        # Turns cascade via the foreign key.
        self.db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def delete_all(self) -> int:
        rows = self.db.query("SELECT COUNT(*) AS n FROM sessions")
        count = int(rows[0]["n"]) if rows else 0
        self.db.execute("DELETE FROM sessions")
        return count


class TurnRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        privacy_class: str = "metadata",
        provider: str | None = None,
        model: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        latency_ms: int | None = None,
        error_code: str | None = None,
    ) -> Turn:
        turn = Turn(
            id=new_id("turn"),
            session_id=session_id,
            role=role,
            content=content,
            created_at=_now(),
            privacy_class=privacy_class,
            provider=provider,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            error_code=error_code,
        )
        self.db.execute(
            "INSERT INTO turns (id, session_id, role, content, privacy_class, provider, "
            "model, tokens_in, tokens_out, latency_ms, error_code, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                turn.id,
                turn.session_id,
                turn.role,
                turn.content,
                turn.privacy_class,
                turn.provider,
                turn.model,
                turn.tokens_in,
                turn.tokens_out,
                turn.latency_ms,
                turn.error_code,
                turn.created_at,
            ),
        )
        return turn

    def history(self, session_id: str, limit: int = 100) -> list[Turn]:
        rows = self.db.query(
            "SELECT * FROM turns WHERE session_id = ? ORDER BY created_at ASC, rowid ASC LIMIT ?",
            (session_id, limit),
        )
        return [
            Turn(
                id=r["id"],
                session_id=r["session_id"],
                role=r["role"],
                content=r["content"],
                created_at=r["created_at"],
                privacy_class=r["privacy_class"],
                provider=r["provider"],
                model=r["model"],
                tokens_in=r["tokens_in"],
                tokens_out=r["tokens_out"],
                latency_ms=r["latency_ms"],
                error_code=r["error_code"],
            )
            for r in rows
        ]

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM turns")
        return int(row["n"]) if row else 0


GENESIS_HASH = "0" * 64


@dataclass
class AuditEntry:
    actor: str
    action: str
    args_digest: str = ""
    risk: str = "safe"
    decision: str = "allowed"
    outcome: str = "ok"
    error_code: str | None = None
    session_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class AuditRepository:
    """Append-only, hash-chained log.

    `hash = sha256(prev_hash | ts | actor | action | args_digest | risk |
    decision | outcome)`. Changing or removing a row breaks every hash after it,
    so tampering is detectable — which is the whole point of keeping an audit
    log you can also read from the app.

    Argument *digests* are stored, never raw arguments, so the log proves what
    happened without becoming a second copy of your file contents.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def digest(payload: Any) -> str:
        """Stable digest of a tool's arguments."""
        encoded = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _chain_hash(prev: str, ts: str, e: AuditEntry) -> str:
        material = "|".join(
            [prev, ts, e.actor, e.action, e.args_digest, e.risk, e.decision, e.outcome]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _head(self) -> str:
        row = self.db.query_one("SELECT hash FROM audit ORDER BY id DESC LIMIT 1")
        return row["hash"] if row else GENESIS_HASH

    def append(self, entry: AuditEntry) -> str:
        with self.db.transaction() as cur:
            row = cur.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
            prev = row["hash"] if row else GENESIS_HASH
            ts = _now()
            digest = self._chain_hash(prev, ts, entry)
            cur.execute(
                "INSERT INTO audit (ts, actor, action, args_digest, risk, decision, outcome, "
                "error_code, session_id, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts,
                    entry.actor,
                    entry.action,
                    entry.args_digest,
                    entry.risk,
                    entry.decision,
                    entry.outcome,
                    entry.error_code,
                    entry.session_id,
                    prev,
                    digest,
                ),
            )
        return digest

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def verify(self) -> tuple[bool, int | None]:
        """Re-walk the chain. Returns (intact, first_broken_row_id)."""
        rows = self.db.query("SELECT * FROM audit ORDER BY id ASC")
        prev = GENESIS_HASH
        for row in rows:
            if row["prev_hash"] != prev:
                return False, int(row["id"])
            expected = self._chain_hash(
                prev,
                row["ts"],
                AuditEntry(
                    actor=row["actor"],
                    action=row["action"],
                    args_digest=row["args_digest"],
                    risk=row["risk"],
                    decision=row["decision"],
                    outcome=row["outcome"],
                ),
            )
            if expected != row["hash"]:
                return False, int(row["id"])
            prev = row["hash"]
        return True, None

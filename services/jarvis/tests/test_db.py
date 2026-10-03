from __future__ import annotations

from jarvis.db.engine import Database
from jarvis.db.migrations import LATEST
from jarvis.db.repositories import (
    AuditEntry,
    AuditRepository,
    SessionRepository,
    TurnRepository,
)


def test_migrations_apply_and_are_idempotent() -> None:
    db = Database(":memory:")
    assert db.version == LATEST
    assert db.migrate() == LATEST
    db.close()


def test_expected_tables_exist() -> None:
    db = Database(":memory:")
    names = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sessions", "turns", "audit", "preferences"} <= names
    db.close()


def test_foreign_keys_cascade() -> None:
    db = Database(":memory:")
    sessions, turns = SessionRepository(db), TurnRepository(db)
    s = sessions.create()
    turns.add(s.id, "user", "hello")
    assert turns.count() == 1
    sessions.delete(s.id)
    assert turns.count() == 0
    db.close()


def test_turn_role_is_constrained() -> None:
    import sqlite3

    import pytest

    db = Database(":memory:")
    s = SessionRepository(db).create()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO turns (id, session_id, role, content, created_at) VALUES (?,?,?,?,?)",
            ("t1", s.id, "wizard", "x", "now"),
        )
    db.close()


def test_history_is_ordered() -> None:
    db = Database(":memory:")
    sessions, turns = SessionRepository(db), TurnRepository(db)
    s = sessions.create()
    for i in range(5):
        turns.add(s.id, "user", f"m{i}")
    assert [t.content for t in turns.history(s.id)] == ["m0", "m1", "m2", "m3", "m4"]
    db.close()


class TestAuditChain:
    """An audit log you can silently edit is not an audit log."""

    def test_chain_verifies(self) -> None:
        db = Database(":memory:")
        audit = AuditRepository(db)
        for i in range(5):
            audit.append(AuditEntry(actor="user", action=f"act.{i}"))
        assert audit.verify() == (True, None)
        db.close()

    def test_edit_is_detected(self) -> None:
        db = Database(":memory:")
        audit = AuditRepository(db)
        for i in range(4):
            audit.append(AuditEntry(actor="user", action=f"act.{i}"))
        db.execute("UPDATE audit SET action = 'forged' WHERE id = 2")
        intact, broken = audit.verify()
        assert intact is False
        assert broken == 2
        db.close()

    def test_deletion_is_detected(self) -> None:
        db = Database(":memory:")
        audit = AuditRepository(db)
        for i in range(4):
            audit.append(AuditEntry(actor="user", action=f"act.{i}"))
        db.execute("DELETE FROM audit WHERE id = 2")
        intact, broken = audit.verify()
        assert intact is False
        assert broken == 3  # the row after the hole no longer chains
        db.close()

    def test_empty_chain_is_intact(self) -> None:
        db = Database(":memory:")
        assert AuditRepository(db).verify() == (True, None)
        db.close()

    def test_digest_is_stable_and_not_reversible(self) -> None:
        a = AuditRepository.digest({"path": "C:/secret.txt", "n": 3})
        b = AuditRepository.digest({"n": 3, "path": "C:/secret.txt"})
        assert a == b, "digest must not depend on key order"
        assert "secret" not in a, "digest must not leak the arguments"

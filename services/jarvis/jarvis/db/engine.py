"""SQLite access.

WAL mode, foreign keys on, and forward-only numbered migrations. No ORM: the
schema is small and hand-written SQL is clearer and faster here than mapping it.

The connection is per-thread (`check_same_thread=False` plus a lock) because
FastAPI runs sync handlers in a thread pool.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from jarvis.config import paths
from jarvis.db import migrations
from jarvis.util.logging import get_logger

log = get_logger(__name__)


class Database:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else paths.db_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,  # autocommit; transactions are explicit
        )
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self.migrate()

    def _configure(self) -> None:
        cur = self._conn.cursor()
        # WAL lets the UI read while the agent writes.
        if str(self.path) != ":memory:":
            cur.execute("PRAGMA journal_mode = WAL")
        cur.execute("PRAGMA foreign_keys = ON")
        cur.execute("PRAGMA synchronous = NORMAL")
        cur.execute("PRAGMA busy_timeout = 5000")
        cur.close()

    def migrate(self) -> int:
        """Apply pending migrations. Returns the resulting schema version."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "  version INTEGER PRIMARY KEY,"
                "  applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
                ")"
            )
            row = cur.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            current = row["v"] or 0

            for version, sql in migrations.ALL:
                if version <= current:
                    continue
                log.info("applying migration", version=version)
                # The transaction must live INSIDE the script: executescript()
                # issues an implicit COMMIT before it runs, which would discard
                # a BEGIN issued out here and leave a half-applied schema.
                script = (
                    "BEGIN;\n"
                    f"{sql}\n"
                    f"INSERT INTO schema_version (version) VALUES ({int(version)});\n"
                    "COMMIT;"
                )
                try:
                    cur.executescript(script)
                except Exception:
                    # A failed script leaves the transaction open.
                    if self._conn.in_transaction:
                        cur.execute("ROLLBACK")
                    log.error("migration failed", version=version)
                    raise
                current = version
            cur.close()
            return current

    @property
    def version(self) -> int:
        row = self.query_one("SELECT MAX(version) AS v FROM schema_version")
        return int(row["v"]) if row and row["v"] is not None else 0

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                yield cur
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
            finally:
                cur.close()

    def execute(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> int:
        """Run a write. Returns lastrowid."""
        with self._lock:
            cur = self._conn.execute(sql, params)
            rowid = cur.lastrowid or 0
            cur.close()
            return rowid

    def query(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall()
            cur.close()
            return rows

    def query_one(
        self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()
    ) -> sqlite3.Row | None:
        with self._lock:
            cur = self._conn.execute(sql, params)
            row: sqlite3.Row | None = cur.fetchone()
            cur.close()
            return row

    def close(self) -> None:
        with self._lock:
            self._conn.close()

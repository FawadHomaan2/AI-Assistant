"""What this machine normally looks like.

Without a baseline, every scan reports the same twelve startup programs as
discoveries, and the user learns within a week to close the panel without
reading it. With one, Jarvis can say the far more useful thing: *this is new
since last time*.

Being in the baseline means "seen before", which is not the same as "safe", and
the two are kept separate: `trusted` is set only when the user says so. A
malicious startup entry installed before Jarvis was does not become safe by
having been there first — it becomes familiar, and the interface says
"familiar", not "fine".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from jarvis.db.engine import Database
from jarvis.util.logging import get_logger

log = get_logger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class BaselineEntry:
    fingerprint: str
    category: str
    label: str
    first_seen: str
    last_seen: str
    seen_count: int
    trusted: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "category": self.category,
            "label": self.label,
            "firstSeen": self.first_seen,
            "lastSeen": self.last_seen,
            "seenCount": self.seen_count,
            "trusted": self.trusted,
        }


class Baseline:
    def __init__(self, db: Database) -> None:
        self.db = db

    def observe(self, fingerprint: str, category: str, label: str = "") -> bool:
        """Record that this was seen. Returns True if it is new.

        The return value is the point: a check calls this once per item and
        learns, in the same breath, whether to mention it as new.
        """
        now = _now()
        existing = self.db.query_one(
            "SELECT seen_count FROM security_baseline WHERE fingerprint = ?", (fingerprint,)
        )
        if existing is None:
            self.db.execute(
                "INSERT INTO security_baseline"
                " (fingerprint, category, label, first_seen, last_seen, seen_count)"
                " VALUES (?,?,?,?,?,1)",
                (fingerprint, category, label, now, now),
            )
            return True
        self.db.execute(
            "UPDATE security_baseline SET last_seen = ?, seen_count = seen_count + 1,"
            " label = CASE WHEN ? != '' THEN ? ELSE label END"
            " WHERE fingerprint = ?",
            (now, label, label, fingerprint),
        )
        return False

    def is_trusted(self, fingerprint: str) -> bool:
        row = self.db.query_one(
            "SELECT trusted FROM security_baseline WHERE fingerprint = ?", (fingerprint,)
        )
        return bool(row["trusted"]) if row else False

    def trust(self, fingerprint: str, trusted: bool = True) -> bool:
        """Mark something as fine. Only ever called because the user said so."""
        before = self.db.query_one(
            "SELECT fingerprint FROM security_baseline WHERE fingerprint = ?", (fingerprint,)
        )
        if before is None:
            return False
        self.db.execute(
            "UPDATE security_baseline SET trusted = ? WHERE fingerprint = ?",
            (int(trusted), fingerprint),
        )
        log.info("baseline trust changed", fingerprint=fingerprint[:12], trusted=trusted)
        return True

    def entries(self, category: str = "", limit: int = 500) -> list[BaselineEntry]:
        sql = "SELECT * FROM security_baseline"
        params: list[Any] = []
        if category:
            sql += " WHERE category = ?"
            params.append(category)
        sql += " ORDER BY category, label LIMIT ?"
        params.append(limit)
        return [
            BaselineEntry(
                fingerprint=r["fingerprint"],
                category=r["category"],
                label=r["label"],
                first_seen=r["first_seen"],
                last_seen=r["last_seen"],
                seen_count=r["seen_count"],
                trusted=bool(r["trusted"]),
            )
            for r in self.db.query(sql, tuple(params))
        ]

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM security_baseline")
        return int(row["n"]) if row else 0

    def established(self) -> bool:
        """Whether there has been a scan to compare against at all.

        On a first run everything is new, and saying so about fifty items is
        noise. The interface uses this to say "first scan — learning what is
        normal" instead.
        """
        return self.count() > 0

    def forget(self, fingerprint: str) -> bool:
        before = self.count()
        self.db.execute("DELETE FROM security_baseline WHERE fingerprint = ?", (fingerprint,))
        return self.count() < before

    def clear(self) -> int:
        n = self.count()
        self.db.execute("DELETE FROM security_baseline")
        return n

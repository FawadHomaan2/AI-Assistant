"""Permissions that survive a restart.

A permission model that forgets is not one: if revoking a capability lasts
until the next launch, the revocation was theatre. Equally, if *granting* one
lasts forever by accident, the user has a standing permission they granted for
one task six months ago.

So both directions are persisted, and the expiry is persisted with them.

The global posture is persisted for the same reason in the other direction: a
paused assistant that quietly returns to acting after a restart is the worst
possible default, because the user's last deliberate instruction was "stop".
"""

from __future__ import annotations

from datetime import UTC, datetime

from jarvis.db.engine import Database
from jarvis.governance.scopes import Grant, Scope, ScopeGrants
from jarvis.util.logging import get_logger

log = get_logger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class GrantStore:
    """Reads and writes the grant set and the posture."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ── grants ───────────────────────────────────────────────────────────
    def load(self) -> ScopeGrants:
        """The stored grants, or the install defaults when there are none.

        "No rows" means a fresh install, not "everything revoked" — a user who
        revokes everything leaves a `revoked_all` marker row rather than an
        empty table, so the two are distinguishable.
        """
        rows = self.db.query("SELECT * FROM scope_grants ORDER BY scope, target")
        if not rows:
            defaults = ScopeGrants()
            self.save(defaults)
            return defaults

        grants: list[Grant] = []
        for row in rows:
            if row["scope"] == _REVOKED_ALL:
                continue
            try:
                scope = Scope(row["scope"])
            except ValueError:
                # A scope this build does not know about — from a newer version,
                # or a corrupted row. Dropped rather than guessed at.
                log.warning("ignoring unknown scope in grants", scope=row["scope"])
                continue
            grants.append(
                Grant(
                    scope=scope,
                    target=row["target"],
                    expires_at=row["expires_at"],
                    source=row["source"],
                    granted_at=row["granted_at"],
                )
            )
        return ScopeGrants(grants=grants)

    def save(self, grants: ScopeGrants) -> int:
        """Replace the stored set. Expired grants are not written back."""
        live = grants.live
        with self.db.transaction() as cur:
            cur.execute("DELETE FROM scope_grants")
            for grant in live:
                cur.execute(
                    "INSERT INTO scope_grants (scope, target, granted_at, expires_at, source)"
                    " VALUES (?,?,?,?,?)",
                    (
                        grant.scope.value,
                        grant.target,
                        grant.granted_at,
                        grant.expires_at,
                        grant.source,
                    ),
                )
            if not live:
                # Distinguish "revoked everything" from "never configured".
                cur.execute(
                    "INSERT INTO scope_grants (scope, target, granted_at, expires_at, source)"
                    " VALUES (?,?,?,?,?)",
                    (_REVOKED_ALL, "", _now(), "", "user"),
                )
        return len(live)

    # ── posture ──────────────────────────────────────────────────────────
    def load_posture(self) -> tuple[str, bool]:
        row = self.db.query_one("SELECT mode, read_only FROM posture WHERE id = 1")
        if row is None:
            return "guarded", False
        return str(row["mode"]), bool(row["read_only"])

    def save_posture(self, mode: str, read_only: bool) -> None:
        self.db.execute(
            "INSERT INTO posture (id, mode, read_only, updated_at) VALUES (1,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET mode = excluded.mode,"
            " read_only = excluded.read_only, updated_at = excluded.updated_at",
            (mode, int(read_only), _now()),
        )
        log.info("posture saved", mode=mode, read_only=read_only)


#: A marker row meaning "the user revoked everything", so a genuinely empty
#: grant set is not mistaken for a fresh install and refilled with defaults.
_REVOKED_ALL = "__revoked_all__"

"""Forward-only schema migrations.

Rules: never edit a migration that has shipped; add a new one. Each entry is
(version, SQL). The full target schema is in docs/ARCHITECTURE.md section 12;
tables arrive with the phase that uses them, so this file grows per phase.
"""

from __future__ import annotations

# Phase 2: conversations, turns, and the audit spine.
_0001_INITIAL = """
CREATE TABLE sessions (
    id          TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    title       TEXT,
    mode        TEXT NOT NULL DEFAULT 'guarded',
    voice_used  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE turns (
    id            TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role          TEXT NOT NULL CHECK (role IN ('user','assistant','system','tool')),
    content       TEXT NOT NULL,
    privacy_class TEXT NOT NULL DEFAULT 'metadata'
                  CHECK (privacy_class IN ('public','metadata','content','sensitive')),
    provider      TEXT,
    model         TEXT,
    tokens_in     INTEGER,
    tokens_out    INTEGER,
    latency_ms    INTEGER,
    error_code    TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX idx_turns_session ON turns(session_id, created_at);

-- Append-only and hash-chained: each row commits to the previous one, so a
-- silent edit or deletion breaks the chain and is detectable.
CREATE TABLE audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    args_digest TEXT NOT NULL DEFAULT '',
    risk        TEXT NOT NULL DEFAULT 'safe',
    decision    TEXT NOT NULL DEFAULT 'allowed',
    outcome     TEXT NOT NULL DEFAULT 'ok',
    error_code  TEXT,
    session_id  TEXT,
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL
);
CREATE INDEX idx_audit_ts ON audit(ts);

CREATE TABLE preferences (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    set_by     TEXT NOT NULL DEFAULT 'user' CHECK (set_by IN ('user','learned'))
);
"""

ALL: tuple[tuple[int, str], ...] = ((1, _0001_INITIAL),)
LATEST = max(version for version, _ in ALL)

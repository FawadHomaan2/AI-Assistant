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

# Phase 8: durable memory. `preferences` already exists from migration 1 and is
# left alone — it holds settings the user set directly, which is a different
# thing from something Jarvis worked out and must be able to be wrong about.
_0002_MEMORY = """
CREATE TABLE memory (
    id                TEXT PRIMARY KEY,
    tier              TEXT NOT NULL
                      CHECK (tier IN ('episodic','semantic','procedural')),
    key               TEXT NOT NULL,
    value_json        TEXT NOT NULL,
    -- 0..1. Written memory starts low and rises with repeated observation; an
    -- explicit statement starts high. Never 1.0: nothing learned is certain.
    confidence        REAL NOT NULL DEFAULT 0.3
                      CHECK (confidence >= 0.0 AND confidence <= 1.0),
    observation_count INTEGER NOT NULL DEFAULT 1,
    -- 'stated' means the user said it; 'observed' means Jarvis inferred it.
    -- The dashboard shows which, because they deserve different trust.
    source            TEXT NOT NULL DEFAULT 'observed'
                      CHECK (source IN ('stated','observed')),
    source_turn_id    TEXT REFERENCES turns(id) ON DELETE SET NULL,
    session_id        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    last_used_at      TEXT,
    use_count         INTEGER NOT NULL DEFAULT 0,
    pinned            INTEGER NOT NULL DEFAULT 0,
    -- A candidate has been seen, but not often enough to act on. Promotion to
    -- 'active' needs an explicit statement or three consistent observations.
    status            TEXT NOT NULL DEFAULT 'candidate'
                      CHECK (status IN ('candidate','active','retired'))
);

-- One row per (tier, key): a preference is a fact about the user, not a log.
CREATE UNIQUE INDEX idx_memory_key ON memory(tier, key);
CREATE INDEX idx_memory_status ON memory(status, tier);

CREATE TABLE memory_vec (
    memory_id TEXT PRIMARY KEY REFERENCES memory(id) ON DELETE CASCADE,
    -- Which embedder produced this vector, and how long it is. Vectors from
    -- different models are not comparable, so a model change invalidates them
    -- rather than silently mixing two coordinate systems.
    embedder  TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    embedding BLOB NOT NULL
);
"""

# Phase 9: the Security Center.
_0003_SECURITY = """
CREATE TABLE security_findings (
    id             TEXT PRIMARY KEY,
    detected_at    TEXT NOT NULL,
    category       TEXT NOT NULL,
    severity       TEXT NOT NULL
                   CHECK (severity IN ('info','low','medium','high','critical')),
    -- The honesty rule, enforced by the schema rather than by prompt wording:
    -- there is no value here that means "probably malware". Something Jarvis
    -- has not seen before is 'normal_activity' with a note, never an accusation.
    classification TEXT NOT NULL
                   CHECK (classification IN ('confirmed_event','suspicious_behavior',
                                             'potential_risk','normal_activity')),
    title          TEXT NOT NULL,
    evidence_json  TEXT NOT NULL DEFAULT '{}',
    explanation    TEXT NOT NULL DEFAULT '',
    remediation    TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open','acknowledged','resolved')),
    acknowledged_at TEXT,
    -- Stable across scans, so the same condition updates one row instead of
    -- producing a new alert every time the check runs.
    fingerprint    TEXT NOT NULL UNIQUE,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    seen_count     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_findings_status ON security_findings(status, severity);

-- What this machine normally looks like. Without it, every scan reports the
-- same startup programs as discoveries, and the user learns to ignore them.
CREATE TABLE security_baseline (
    fingerprint TEXT PRIMARY KEY,
    category    TEXT NOT NULL,
    label       TEXT NOT NULL DEFAULT '',
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    seen_count  INTEGER NOT NULL DEFAULT 1,
    -- The user said this one is fine. Distinct from merely familiar.
    trusted     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_baseline_category ON security_baseline(category);
"""

# Phase 10: permissions that survive a restart.
_0004_GRANTS = """
CREATE TABLE scope_grants (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT NOT NULL,
    -- A folder, for the path scopes. Empty means "wherever the scope applies".
    target     TEXT NOT NULL DEFAULT '',
    granted_at TEXT NOT NULL,
    -- Empty means it does not expire. Checked on every use rather than swept:
    -- a grant that is never swept is a grant that still works.
    expires_at TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'user'
               CHECK (source IN ('default','user','session'))
);
-- One row per capability-and-folder, so revoking once actually revokes.
CREATE UNIQUE INDEX idx_grant_scope_target ON scope_grants(scope, target);

-- The global posture, kept here so a restart does not silently return a paused
-- assistant to its default of acting.
CREATE TABLE posture (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    mode       TEXT NOT NULL DEFAULT 'guarded'
               CHECK (mode IN ('paused','guarded','assisted','developer')),
    read_only  INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""

# Phase 11: installed plugins and what the user approved for each.
_0005_PLUGINS = """
CREATE TABLE plugins (
    name          TEXT PRIMARY KEY,
    version       TEXT NOT NULL,
    -- Off until deliberately enabled. Installing something must never be the
    -- same act as running it.
    enabled       INTEGER NOT NULL DEFAULT 0,
    manifest_json TEXT NOT NULL DEFAULT '{}',
    -- The scopes approved at the time of enabling, kept separately from the
    -- manifest so an update that asks for more is detectable.
    scopes_json   TEXT NOT NULL DEFAULT '[]',
    installed_at  TEXT NOT NULL
);
"""

ALL: tuple[tuple[int, str], ...] = (
    (1, _0001_INITIAL),
    (2, _0002_MEMORY),
    (3, _0003_SECURITY),
    (4, _0004_GRANTS),
    (5, _0005_PLUGINS),
)
LATEST = max(version for version, _ in ALL)

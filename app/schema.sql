-- RUNBOOK schema (SQLite)
--
-- Design notes
-- ------------
--  * Every event-owned row carries event_id so that "event scoping" is a
--    property of the schema, not a convention the route handlers have to
--    remember.
--  * There is deliberately NO workflow-state table. Runbook stages are
--    computed from the facts below (see app/runbook/service.py).
--  * The only persisted workflow history is `normalization_runs`, which is
--    required for STALE detection: it records *which* reviews a published
--    normalized result was computed from.

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT    NOT NULL UNIQUE,
    password_hash   TEXT    NOT NULL,
    display_name    TEXT    NOT NULL,
    role            TEXT    NOT NULL CHECK (role IN ('participant','judge','organizer','admin')),
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,          -- 256-bit urlsafe token (the session secret)
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT    NOT NULL,
    expires_at  TEXT    NOT NULL,
    user_agent  TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    slug                TEXT NOT NULL UNIQUE,
    tagline             TEXT NOT NULL DEFAULT '',
    location            TEXT NOT NULL DEFAULT '',
    timezone_label      TEXT NOT NULL DEFAULT 'UTC',
    starts_at           TEXT NOT NULL,          -- ISO-8601 UTC
    submission_deadline TEXT NOT NULL,          -- ISO-8601 UTC
    judging_starts_at   TEXT NOT NULL,
    judging_ends_at     TEXT NOT NULL,
    results_published_at TEXT,                  -- historical publication timestamp
    results_published_run_id INTEGER,           -- exact normalization snapshot published; NULL on legacy databases
    created_by          INTEGER REFERENCES users(id),
    created_at          TEXT NOT NULL,
    CHECK (submission_deadline > starts_at),
    CHECK (judging_starts_at >= submission_deadline),
    CHECK (judging_ends_at >= judging_starts_at)
);

CREATE TABLE IF NOT EXISTS tracks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    slug        TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    sort_order  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (event_id, slug)
);

CREATE TABLE IF NOT EXISTS prizes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    track_id    INTEGER REFERENCES tracks(id) ON DELETE CASCADE,  -- NULL => event-wide prize
    name        TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    rank        INTEGER,
    UNIQUE (event_id, track_id, name)
);

CREATE TABLE IF NOT EXISTS teams (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id         INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name             TEXT    NOT NULL,
    invite_token     TEXT    NOT NULL UNIQUE,   -- secrets.token_urlsafe(24), never sequential
    created_by       INTEGER NOT NULL REFERENCES users(id),
    created_at       TEXT    NOT NULL,
    UNIQUE (event_id, name)
);
CREATE INDEX IF NOT EXISTS idx_teams_event ON teams(event_id);

-- event_id is denormalised on purpose: it lets the database itself enforce
-- "a participant belongs to at most one team per event".
CREATE TABLE IF NOT EXISTS team_members (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    team_id    INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    is_lead    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL,
    UNIQUE (event_id, user_id),
    UNIQUE (team_id, user_id)
);

CREATE TABLE IF NOT EXISTS projects (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    team_id      INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    track_id     INTEGER REFERENCES tracks(id),
    name         TEXT    NOT NULL,
    tagline      TEXT    NOT NULL DEFAULT '',
    description  TEXT    NOT NULL DEFAULT '',
    repo_url     TEXT    NOT NULL DEFAULT '',
    demo_url     TEXT    NOT NULL DEFAULT '',
    web_url      TEXT    NOT NULL DEFAULT '',
    status       TEXT    NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','submitted')),
    submitted_at TEXT,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    -- "Each team may have one project per event"
    UNIQUE (event_id, team_id)
);
CREATE INDEX IF NOT EXISTS idx_projects_event_status ON projects(event_id, status);

CREATE TABLE IF NOT EXISTS rubric_criteria (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    weight_pct  REAL    NOT NULL,               -- percentage of the weighted total
    max_score   REAL    NOT NULL DEFAULT 10,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (event_id, name),
    CHECK (weight_pct >= 0),
    CHECK (max_score > 0)
);

CREATE TABLE IF NOT EXISTS assignments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    track_id     INTEGER REFERENCES tracks(id),
    judge_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at   TEXT    NOT NULL,
    -- duplicate assignment is not allowed
    UNIQUE (project_id, judge_id)
);
CREATE INDEX IF NOT EXISTS idx_assignments_judge ON assignments(event_id, judge_id);

CREATE TABLE IF NOT EXISTS reviews (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    judge_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    assignment_id INTEGER REFERENCES assignments(id) ON DELETE CASCADE,
    status        TEXT    NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','complete')),
    raw_total     REAL,                          -- weighted total, 0..rubric_max
    comment       TEXT NOT NULL DEFAULT '',
    submitted_at  TEXT,
    updated_at    TEXT    NOT NULL,
    UNIQUE (project_id, judge_id)
);
CREATE INDEX IF NOT EXISTS idx_reviews_event_status ON reviews(event_id, status);

CREATE TABLE IF NOT EXISTS review_scores (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id    INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    criterion_id INTEGER NOT NULL REFERENCES rubric_criteria(id) ON DELETE CASCADE,
    score        REAL    NOT NULL,
    UNIQUE (review_id, criterion_id)
);

CREATE TABLE IF NOT EXISTS judge_invites (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    email         TEXT    NOT NULL,
    token         TEXT    NOT NULL UNIQUE,      -- secrets.token_urlsafe(24)
    track_id      INTEGER REFERENCES tracks(id),
    created_by    INTEGER NOT NULL REFERENCES users(id),
    created_at    TEXT    NOT NULL,
    accepted_by   INTEGER REFERENCES users(id),
    accepted_at   TEXT,
    UNIQUE (event_id, email)
);

-- The single piece of persisted workflow history in the system.
-- A row means: "a normalized result was published for this event, computed
-- from exactly these completed reviews, as identified by fingerprint".
CREATE TABLE IF NOT EXISTS normalization_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    created_at      TEXT    NOT NULL,
    created_by      INTEGER REFERENCES users(id),
    method          TEXT    NOT NULL DEFAULT 'per-judge z-score (population sigma)',
    review_count    INTEGER NOT NULL,
    judge_count     INTEGER NOT NULL,
    project_count   INTEGER NOT NULL,
    rubric_fingerprint TEXT NOT NULL,
    fingerprint     TEXT    NOT NULL,          -- sha256 over (project, judge, raw_total, submitted_at)
    payload         TEXT    NOT NULL           -- JSON snapshot of the computed ranking
);
CREATE INDEX IF NOT EXISTS idx_norm_event ON normalization_runs(event_id, created_at DESC);
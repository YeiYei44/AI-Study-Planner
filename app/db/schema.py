"""SQLite schema. Idempotent — every statement is safe to re-run.

Canvas's own numeric ids are used directly as primary keys, since Canvas
is the only real source right now. Once a second source (e.g. the ICS
feed) needs to contribute rows without a Canvas id, `courses`/
`assignments` will need a surrogate id + a separate `canvas_id` column,
matching the multi-source shape sketched in docs/DESIGN.md — deferred
until that's actually needed.
"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS courses (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    course_code     TEXT,
    term            TEXT,
    workflow_state  TEXT,
    source          TEXT NOT NULL,
    raw_json        TEXT NOT NULL,
    fetched_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
    id                  INTEGER PRIMARY KEY,
    course_id           INTEGER NOT NULL REFERENCES courses(id),
    name                TEXT NOT NULL,
    description_html    TEXT,
    due_at              TEXT,
    points_possible     REAL,
    submission_types    TEXT,
    html_url            TEXT,
    workflow_state      TEXT,
    source              TEXT NOT NULL,
    raw_json            TEXT NOT NULL,
    fetched_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assignments_course ON assignments(course_id);
CREATE INDEX IF NOT EXISTS idx_assignments_due_at ON assignments(due_at);

CREATE TABLE IF NOT EXISTS sync_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at          TEXT NOT NULL,
    ended_at            TEXT,
    ok                  INTEGER NOT NULL,
    course_count        INTEGER,
    assignment_count    INTEGER,
    changes_json        TEXT
);

-- Weekly recurring study-availability template. v1: no blackouts/one-off
-- exceptions table yet — deferred until the plan is actually used enough
-- to need them.
CREATE TABLE IF NOT EXISTS availability (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    dow     INTEGER NOT NULL,   -- 0=Monday .. 6=Sunday
    start   TEXT NOT NULL,      -- "HH:MM"
    end     TEXT NOT NULL       -- "HH:MM"
);

-- Effort estimate per assignment. 'basis' is 'user' (hand-entered,
-- always wins, never touched by calibration or re-estimation), 'llm'
-- (Claude, cached against content_hash), or 'default' (the placeholder
-- heuristic, used until an assignment's actually been LLM-estimated).
-- 'minutes' is the p50 estimate; minutes_p80/content_hash were added
-- after this table's first release — see connection.py's migration.
CREATE TABLE IF NOT EXISTS estimates (
    assignment_id   INTEGER PRIMARY KEY REFERENCES assignments(id),
    minutes         INTEGER NOT NULL,
    basis           TEXT NOT NULL
);

-- Actual time spent, logged after the fact — the ground truth
-- calibration.py learns from. estimated_minutes_at_log captures the
-- *fixed, uncalibrated* default_minutes() heuristic at logging time
-- (never the LLM or already-calibrated figure), so the actual/estimate
-- ratio stays a stable comparison and calibration can't compound on
-- itself across repeated recomputation.
CREATE TABLE IF NOT EXISTS sessions (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id               INTEGER REFERENCES assignments(id),
    actual_minutes              INTEGER NOT NULL,
    estimated_minutes_at_log    INTEGER,
    submission_type             TEXT,
    logged_at                   TEXT NOT NULL,
    note                        TEXT
);

-- Whether the *whole assignment* is done, not just individual scheduled
-- blocks — a separate table, deliberately, same reason estimates/
-- sessions are separate from assignments: sync.py's upsert overwrites
-- every Canvas-sourced column on every sync, so a "completed" flag
-- stored directly on the assignments row would get silently wiped the
-- next time you sync. generate_plan() excludes anything marked here —
-- closing a gap this project's own docs flagged and left open: a
-- regenerated plan had no way to know an assignment was actually done,
-- and would schedule it again forever.
CREATE TABLE IF NOT EXISTS assignment_status (
    assignment_id   INTEGER PRIMARY KEY REFERENCES assignments(id),
    completed       INTEGER NOT NULL DEFAULT 0,
    completed_at    TEXT
);

CREATE TABLE IF NOT EXISTS plan_blocks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL,     -- "YYYY-MM-DD"
    start           TEXT NOT NULL,     -- "HH:MM"
    end             TEXT NOT NULL,     -- "HH:MM"
    kind            TEXT NOT NULL,     -- 'assignment' | 'buffer'
    assignment_id   INTEGER REFERENCES assignments(id),
    locked          INTEGER NOT NULL DEFAULT 0,
    completed       INTEGER NOT NULL DEFAULT 0,
    generated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plan_blocks_date ON plan_blocks(date);

-- Manually-uploaded course material (syllabi, slides, notes) — Canvas
-- access alone was never going to cover lecture content. course_id is
-- nullable: not everything ties cleanly to one course. kind='assignment'
-- rows are synthesized from an assignment's own name/description/due
-- date/points (see tutor/assignment_sync.py) rather than uploaded, so
-- the tutor can answer questions about assignment content too, not just
-- files you hand it — assignment_id links back to which one, and
-- content_hash (same caching pattern as estimate-llm's) means an
-- assignment sync only re-embeds ones that actually changed. Both
-- columns were added after this table's first release — see
-- connection.py's migration.
CREATE TABLE IF NOT EXISTS materials (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id       INTEGER REFERENCES courses(id),
    kind            TEXT NOT NULL,     -- 'syllabus' | 'slides' | 'notes' | 'file' | 'assignment'
    title           TEXT NOT NULL,
    source_path     TEXT,
    added_at        TEXT NOT NULL
);

-- ord is position within the material; page_ref is what a citation
-- shows the user ("Lecture 7, slide 12", "page 3") — this is what
-- keeps the tutor's answers checkable instead of just plausible.
-- embedding is a little-endian float32 vector, fixed dimension per
-- embed.py's model (384 for the default).
CREATE TABLE IF NOT EXISTS chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id     INTEGER NOT NULL REFERENCES materials(id),
    ord             INTEGER NOT NULL,
    text            TEXT NOT NULL,
    page_ref        TEXT,
    embedding       BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_material ON chunks(material_id);
"""

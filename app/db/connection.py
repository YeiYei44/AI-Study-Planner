"""SQLite connection + schema bootstrap."""

from __future__ import annotations

import sqlite3

from app.config import Settings, get_settings
from app.db.schema import SCHEMA_SQL

# Columns added to an existing table after its first CREATE TABLE
# shipped. No migration framework yet — the project's young enough that
# a couple of guarded ALTER TABLEs are simpler and more honest than
# pretending we need Alembic already. (table, column, type declaration)
_COLUMN_MIGRATIONS = [
    ("estimates", "minutes_p80", "INTEGER"),
    ("estimates", "content_hash", "TEXT"),
    ("materials", "assignment_id", "INTEGER REFERENCES assignments(id)"),
    ("materials", "content_hash", "TEXT"),
    ("assignment_status", "skip_planning", "INTEGER NOT NULL DEFAULT 0"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in _COLUMN_MIGRATIONS:
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.commit()
    _ensure_chunks_fts(conn)


def _ensure_chunks_fts(conn: sqlite3.Connection) -> None:
    """FTS5 full-text index over chunks, for hybrid (lexical + semantic)
    retrieval — found necessary against real data: a small dense
    embedding model alone ranks a chunk containing the exact text
    "Document 3" 16th of 19 for the query "what is document 3 about",
    because the number gets diluted into an average against generic
    surrounding words. See docs/DESIGN.md and qa.py.

    External-content table + triggers keep it in sync with `chunks`
    automatically, from any code path that inserts/deletes rows, not
    just materials.py. If this SQLite build lacks FTS5, table creation
    raises OperationalError and retrieval just falls back to
    semantic-only (qa.py catches it there, not here) — a personal SQLite
    build without FTS5 compiled in is rare but not impossible.
    """
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
    ).fetchone()
    if exists:
        return
    try:
        conn.executescript(
            """
            CREATE VIRTUAL TABLE chunks_fts USING fts5(text, content='chunks', content_rowid='id');
            CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
                INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
            END;
            CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
            END;
            CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
                INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
            END;
            """
        )
        conn.execute("INSERT INTO chunks_fts(rowid, text) SELECT id, text FROM chunks")
        conn.commit()
    except sqlite3.OperationalError:
        pass


def connect(settings: Settings | None = None) -> sqlite3.Connection:
    settings = settings or get_settings()
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    _migrate(conn)
    return conn

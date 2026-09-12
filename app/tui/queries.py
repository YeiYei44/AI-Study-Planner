"""Read-only query helpers for the TUI panes — kept separate from the
widgets so they're unit-testable without spinning up a Textual app.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone


def upcoming_assignments(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    now = datetime.now(timezone.utc).isoformat()
    return conn.execute(
        "SELECT a.*, c.name AS course_name FROM assignments a "
        "JOIN courses c ON c.id = a.course_id "
        "WHERE a.due_at IS NOT NULL AND a.due_at >= ? AND a.workflow_state = 'published' "
        "ORDER BY a.due_at LIMIT ?",
        (now, limit),
    ).fetchall()


def upcoming_plan_blocks(conn: sqlite3.Connection, days: int = 7) -> list[sqlite3.Row]:
    today = date.today().isoformat()
    end = (date.today() + timedelta(days=days)).isoformat()
    return conn.execute(
        "SELECT pb.*, a.name AS assignment_name FROM plan_blocks pb "
        "LEFT JOIN assignments a ON a.id = pb.assignment_id "
        "WHERE pb.date >= ? AND pb.date <= ? "
        "ORDER BY pb.date, pb.start",
        (today, end),
    ).fetchall()


def all_assignments_with_status(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every published assignment, joined with its estimate (if any),
    completion, and skip-planning status — soonest-due first, undated
    last. Backs the Assignments pane, which is where estimate/complete/
    log/skip actions live, so unlike `upcoming_assignments` this
    deliberately doesn't filter out past-due, already-completed, or
    skipped rows: those are exactly the ones a user might come here to
    undo or adjust."""
    return conn.execute(
        "SELECT a.id, a.due_at, a.name, a.points_possible, c.name AS course_name, "
        "e.minutes AS estimate_minutes, e.basis AS estimate_basis, "
        "COALESCE(s.completed, 0) AS completed, "
        "COALESCE(s.skip_planning, 0) AS skip_planning "
        "FROM assignments a "
        "JOIN courses c ON c.id = a.course_id "
        "LEFT JOIN estimates e ON e.assignment_id = a.id "
        "LEFT JOIN assignment_status s ON s.assignment_id = a.id "
        "WHERE a.workflow_state = 'published' "
        "ORDER BY (a.due_at IS NULL), a.due_at"
    ).fetchall()


def last_sync_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    def _one(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    return {
        "courses": _one("SELECT COUNT(*) FROM courses"),
        "assignments": _one("SELECT COUNT(*) FROM assignments"),
        "materials": _one("SELECT COUNT(*) FROM materials"),
        "plan_blocks_open": _one(
            "SELECT COUNT(*) FROM plan_blocks WHERE completed = 0"
        ),
    }

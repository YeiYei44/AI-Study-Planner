"""Mark a whole assignment done — distinct from log_session() (which
marks specific already-scheduled *blocks* done and records actual time).
This is the coarser, simpler action: "stop scheduling this at all,"
independent of whether time was ever logged against it. generate_plan()
excludes anything marked here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


class UnknownAssignmentError(ValueError):
    pass


@dataclass
class CompletionResult:
    assignment_name: str
    completed: bool
    blocks_marked_done: int


def set_completed(
    conn: sqlite3.Connection, assignment_id: int, completed: bool = True
) -> CompletionResult:
    a = conn.execute("SELECT * FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
    if not a:
        raise UnknownAssignmentError(f"No assignment with id {assignment_id}")

    now = datetime.now(timezone.utc).isoformat() if completed else None
    conn.execute(
        """
        INSERT INTO assignment_status (assignment_id, completed, completed_at)
        VALUES (?, ?, ?)
        ON CONFLICT(assignment_id) DO UPDATE SET
            completed = excluded.completed, completed_at = excluded.completed_at
        """,
        (assignment_id, 1 if completed else 0, now),
    )

    blocks_marked = 0
    if completed:
        cur = conn.execute(
            "UPDATE plan_blocks SET completed = 1 "
            "WHERE assignment_id = ? AND locked = 0 AND completed = 0",
            (assignment_id,),
        )
        blocks_marked = cur.rowcount

    conn.commit()
    return CompletionResult(
        assignment_name=a["name"], completed=completed, blocks_marked_done=blocks_marked
    )


def is_completed(conn: sqlite3.Connection, assignment_id: int) -> bool:
    row = conn.execute(
        "SELECT completed FROM assignment_status WHERE assignment_id = ?", (assignment_id,)
    ).fetchone()
    return bool(row and row["completed"])

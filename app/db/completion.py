"""Per-assignment planning status: two independent flags, both kept out
of generate_plan()'s scheduling entirely once set.

set_completed() marks a whole assignment done — distinct from
log_session() (which marks specific already-scheduled *blocks* done and
records actual time). This is the coarser, simpler action: "stop
scheduling this at all," independent of whether time was ever logged
against it.

set_skip_planning() marks an assignment as needing no home-prep time at
all — for in-class work (tests, quizzes, labs graded in person) that
still syncs in as an ordinary Canvas assignment and would otherwise get
scheduled study blocks nobody needs. Distinct from completed: skipping
doesn't mean it's done, just that `plan` shouldn't block out time for it.
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


@dataclass
class SkipResult:
    assignment_name: str
    skipped: bool
    blocks_removed: int


def set_skip_planning(
    conn: sqlite3.Connection, assignment_id: int, skip: bool = True
) -> SkipResult:
    a = conn.execute("SELECT * FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
    if not a:
        raise UnknownAssignmentError(f"No assignment with id {assignment_id}")

    conn.execute(
        """
        INSERT INTO assignment_status (assignment_id, skip_planning)
        VALUES (?, ?)
        ON CONFLICT(assignment_id) DO UPDATE SET skip_planning = excluded.skip_planning
        """,
        (assignment_id, 1 if skip else 0),
    )

    # Unlike completed blocks, these aren't marked done (nothing was
    # studied) — they're removed outright, since there's nothing to do
    # for them at all. Locked blocks are left alone, same as everywhere
    # else: locking is a stronger, more deliberate signal than a status
    # flag. Un-skipping doesn't recreate anything — re-run `plan`.
    blocks_removed = 0
    if skip:
        cur = conn.execute(
            "DELETE FROM plan_blocks WHERE assignment_id = ? AND locked = 0 AND completed = 0",
            (assignment_id,),
        )
        blocks_removed = cur.rowcount

    conn.commit()
    return SkipResult(assignment_name=a["name"], skipped=skip, blocks_removed=blocks_removed)


def is_skipped(conn: sqlite3.Connection, assignment_id: int) -> bool:
    row = conn.execute(
        "SELECT skip_planning FROM assignment_status WHERE assignment_id = ?", (assignment_id,)
    ).fetchone()
    return bool(row and row["skip_planning"])

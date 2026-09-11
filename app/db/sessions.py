"""Logging actual time spent — the ground truth calibration.py learns
from. Kept separate from db/sync.py since it's user-driven, not
sync-driven.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from app.planner.estimate import default_minutes


class UnknownAssignmentError(ValueError):
    pass


@dataclass
class LogResult:
    assignment_name: str
    blocks_marked_done: int


def log_session(
    conn: sqlite3.Connection, assignment_id: int, actual_minutes: int, note: str | None = None
) -> LogResult:
    """Record a session and mark that assignment's still-open scheduled
    blocks as completed, so the next `plan` doesn't re-offer time already
    spent. `estimated_minutes_at_log` is always the fixed, uncalibrated
    heuristic — see schema.py for why.
    """
    a = conn.execute("SELECT * FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
    if not a:
        raise UnknownAssignmentError(f"No assignment with id {assignment_id}")

    types = json.loads(a["submission_types"] or "[]")
    baseline = default_minutes(a["points_possible"], types)

    conn.execute(
        "INSERT INTO sessions "
        "(assignment_id, actual_minutes, estimated_minutes_at_log, submission_type, logged_at, note) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            assignment_id,
            actual_minutes,
            baseline,
            types[0] if types else None,
            datetime.now(timezone.utc).isoformat(),
            note,
        ),
    )
    cur = conn.execute(
        "UPDATE plan_blocks SET completed = 1 "
        "WHERE assignment_id = ? AND locked = 0 AND completed = 0",
        (assignment_id,),
    )
    conn.commit()
    return LogResult(assignment_name=a["name"], blocks_marked_done=cur.rowcount)

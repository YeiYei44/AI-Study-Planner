"""Effort estimation.

Three sources, in priority order:

1. ``basis='user'`` — hand-entered (`set_estimate`), always wins, never
   touched by calibration or re-estimation.
2. ``basis='llm'`` — a Claude estimate (see llm_estimate.py), cached
   against a content hash so it's only recomputed when the assignment
   actually changes.
3. ``default_minutes()`` — the placeholder heuristic, for anything not
   yet LLM-estimated.

Calibration (calibration.py) is applied on top of whichever of (2)/(3)
applies, never on (1) — a multiplier learned from logged actual time
adjusts the model's or the heuristic's guess, but never overrides what
the user explicitly said.
"""

from __future__ import annotations

import json
import sqlite3

from app.planner.calibration import get_multiplier

_MIN_MINUTES = 20
_MAX_MINUTES = 240
_LIGHT_TYPES = {"online_quiz"}


def default_minutes(points_possible: float | None, submission_types: list[str]) -> int:
    pts = points_possible if points_possible else 10
    minutes = 20 + pts * 3
    if any(t in _LIGHT_TYPES for t in submission_types):
        minutes *= 0.6
    minutes = round(minutes / 5) * 5  # round to a tidy 5-minute increment
    return int(max(_MIN_MINUTES, min(_MAX_MINUTES, minutes)))


def get_estimate_minutes(conn: sqlite3.Connection, assignment_row: sqlite3.Row) -> int:
    types = json.loads(assignment_row["submission_types"] or "[]")
    row = conn.execute(
        "SELECT minutes, basis FROM estimates WHERE assignment_id = ?",
        (assignment_row["id"],),
    ).fetchone()

    if row and row["basis"] == "user":
        return row["minutes"]

    base = row["minutes"] if row else default_minutes(assignment_row["points_possible"], types)
    return int(round(base * get_multiplier(conn, types)))


def set_estimate(
    conn: sqlite3.Connection, assignment_id: int, minutes: int, basis: str = "user"
) -> None:
    conn.execute(
        """
        INSERT INTO estimates (assignment_id, minutes, basis) VALUES (?, ?, ?)
        ON CONFLICT(assignment_id) DO UPDATE SET
            minutes = excluded.minutes, basis = excluded.basis
        """,
        (assignment_id, minutes, basis),
    )
    conn.commit()

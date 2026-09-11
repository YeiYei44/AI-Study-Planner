"""Diff incoming Canvas data against what's stored, then upsert it.

The diff runs against the row still in the DB from last time, *before*
that row gets overwritten — so a change event reflects a real
transition, not the value we're about to write over it.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.ingest.normalize import normalize_assignment, normalize_course


@dataclass
class Change:
    kind: str  # new | removed | due_changed | points_changed | name_changed
    course_id: int
    assignment_id: int | None
    title: str
    detail: str = ""

    def __str__(self) -> str:
        return f"[{self.kind}] {self.title}" + (f" — {self.detail}" if self.detail else "")


def _upsert_course(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO courses
            (id, name, course_code, term, workflow_state, source, raw_json, fetched_at)
        VALUES
            (:id, :name, :course_code, :term, :workflow_state, :source, :raw_json, :fetched_at)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, course_code=excluded.course_code, term=excluded.term,
            workflow_state=excluded.workflow_state, source=excluded.source,
            raw_json=excluded.raw_json, fetched_at=excluded.fetched_at
        """,
        row,
    )


def _upsert_assignment(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO assignments
            (id, course_id, name, description_html, due_at, points_possible,
             submission_types, html_url, workflow_state, source, raw_json, fetched_at)
        VALUES
            (:id, :course_id, :name, :description_html, :due_at, :points_possible,
             :submission_types, :html_url, :workflow_state, :source, :raw_json, :fetched_at)
        ON CONFLICT(id) DO UPDATE SET
            course_id=excluded.course_id, name=excluded.name,
            description_html=excluded.description_html, due_at=excluded.due_at,
            points_possible=excluded.points_possible,
            submission_types=excluded.submission_types, html_url=excluded.html_url,
            workflow_state=excluded.workflow_state, source=excluded.source,
            raw_json=excluded.raw_json, fetched_at=excluded.fetched_at
        """,
        row,
    )


def _diff_assignment(prev: sqlite3.Row | None, new: dict[str, Any]) -> list[Change]:
    course_id, aid, title = new["course_id"], new["id"], new["name"]

    if prev is None:
        detail = f"due {new['due_at']}" if new["due_at"] else "no due date"
        return [Change("new", course_id, aid, title, detail)]

    out: list[Change] = []
    if prev["due_at"] != new["due_at"]:
        out.append(
            Change("due_changed", course_id, aid, title,
                   f"{prev['due_at']} -> {new['due_at']}")
        )
    if prev["points_possible"] != new["points_possible"]:
        out.append(
            Change("points_changed", course_id, aid, title,
                   f"{prev['points_possible']} -> {new['points_possible']}")
        )
    if prev["name"] != new["name"]:
        out.append(Change("name_changed", course_id, aid, prev["name"], f"-> {new['name']}"))
    return out


def sync_courses_and_assignments(
    conn: sqlite3.Connection,
    courses: list[dict[str, Any]],
    assignments_by_course: dict[int, list[dict[str, Any]]],
    fetched_at: str,
) -> list[Change]:
    """Normalize, diff against what's stored, upsert, and commit.

    Returns every detected change. Assignments Canvas stopped returning
    for a course are reported as ``removed`` but not deleted — could mean
    unpublished, deleted, or just an enrollment change, and the row
    (including its raw_json history) is more useful kept than guessed
    away.
    """
    changes: list[Change] = []

    for raw_course in courses:
        _upsert_course(conn, normalize_course(raw_course, fetched_at))

    for course_id, raw_assignments in assignments_by_course.items():
        existing = {
            row["id"]: row
            for row in conn.execute(
                "SELECT * FROM assignments WHERE course_id = ?", (course_id,)
            )
        }
        seen_ids: set[int] = set()

        for raw in raw_assignments:
            row = normalize_assignment(raw, course_id, fetched_at)
            seen_ids.add(row["id"])
            changes.extend(_diff_assignment(existing.get(row["id"]), row))
            _upsert_assignment(conn, row)

        for old_id, prev in existing.items():
            if old_id not in seen_ids:
                changes.append(
                    Change("removed", course_id, old_id, prev["name"],
                           "no longer returned by Canvas")
                )

    conn.commit()
    return changes


def record_sync_run(
    conn: sqlite3.Connection,
    started_at: str,
    ended_at: str,
    ok: bool,
    course_count: int,
    assignment_count: int,
    changes: list[Change],
) -> None:
    conn.execute(
        """
        INSERT INTO sync_runs
            (started_at, ended_at, ok, course_count, assignment_count, changes_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            started_at,
            ended_at,
            1 if ok else 0,
            course_count,
            assignment_count,
            json.dumps([c.__dict__ for c in changes]),
        ),
    )
    conn.commit()

from pathlib import Path

import pytest

from app.config import Settings
from app.db.connection import connect
from app.db.sync import record_sync_run, sync_courses_and_assignments

COURSE = {"id": 1, "name": "Course A", "course_code": "A1", "workflow_state": "available"}


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(Settings(data_dir=tmp_path))
    yield c
    c.close()


def test_first_sync_reports_new_assignments(conn):
    a1 = {"id": 10, "name": "HW1", "due_at": "2026-09-15T23:59:00Z", "points_possible": 10}
    changes = sync_courses_and_assignments(conn, [COURSE], {1: [a1]}, "t0")
    assert len(changes) == 1
    assert changes[0].kind == "new"
    row = conn.execute("SELECT * FROM assignments WHERE id=10").fetchone()
    assert row["due_at"] == "2026-09-15T23:59:00Z"


def test_due_date_change_detected(conn):
    a1 = {"id": 10, "name": "HW1", "due_at": "2026-09-15T23:59:00Z"}
    sync_courses_and_assignments(conn, [COURSE], {1: [a1]}, "t0")
    changes = sync_courses_and_assignments(
        conn, [COURSE], {1: [{**a1, "due_at": "2026-09-20T23:59:00Z"}]}, "t1"
    )
    assert len(changes) == 1
    assert changes[0].kind == "due_changed"
    assert "2026-09-15" in changes[0].detail
    assert "2026-09-20" in changes[0].detail


def test_points_change_detected(conn):
    a1 = {"id": 10, "name": "HW1", "points_possible": 10}
    sync_courses_and_assignments(conn, [COURSE], {1: [a1]}, "t0")
    changes = sync_courses_and_assignments(
        conn, [COURSE], {1: [{**a1, "points_possible": 20}]}, "t1"
    )
    assert len(changes) == 1
    assert changes[0].kind == "points_changed"


def test_name_change_detected(conn):
    a1 = {"id": 10, "name": "HW1"}
    sync_courses_and_assignments(conn, [COURSE], {1: [a1]}, "t0")
    changes = sync_courses_and_assignments(
        conn, [COURSE], {1: [{**a1, "name": "HW1 (revised)"}]}, "t1"
    )
    assert len(changes) == 1
    assert changes[0].kind == "name_changed"


def test_no_changes_when_nothing_differs(conn):
    a1 = {"id": 10, "name": "HW1", "due_at": "2026-09-15T23:59:00Z", "points_possible": 10}
    sync_courses_and_assignments(conn, [COURSE], {1: [a1]}, "t0")
    changes = sync_courses_and_assignments(conn, [COURSE], {1: [dict(a1)]}, "t1")
    assert changes == []


def test_removed_assignment_detected_but_not_deleted(conn):
    a1, a2 = {"id": 10, "name": "HW1"}, {"id": 11, "name": "HW2"}
    sync_courses_and_assignments(conn, [COURSE], {1: [a1, a2]}, "t0")
    changes = sync_courses_and_assignments(conn, [COURSE], {1: [a1]}, "t1")
    assert len(changes) == 1
    assert changes[0].kind == "removed"
    assert changes[0].assignment_id == 11
    # still present — a diff event, not a deletion
    assert conn.execute("SELECT * FROM assignments WHERE id=11").fetchone() is not None


def test_record_sync_run(conn):
    record_sync_run(conn, "start", "end", True, 1, 2, [])
    row = conn.execute("SELECT * FROM sync_runs").fetchone()
    assert row["ok"] == 1
    assert row["course_count"] == 1
    assert row["assignment_count"] == 2

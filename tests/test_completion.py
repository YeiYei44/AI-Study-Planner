from pathlib import Path

import pytest

from app.config import Settings
from app.db.completion import UnknownAssignmentError, is_completed, set_completed
from app.db.connection import connect


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(Settings(data_dir=tmp_path))
    yield c
    c.close()


def _insert_assignment(conn, aid=1, points=50):
    conn.execute(
        "INSERT INTO courses (id, name, source, raw_json, fetched_at) "
        "VALUES (1, 'C', 's', '{}', 't') ON CONFLICT(id) DO NOTHING"
    )
    conn.execute(
        """
        INSERT INTO assignments
            (id, course_id, name, points_possible, workflow_state, source, raw_json, fetched_at)
        VALUES (?, 1, 'HW', ?, 'published', 's', '{}', 't')
        """,
        (aid, points),
    )
    conn.commit()


def test_unknown_assignment_raises(conn):
    with pytest.raises(UnknownAssignmentError):
        set_completed(conn, 999)


def test_marks_completed_and_records_timestamp(conn):
    _insert_assignment(conn)
    result = set_completed(conn, 1)
    assert result.assignment_name == "HW"
    assert result.completed is True
    assert is_completed(conn, 1)
    row = conn.execute("SELECT completed_at FROM assignment_status WHERE assignment_id = 1").fetchone()
    assert row["completed_at"] is not None


def test_undo_clears_completed_and_timestamp(conn):
    _insert_assignment(conn)
    set_completed(conn, 1)
    result = set_completed(conn, 1, completed=False)
    assert result.completed is False
    assert not is_completed(conn, 1)
    row = conn.execute("SELECT completed_at FROM assignment_status WHERE assignment_id = 1").fetchone()
    assert row["completed_at"] is None


def test_marks_open_blocks_done_but_not_locked_ones(conn):
    _insert_assignment(conn)
    conn.execute(
        "INSERT INTO plan_blocks (date, start, end, kind, assignment_id, locked, completed, generated_at) "
        "VALUES ('2026-01-01', '10:00', '10:45', 'assignment', 1, 0, 0, 't')"
    )
    conn.execute(
        "INSERT INTO plan_blocks (date, start, end, kind, assignment_id, locked, completed, generated_at) "
        "VALUES ('2026-01-02', '10:00', '10:45', 'assignment', 1, 1, 0, 't')"
    )
    conn.commit()

    result = set_completed(conn, 1)
    assert result.blocks_marked_done == 1

    rows = conn.execute("SELECT date, completed, locked FROM plan_blocks ORDER BY date").fetchall()
    assert rows[0]["completed"] == 1  # the open one
    assert rows[1]["completed"] == 0  # the locked one, left alone


def test_is_completed_false_when_never_set(conn):
    _insert_assignment(conn)
    assert not is_completed(conn, 1)

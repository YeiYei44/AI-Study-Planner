from pathlib import Path

import pytest

from app.config import Settings
from app.db.connection import connect
from app.db.sessions import UnknownAssignmentError, log_session


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(Settings(data_dir=tmp_path))
    yield c
    c.close()


def _insert_assignment(conn, aid=1, points=50, types='["online_upload"]'):
    conn.execute(
        "INSERT INTO courses (id, name, source, raw_json, fetched_at) "
        "VALUES (1, 'C', 's', '{}', 't') ON CONFLICT(id) DO NOTHING"
    )
    conn.execute(
        """
        INSERT INTO assignments
            (id, course_id, name, points_possible, workflow_state,
             submission_types, source, raw_json, fetched_at)
        VALUES (?, 1, 'HW', ?, 'published', ?, 's', '{}', 't')
        """,
        (aid, points, types),
    )
    conn.commit()


def test_unknown_assignment_raises(conn):
    with pytest.raises(UnknownAssignmentError):
        log_session(conn, 999, 30)


def test_logs_a_session_row(conn):
    _insert_assignment(conn)
    result = log_session(conn, 1, 45, note="felt long")
    assert result.assignment_name == "HW"
    row = conn.execute("SELECT * FROM sessions WHERE assignment_id = 1").fetchone()
    assert row["actual_minutes"] == 45
    assert row["submission_type"] == "online_upload"
    assert row["note"] == "felt long"
    assert row["estimated_minutes_at_log"] > 0


def test_marks_open_blocks_completed_but_not_locked_ones(conn):
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

    result = log_session(conn, 1, 45)
    assert result.blocks_marked_done == 1

    rows = conn.execute("SELECT date, completed, locked FROM plan_blocks ORDER BY date").fetchall()
    assert rows[0]["completed"] == 1  # the open one
    assert rows[1]["completed"] == 0  # the locked one, left alone

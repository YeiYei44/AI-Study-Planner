from pathlib import Path

import pytest

from app.config import Settings
from app.db.connection import connect
from app.planner.estimate import default_minutes, get_estimate_minutes, set_estimate


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(Settings(data_dir=tmp_path))
    yield c
    c.close()


def test_default_minutes_scales_with_points():
    assert default_minutes(10, []) < default_minutes(100, [])


def test_default_minutes_clamped():
    assert default_minutes(0, []) >= 20
    assert default_minutes(10_000, []) <= 240


def test_default_minutes_lighter_for_quizzes():
    assert default_minutes(50, ["online_quiz"]) < default_minutes(50, ["online_upload"])


def _insert_assignment(conn, aid: int, points, submission_types_json: str):
    conn.execute(
        "INSERT INTO courses (id, name, source, raw_json, fetched_at) "
        "VALUES (1, 'C', 's', '{}', 't') ON CONFLICT(id) DO NOTHING"
    )
    conn.execute(
        """
        INSERT INTO assignments
            (id, course_id, name, points_possible, submission_types, source, raw_json, fetched_at)
        VALUES (?, 1, 'A', ?, ?, 's', '{}', 't')
        """,
        (aid, points, submission_types_json),
    )
    conn.commit()


def test_get_estimate_falls_back_to_default(conn):
    _insert_assignment(conn, 1, 50, "[]")
    row = conn.execute("SELECT * FROM assignments WHERE id=1").fetchone()
    assert get_estimate_minutes(conn, row) == default_minutes(50, [])


def test_user_estimate_overrides_default(conn):
    _insert_assignment(conn, 1, 50, "[]")
    set_estimate(conn, 1, 123, basis="user")
    row = conn.execute("SELECT * FROM assignments WHERE id=1").fetchone()
    assert get_estimate_minutes(conn, row) == 123

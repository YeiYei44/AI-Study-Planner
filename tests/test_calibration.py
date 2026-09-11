from pathlib import Path

import pytest

from app.config import Settings
from app.db.connection import connect
from app.planner.calibration import MIN_SAMPLES, get_multiplier, multipliers_by_type


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(Settings(data_dir=tmp_path))
    yield c
    c.close()


def _log(conn, actual, estimated, submission_type):
    conn.execute(
        "INSERT INTO sessions (assignment_id, actual_minutes, estimated_minutes_at_log, "
        "submission_type, logged_at) VALUES (NULL, ?, ?, ?, 't')",
        (actual, estimated, submission_type),
    )
    conn.commit()


def test_no_sessions_means_no_multipliers(conn):
    assert multipliers_by_type(conn) == {}
    assert get_multiplier(conn, ["online_upload"]) == 1.0


def test_multiplier_computed_from_ratio(conn):
    _log(conn, 100, 50, "online_upload")  # 2x
    _log(conn, 60, 50, "online_upload")  # 1.2x
    _log(conn, 80, 50, "online_upload")  # 1.6x
    mult, n = multipliers_by_type(conn)["online_upload"]
    assert n == 3
    assert mult == pytest.approx((2 + 1.2 + 1.6) / 3)


def test_not_applied_below_min_samples(conn):
    for _ in range(MIN_SAMPLES - 1):
        _log(conn, 100, 50, "online_quiz")
    assert get_multiplier(conn, ["online_quiz"]) == 1.0  # cold start, not enough yet


def test_applied_once_min_samples_reached(conn):
    for _ in range(MIN_SAMPLES):
        _log(conn, 25, 50, "online_quiz")  # consistently 0.5x
    assert get_multiplier(conn, ["online_quiz"]) == pytest.approx(0.5)


def test_averages_across_multiple_types(conn):
    for _ in range(MIN_SAMPLES):
        _log(conn, 100, 50, "a")  # 2.0x
        _log(conn, 50, 50, "b")  # 1.0x
    assert get_multiplier(conn, ["a", "b"]) == pytest.approx(1.5)


def test_unknown_type_ignored_not_zeroed(conn):
    for _ in range(MIN_SAMPLES):
        _log(conn, 100, 50, "a")  # 2.0x
    # "b" has no data at all; shouldn't drag the average toward 1.0 or 0
    assert get_multiplier(conn, ["a", "b"]) == pytest.approx(2.0)


def test_zero_baseline_excluded_no_division_error(conn):
    conn.execute(
        "INSERT INTO sessions (assignment_id, actual_minutes, estimated_minutes_at_log, "
        "submission_type, logged_at) VALUES (NULL, 10, 0, 'x', 't')"
    )
    conn.commit()
    assert multipliers_by_type(conn) == {}

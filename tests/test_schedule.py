from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.db.connection import connect
from app.planner.availability import set_weekly
from app.planner.estimate import set_estimate
from app.planner.schedule import BLOCK_MINUTES, generate_plan, write_plan

MONDAY = date.fromisocalendar(2026, 10, 1)  # a known Monday
# The "current moment" tests run against — kept consistent with MONDAY so
# generate_plan's due-date liveness check (which runs off `now`, not the
# `today` used for slot placement) doesn't silently disagree with it.
NOW = datetime.combine(MONDAY, time(6, 0), tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(Settings(data_dir=tmp_path))
    yield c
    c.close()


def _insert_assignment(conn, aid: int, name: str, due_at: str, points=50):
    conn.execute(
        "INSERT INTO courses (id, name, source, raw_json, fetched_at) "
        "VALUES (1, 'C', 's', '{}', 't') ON CONFLICT(id) DO NOTHING"
    )
    conn.execute(
        """
        INSERT INTO assignments
            (id, course_id, name, due_at, points_possible, workflow_state,
             submission_types, source, raw_json, fetched_at)
        VALUES (?, 1, ?, ?, ?, 'published', '[]', 's', '{}', 't')
        """,
        (aid, name, due_at, points),
    )
    conn.commit()


def _weekdays(conn, start="16:00", end="19:00"):
    for dow in range(5):  # Mon-Fri
        set_weekly(conn, dow, start, end)


def _plan(conn, **kwargs):
    kwargs.setdefault("today", MONDAY)
    kwargs.setdefault("now", NOW)
    return generate_plan(conn, **kwargs)


def test_no_availability_means_no_blocks(conn):
    _insert_assignment(conn, 1, "HW1", (MONDAY + timedelta(days=3)).isoformat() + "T23:59:00Z")
    blocks, shortfalls = _plan(conn, horizon_days=7)
    assert blocks == []


def test_buffer_reserved_each_available_day(conn):
    _weekdays(conn)  # 16:00-19:00 = 180min = 4 blocks/day
    blocks, _ = _plan(conn, horizon_days=5)
    buffer_days = {b.date for b in blocks if b.kind == "buffer"}
    # Mon-Fri within the 5-day horizon should each have exactly one buffer block
    assert buffer_days == {MONDAY + timedelta(days=i) for i in range(5)}


def test_assignment_gets_scheduled_before_deadline(conn):
    _weekdays(conn)
    due = MONDAY + timedelta(days=3)  # Thursday
    _insert_assignment(conn, 1, "Essay", due.isoformat() + "T23:59:00Z", points=90)
    set_estimate(conn, 1, 90)  # exactly 2 blocks

    blocks, shortfalls = _plan(conn, horizon_days=7)
    assigned = [b for b in blocks if b.assignment_id == 1]
    assert len(assigned) == 2
    assert shortfalls == []
    # 12h safety margin pulls the deadline day back to Wednesday
    assert all(b.date <= MONDAY + timedelta(days=2) for b in assigned)


def test_shortfall_reported_when_not_enough_room(conn):
    set_weekly(conn, MONDAY.weekday(), "16:00", "16:45")  # exactly 1 block, today only
    due = datetime.combine(MONDAY, time(20, 0), tzinfo=timezone.utc)  # due later today
    _insert_assignment(conn, 1, "Cram", due.isoformat(), points=50)
    set_estimate(conn, 1, 300)  # way more than one block can cover

    blocks, shortfalls = _plan(conn, horizon_days=3)
    assert len(shortfalls) == 1
    assert shortfalls[0][0] == "Cram"
    assert shortfalls[0][1] > 0


def test_higher_priority_assignment_scheduled_first(conn):
    # Only one block/day available; two assignments both fit only one.
    set_weekly(conn, MONDAY.weekday(), "16:00", "16:45")
    set_weekly(conn, (MONDAY + timedelta(days=1)).weekday(), "16:00", "16:45")
    urgent_due = (MONDAY + timedelta(days=1)).isoformat() + "T23:59:00Z"
    later_due = (MONDAY + timedelta(days=6)).isoformat() + "T23:59:00Z"
    _insert_assignment(conn, 1, "Urgent", urgent_due, points=100)
    _insert_assignment(conn, 2, "LaterLowValue", later_due, points=10)
    set_estimate(conn, 1, 45)
    set_estimate(conn, 2, 45)

    blocks, _ = _plan(conn, horizon_days=7)
    first_assigned = next(b for b in blocks if b.kind == "assignment")
    assert first_assigned.assignment_id == 1


def test_write_plan_preserves_locked_and_completed(conn):
    _weekdays(conn)
    conn.execute(
        "INSERT INTO plan_blocks (date, start, end, kind, locked, completed, generated_at) "
        "VALUES ('2026-01-01', '10:00', '10:45', 'assignment', 1, 0, 't')"
    )
    conn.execute(
        "INSERT INTO plan_blocks (date, start, end, kind, locked, completed, generated_at) "
        "VALUES ('2026-01-02', '10:00', '10:45', 'assignment', 0, 1, 't')"
    )
    conn.commit()

    blocks, _ = _plan(conn, horizon_days=3)
    write_plan(conn, blocks)

    remaining_special = conn.execute(
        "SELECT date, locked, completed FROM plan_blocks WHERE locked=1 OR completed=1"
    ).fetchall()
    assert {r["date"] for r in remaining_special} == {"2026-01-01", "2026-01-02"}


def test_block_length_is_45_minutes():
    assert BLOCK_MINUTES == 45

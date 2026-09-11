import pytest

from app.planner.availability import AvailabilitySpecError, parse_availability_spec


def test_single_day():
    assert parse_availability_spec("sat 10:00-16:00") == [(5, "10:00", "16:00")]


def test_day_range():
    got = parse_availability_spec("mon-fri 16:00-19:00")
    assert got == [(d, "16:00", "19:00") for d in range(0, 5)]


def test_wraparound_range():
    # fri-mon wraps past Sunday back to Monday
    got = parse_availability_spec("fri-mon 18:00-20:00")
    assert [d for d, _, _ in got] == [4, 5, 6, 0]


def test_bad_format_rejected():
    with pytest.raises(AvailabilitySpecError):
        parse_availability_spec("whenever I feel like it")


def test_unknown_day_rejected():
    with pytest.raises(AvailabilitySpecError):
        parse_availability_spec("funday 10:00-11:00")


def test_start_after_end_rejected():
    with pytest.raises(AvailabilitySpecError):
        parse_availability_spec("mon 19:00-16:00")

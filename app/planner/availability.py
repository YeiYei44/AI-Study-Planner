"""Weekly study-availability template.

v1: a recurring weekly pattern only (no blackout/exception dates yet —
see docs/DESIGN.md for that as a known follow-up).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, time

_DOW = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_SPEC_RE = re.compile(
    r"^\s*(?P<d1>[a-z]{3})(?:-(?P<d2>[a-z]{3}))?\s+"
    r"(?P<start>\d{1,2}:\d{2})\s*-\s*(?P<end>\d{1,2}:\d{2})\s*$",
    re.IGNORECASE,
)


class AvailabilitySpecError(ValueError):
    pass


def parse_availability_spec(spec: str) -> list[tuple[int, str, str]]:
    """Parse e.g. "mon-fri 16:00-19:00" or "sat 10:00-16:00" into
    ``(dow, start, end)`` tuples, one per day in the range."""
    m = _SPEC_RE.match(spec)
    if not m:
        raise AvailabilitySpecError(
            f'Expected "<day>[-<day>] HH:MM-HH:MM", e.g. "mon-fri 16:00-19:00" '
            f'or "sat 10:00-16:00" — got {spec!r}'
        )
    d1, d2, start, end = m.group("d1", "d2", "start", "end")
    try:
        i1 = _DOW[d1.lower()]
        i2 = _DOW[d2.lower()] if d2 else i1
    except KeyError as e:
        raise AvailabilitySpecError(f"Unknown day {e}. Use mon/tue/wed/thu/fri/sat/sun.")
    if time.fromisoformat(start) >= time.fromisoformat(end):
        raise AvailabilitySpecError(f"Start must be before end: {start}-{end}")
    dows = (
        list(range(i1, i2 + 1)) if i1 <= i2 else list(range(i1, 7)) + list(range(0, i2 + 1))
    )
    return [(d, start, end) for d in dows]


def set_weekly(conn: sqlite3.Connection, dow: int, start: str, end: str) -> None:
    conn.execute("INSERT INTO availability (dow, start, end) VALUES (?, ?, ?)", (dow, start, end))
    conn.commit()


def clear_weekly(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM availability")
    conn.commit()


def list_weekly(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM availability ORDER BY dow, start").fetchall()


def windows_for_day(conn: sqlite3.Connection, day: date) -> list[tuple[time, time]]:
    rows = conn.execute(
        "SELECT start, end FROM availability WHERE dow = ? ORDER BY start", (day.weekday(),)
    ).fetchall()
    return [(time.fromisoformat(r["start"]), time.fromisoformat(r["end"])) for r in rows]

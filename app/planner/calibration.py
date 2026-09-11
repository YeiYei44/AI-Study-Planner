"""Per-type pace calibration: how actual logged time compares to the
fixed `default_minutes()` heuristic, learned from `sessions`.

Computed live on every read rather than stored — cheap at this data
scale, and it sidesteps the whole "when do I recompute, and is it stale"
question. Grouped by Canvas's `submission_types` (not course subject —
that field is free text Canvas doesn't standardize, submission type is
the sturdier signal and the same one `default_minutes()` already keys
off) and averaged across a multi-type assignment's types.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

MIN_SAMPLES = 3


def multipliers_by_type(conn: sqlite3.Connection) -> dict[str, tuple[float, int]]:
    """submission_type -> (multiplier, sample_count), regardless of
    whether a type has enough samples yet to actually be applied."""
    rows = conn.execute(
        "SELECT submission_type, actual_minutes, estimated_minutes_at_log FROM sessions "
        "WHERE submission_type IS NOT NULL AND estimated_minutes_at_log > 0"
    ).fetchall()
    groups: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        groups[r["submission_type"]].append(r["actual_minutes"] / r["estimated_minutes_at_log"])
    return {t: (sum(v) / len(v), len(v)) for t, v in groups.items()}


def get_multiplier(conn: sqlite3.Connection, submission_types: list[str]) -> float:
    """Average multiplier across an assignment's types. 1.0 (no
    adjustment — cold start) until a type has MIN_SAMPLES logged
    sessions."""
    table = multipliers_by_type(conn)
    mults = [
        mult
        for t in submission_types
        if (row := table.get(t)) and row[1] >= MIN_SAMPLES
        for mult in [row[0]]
    ]
    return sum(mults) / len(mults) if mults else 1.0

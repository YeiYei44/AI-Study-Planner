"""Deterministic backward-from-due-date scheduler.

Given effort estimates (an LLM's job, or hand-entered — see estimate.py)
and weekly availability, this is plain, testable code: no model calls,
runs in milliseconds, same input always gives the same plan.

v1 scope, stated plainly:
* fixed 45-minute blocks, not variable-length slicing — simpler to place
  correctly, and a consistent block length is easier to actually keep to
* one reserved buffer block per day, not a percentage
* weekly availability template only; no blackout dates yet
* an assignment marked fully done (app/db/completion.py's
  assignment_status, distinct from log_session()'s per-block completed
  flag) is excluded outright — closing the gap this docstring used to
  flag here. What's still open: *partial* progress isn't accounted for.
  Logging some time via log_session() marks whichever blocks already
  existed as done, but doesn't reduce what a later regeneration thinks
  is still needed — it'll schedule the assignment's full estimate again
  from scratch rather than the remainder. Only "fully done" is tracked,
  not "X minutes in."
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from app.planner.availability import windows_for_day
from app.planner.estimate import get_estimate_minutes

BLOCK_MINUTES = 45
BUFFER_BLOCKS_PER_DAY = 1
SAFETY_MARGIN = timedelta(hours=12)


@dataclass
class PlanBlock:
    date: date
    start: time
    end: time
    kind: str  # "assignment" | "buffer"
    assignment_id: int | None
    title: str = ""


def _slice_day(day: date, windows: list[tuple[time, time]]) -> list[tuple[time, time]]:
    """Chop a day's availability windows into fixed-size blocks."""
    blocks: list[tuple[time, time]] = []
    for start, end in windows:
        cur = datetime.combine(day, start)
        end_dt = datetime.combine(day, end)
        while cur + timedelta(minutes=BLOCK_MINUTES) <= end_dt:
            nxt = cur + timedelta(minutes=BLOCK_MINUTES)
            blocks.append((cur.time(), nxt.time()))
            cur = nxt
    return blocks


def generate_plan(
    conn: sqlite3.Connection,
    horizon_days: int = 14,
    today: date | None = None,
    now: datetime | None = None,
) -> tuple[list[PlanBlock], list[tuple[str, int]]]:
    """Returns (plan_blocks, shortfalls). A shortfall is (assignment
    title, minutes short) — not enough room before its deadline.

    ``now`` is the single source of truth for "the current moment"; if
    ``today`` isn't given it's derived from ``now``, so the two can never
    silently disagree (as they would if each defaulted to the wall clock
    independently — harmless in real use since both default together, but
    it matters for tests that need to fix "today" without also fixing
    "now" to something inconsistent with it).
    """
    now = now or datetime.now(timezone.utc)
    today = today or now.date()
    horizon_end = today + timedelta(days=horizon_days - 1)

    plan: list[PlanBlock] = []
    day_slots: dict[date, list[tuple[time, time]]] = {}

    for i in range(horizon_days):
        day = today + timedelta(days=i)
        blocks = _slice_day(day, windows_for_day(conn, day))
        if len(blocks) > BUFFER_BLOCKS_PER_DAY:
            buffer_blocks = blocks[-BUFFER_BLOCKS_PER_DAY:]
            blocks = blocks[:-BUFFER_BLOCKS_PER_DAY]
        else:
            buffer_blocks = []
        for start, end in buffer_blocks:
            plan.append(PlanBlock(day, start, end, "buffer", None, "buffer"))
        day_slots[day] = blocks

    assignments = conn.execute(
        "SELECT a.* FROM assignments a "
        "LEFT JOIN assignment_status s ON s.assignment_id = a.id "
        "WHERE a.due_at IS NOT NULL AND a.workflow_state = 'published' "
        "AND COALESCE(s.completed, 0) = 0"
    ).fetchall()

    candidates = []
    for a in assignments:
        try:
            due = datetime.fromisoformat(a["due_at"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if due <= now:
            continue
        deadline_day = max((due - SAFETY_MARGIN).date(), today)
        if deadline_day > horizon_end:
            deadline_day = horizon_end  # still try to start it within the horizon
        days_until = max((due.date() - today).days, 0.5)
        priority = (a["points_possible"] or 10) / days_until
        candidates.append((priority, a, deadline_day))

    candidates.sort(key=lambda c: -c[0])

    shortfalls: list[tuple[str, int]] = []
    for _priority, a, deadline_day in candidates:
        minutes_needed = get_estimate_minutes(conn, a)
        blocks_needed = -(-minutes_needed // BLOCK_MINUTES)  # ceil division

        placed = 0
        day = today
        while day <= deadline_day and placed < blocks_needed:
            slots = day_slots.get(day, [])
            while slots and placed < blocks_needed:
                start, end = slots.pop(0)
                plan.append(PlanBlock(day, start, end, "assignment", a["id"], a["name"]))
                placed += 1
            day += timedelta(days=1)

        if placed < blocks_needed:
            shortfalls.append((a["name"], (blocks_needed - placed) * BLOCK_MINUTES))

    plan.sort(key=lambda b: (b.date, b.start))
    return plan, shortfalls


def write_plan(conn: sqlite3.Connection, blocks: list[PlanBlock]) -> None:
    """Replace the open plan (unlocked, incomplete) with a freshly
    generated one. Locked and completed blocks are left untouched —
    regenerating never erases what's already done or pinned."""
    conn.execute("DELETE FROM plan_blocks WHERE locked = 0 AND completed = 0")
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """
        INSERT INTO plan_blocks
            (date, start, end, kind, assignment_id, locked, completed, generated_at)
        VALUES (?, ?, ?, ?, ?, 0, 0, ?)
        """,
        [
            (
                b.date.isoformat(),
                b.start.isoformat(timespec="minutes"),
                b.end.isoformat(timespec="minutes"),
                b.kind,
                b.assignment_id,
                now,
            )
            for b in blocks
        ],
    )
    conn.commit()

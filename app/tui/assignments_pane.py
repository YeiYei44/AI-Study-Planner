"""Assignments: the CLI's `estimate` / `complete` / `log` in one table
instead of three separate one-shot commands — select a row, press a key.

Three groups, in this order: **missing** (past due, not done — the most
urgent, so they sort to the very top regardless of how the rest of the
table is ordered), the regular upcoming/undated list, then **completed**
at the bottom, out of the way once it's done. Plain header rows (a label
in the Assignment column, keyed "header-*" rather than an assignment id,
each labeled with its own count) mark the boundaries —
`_selected_assignment_id()` returns None for them, so an action pressed
on a header row is a no-op rather than a crash. An "UPCOMING (n)" header
also appears — but only when MISSING is present above it — so a MISSING
section is never mistaken for "everything below this is also missing";
with no MISSING section the plain list needs no boundary marker at all.

Uses ``push_screen_wait`` (an async call inside a ``@work`` method) for
the input prompts rather than callback-passing: chaining "read minutes,
validate, then act" reads top-to-bottom instead of nested through a
callback.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from rich.markup import escape as esc
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from app.db.completion import UnknownAssignmentError as CompletionUnknownError
from app.db.completion import set_completed
from app.db.sessions import UnknownAssignmentError as SessionUnknownError
from app.db.sessions import log_session
from app.planner.estimate import set_estimate
from app.tui.modals import TextInputModal
from app.tui.queries import all_assignments_with_status


def _parse_due(due_at: str | None) -> datetime | None:
    if not due_at:
        return None
    try:
        return datetime.fromisoformat(due_at.replace("Z", "+00:00"))
    except ValueError:
        return None


class AssignmentsPane(Vertical):
    BINDINGS = [
        ("r", "refresh_view", "Refresh"),
        ("c", "toggle_complete", "Toggle done"),
        ("e", "set_estimate_action", "Set estimate"),
        ("l", "log_time_action", "Log time"),
    ]

    def __init__(self, conn: sqlite3.Connection):
        super().__init__()
        self._conn = conn

    def compose(self) -> ComposeResult:
        yield Static(id="assignments-status")
        yield DataTable(id="assignments-table")

    def on_mount(self) -> None:
        self.action_refresh_view()

    def focus_default(self) -> None:
        self.query_one("#assignments-table", DataTable).focus()

    def _add_row(self, table: DataTable, a: sqlite3.Row) -> None:
        due = a["due_at"][:16].replace("T", " ") if a["due_at"] else "-"
        pts = "-" if a["points_possible"] is None else str(a["points_possible"])
        if a["estimate_minutes"] is None:
            est = "-"
        else:
            est = f"{a['estimate_minutes']}m" + (" (you)" if a["estimate_basis"] == "user" else "")
        done = "✓" if a["completed"] else ""
        table.add_row(due, a["course_name"], a["name"], pts, est, done, key=str(a["id"]))

    def action_refresh_view(self, message: str = "") -> None:
        table = self.query_one("#assignments-table", DataTable)
        prior_coord = table.cursor_coordinate
        table.clear(columns=True)
        table.add_columns("Due", "Course", "Assignment", "Pts", "Estimate", "Done")

        now = datetime.now(timezone.utc)
        missing, upcoming, completed = [], [], []
        for a in all_assignments_with_status(self._conn):
            if a["completed"]:
                completed.append(a)
                continue
            due = _parse_due(a["due_at"])
            (missing if due is not None and due < now else upcoming).append(a)

        if missing:
            table.add_row(
                "", "", Text(f"⚠ MISSING ({len(missing)})", style="bold red"),
                "", "", "", key="header-missing",
            )
            for a in missing:
                self._add_row(table, a)
            # Only needed as a boundary marker once there's a MISSING
            # section above it to be confused with — otherwise "row 0
            # is the first real item" stays true, which every other
            # action (and the cursor-preserving move_cursor below)
            # already assumes.
            table.add_row(
                "", "", Text(f"UPCOMING ({len(upcoming)})", style="bold"),
                "", "", "", key="header-upcoming",
            )
        for a in upcoming:
            self._add_row(table, a)
        if completed:
            table.add_row(
                "", "", Text(f"✓ COMPLETED ({len(completed)})", style="bold dim"),
                "", "", "", key="header-completed",
            )
            for a in completed:
                self._add_row(table, a)

        status = message or (
            f"{len(missing) + len(upcoming)} active ({len(missing)} missing) · "
            f"{len(completed)} completed. 'c' toggle done, 'e' set estimate, 'l' log time."
        )
        self.query_one("#assignments-status", Static).update(status)
        if table.row_count:
            table.move_cursor(row=min(prior_coord.row, table.row_count - 1))

    def _selected_assignment_id(self) -> int | None:
        table = self.query_one("#assignments-table", DataTable)
        if table.row_count == 0:
            return None
        row_key, _col_key = table.coordinate_to_cell_key(table.cursor_coordinate)
        try:
            return int(row_key.value)
        except (TypeError, ValueError):
            return None  # a header row, not an assignment

    @work(exclusive=True)
    async def action_toggle_complete(self) -> None:
        aid = self._selected_assignment_id()
        if aid is None:
            return
        row = next((a for a in all_assignments_with_status(self._conn) if a["id"] == aid), None)
        if row is None:
            return

        marking_done = not row["completed"]
        log_note = ""
        if marking_done:
            # Git-commit-style: marking something done is the natural
            # moment to also record what it took, rather than a separate
            # trip to `log`. Escape cancels the whole action (nothing
            # marked done); submitting blank marks it done without
            # logging time.
            value = await self.app.push_screen_wait(
                TextInputModal(
                    "Minutes spent? (blank to mark done without logging time)",
                    placeholder="e.g. 45",
                )
            )
            if value is None:
                return
            if value:
                try:
                    minutes = int(value)
                except ValueError:
                    log_note = f" [red]Not logged — {esc(value)!r} isn't a number.[/]"
                else:
                    log_session(self._conn, aid, minutes)
                    log_note = f" Logged {minutes} min."

        try:
            result = set_completed(self._conn, aid, completed=marking_done)
        except CompletionUnknownError:
            return
        verb = "done" if result.completed else "not done"
        self.action_refresh_view(f'Marked "{esc(result.assignment_name)}" {verb}.{log_note}')

    @work(exclusive=True)
    async def action_set_estimate_action(self) -> None:
        aid = self._selected_assignment_id()
        if aid is None:
            return
        value = await self.app.push_screen_wait(
            TextInputModal("Your estimate, in minutes:", placeholder="e.g. 45")
        )
        if not value:
            return
        try:
            minutes = int(value)
        except ValueError:
            self.action_refresh_view(f"[red]Not a number:[/] {esc(value)!r}")
            return
        set_estimate(self._conn, aid, minutes, basis="user")
        self.action_refresh_view(f"Estimate set to {minutes} min.")

    @work(exclusive=True)
    async def action_log_time_action(self) -> None:
        aid = self._selected_assignment_id()
        if aid is None:
            return
        value = await self.app.push_screen_wait(
            TextInputModal("Actual minutes spent:", placeholder="e.g. 60")
        )
        if not value:
            return
        try:
            minutes = int(value)
        except ValueError:
            self.action_refresh_view(f"[red]Not a number:[/] {esc(value)!r}")
            return
        try:
            result = log_session(self._conn, aid, minutes)
        except SessionUnknownError:
            return
        self.action_refresh_view(f'Logged {minutes} min on "{esc(result.assignment_name)}".')

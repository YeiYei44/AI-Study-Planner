"""Dashboard: what's due soon, what's scheduled soon, and whether the
last sync actually worked — the "should I even be worried right now"
screen.
"""

from __future__ import annotations

import sqlite3

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from app.tui.queries import counts, last_sync_run, upcoming_assignments, upcoming_plan_blocks


class DashboardPane(Vertical):
    BINDINGS = [("r", "refresh_view", "Refresh")]

    def __init__(self, conn: sqlite3.Connection):
        super().__init__()
        self._conn = conn

    def compose(self) -> ComposeResult:
        yield Static(id="status-line")
        yield Static("[b]Upcoming assignments[/b]")
        yield DataTable(id="dash-assignments")
        yield Static("[b]Scheduled this week[/b]")
        yield DataTable(id="dash-plan")

    def on_mount(self) -> None:
        self.action_refresh_view()

    def focus_default(self) -> None:
        self.query_one("#dash-assignments", DataTable).focus()

    def action_refresh_view(self) -> None:
        self._render_status()
        self._render_assignments()
        self._render_plan()

    def _render_status(self) -> None:
        c = counts(self._conn)
        run = last_sync_run(self._conn)
        if run is None:
            sync_text = "[yellow]never synced — run `sync` from the CLI[/]"
        elif run["ok"]:
            when = (run["ended_at"] or run["started_at"])[:16].replace("T", " ")
            sync_text = f"[green]last sync ok[/] at {when}"
        else:
            sync_text = "[red]last sync failed[/] — check the CLI output"
        self.query_one("#status-line", Static).update(
            f"{c['courses']} courses · {c['assignments']} assignments · "
            f"{c['materials']} materials · {c['plan_blocks_open']} open plan blocks   "
            f"[dim]|[/]   {sync_text}"
        )

    def _render_assignments(self) -> None:
        table = self.query_one("#dash-assignments", DataTable)
        table.clear(columns=True)
        table.add_columns("Due", "Course", "Assignment", "Pts")
        for a in upcoming_assignments(self._conn, limit=10):
            due = a["due_at"][:16].replace("T", " ") if a["due_at"] else "-"
            pts = "-" if a["points_possible"] is None else str(a["points_possible"])
            table.add_row(due, a["course_name"], a["name"], pts)

    def _render_plan(self) -> None:
        table = self.query_one("#dash-plan", DataTable)
        table.clear(columns=True)
        table.add_columns("Date", "Time", "Block")
        for b in upcoming_plan_blocks(self._conn, days=7):
            label = "buffer" if b["kind"] == "buffer" else (b["assignment_name"] or "?")
            mark = "✓ " if b["completed"] else ("🔒 " if b["locked"] else "")
            table.add_row(b["date"], f"{b['start']}-{b['end']}", f"{mark}{label}")

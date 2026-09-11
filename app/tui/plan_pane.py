"""Plan: the full open (unlocked, incomplete) schedule, with an in-place
regenerate — the same generate_plan()/write_plan() the `plan` CLI command
uses, just without having to leave the TUI to re-run it.
"""

from __future__ import annotations

import sqlite3

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from app.planner.availability import list_weekly
from app.planner.schedule import generate_plan, write_plan
from app.tui.queries import upcoming_plan_blocks


class PlanPane(Vertical):
    BINDINGS = [("r", "regenerate", "Regenerate plan")]

    def __init__(self, conn: sqlite3.Connection):
        super().__init__()
        self._conn = conn

    def compose(self) -> ComposeResult:
        yield Static(id="plan-status")
        yield DataTable(id="plan-table")

    def on_mount(self) -> None:
        self.refresh_data()

    def focus_default(self) -> None:
        self.query_one("#plan-table", DataTable).focus()

    def refresh_data(self, message: str = "") -> None:
        table = self.query_one("#plan-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Date", "Time", "Block")
        blocks = upcoming_plan_blocks(self._conn, days=30)
        for b in blocks:
            label = "buffer" if b["kind"] == "buffer" else (b["assignment_name"] or "?")
            mark = "✓ " if b["completed"] else ("🔒 " if b["locked"] else "")
            table.add_row(b["date"], f"{b['start']}-{b['end']}", f"{mark}{label}")

        status = message or f"{len(blocks)} blocks scheduled. Press 'r' to regenerate."
        self.query_one("#plan-status", Static).update(status)

    def action_regenerate(self) -> None:
        if not list_weekly(self._conn):
            self.refresh_data(
                '[yellow]No availability set — nothing to plan into. '
                'Set it from the CLI: availability --add "mon-fri 16:00-19:00"[/]'
            )
            return
        blocks, shortfalls = generate_plan(self._conn, horizon_days=14)
        write_plan(self._conn, blocks)
        msg = f"[green]Regenerated: {len(blocks)} blocks.[/]"
        if shortfalls:
            msg += f" [red]{len(shortfalls)} assignment(s) didn't fully fit.[/]"
        self.refresh_data(msg)

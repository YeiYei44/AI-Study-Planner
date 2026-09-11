"""Settings: weekly availability — the CLI's `availability --add`/`--clear`
in the TUI. Other CLI-only settings (estimate, complete, log) live on the
Assignments pane instead, since they're per-assignment actions, not
standalone settings.
"""

from __future__ import annotations

import sqlite3

from rich.markup import escape as esc
from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from app.planner.availability import (
    AvailabilitySpecError,
    clear_weekly,
    list_weekly,
    parse_availability_spec,
    set_weekly,
)
from app.tui.modals import ConfirmModal, TextInputModal

_DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class SettingsPane(Vertical):
    BINDINGS = [
        ("r", "refresh_view", "Refresh"),
        ("a", "add_availability", "Add availability"),
        ("x", "clear_availability", "Clear all"),
    ]

    def __init__(self, conn: sqlite3.Connection):
        super().__init__()
        self._conn = conn

    def compose(self) -> ComposeResult:
        yield Static(id="settings-status")
        yield DataTable(id="settings-availability")

    def on_mount(self) -> None:
        self.action_refresh_view()

    def focus_default(self) -> None:
        self.query_one("#settings-availability", DataTable).focus()

    def action_refresh_view(self, message: str = "") -> None:
        table = self.query_one("#settings-availability", DataTable)
        table.clear(columns=True)
        table.add_columns("Day", "Start", "End")
        rows = list_weekly(self._conn)
        for r in rows:
            table.add_row(_DOW_NAMES[r["dow"]], r["start"], r["end"])

        status = message or (
            f"{len(rows)} availability row(s). 'a' add, 'x' clear all."
            if rows
            else 'Nothing set. Press \'a\' to add, e.g. "mon-fri 16:00-19:00".'
        )
        self.query_one("#settings-status", Static).update(status)

    @work(exclusive=True)
    async def action_add_availability(self) -> None:
        spec = await self.app.push_screen_wait(
            TextInputModal(
                "Availability spec (day[-day] HH:MM-HH:MM):",
                placeholder="mon-fri 16:00-19:00",
            )
        )
        if not spec:
            return
        try:
            parsed = parse_availability_spec(spec)
        except AvailabilitySpecError as e:
            self.action_refresh_view(f"[red]{esc(str(e))}[/]")
            return
        for dow, start, end in parsed:
            set_weekly(self._conn, dow, start, end)
        self.action_refresh_view(f"Added {len(parsed)} day(s).")

    @work(exclusive=True)
    async def action_clear_availability(self) -> None:
        confirmed = await self.app.push_screen_wait(
            ConfirmModal("Clear ALL availability and start over?")
        )
        if not confirmed:
            return
        clear_weekly(self._conn)
        self.action_refresh_view("Cleared.")

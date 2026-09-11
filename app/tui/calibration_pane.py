"""Calibration: the learned per-type pace multipliers, same data as the
`calibration` CLI command — live-computed from sessions.py, not stored.
"""

from __future__ import annotations

import sqlite3

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from app.planner.calibration import MIN_SAMPLES, multipliers_by_type


class CalibrationPane(Vertical):
    BINDINGS = [("r", "refresh_view", "Refresh")]

    def __init__(self, conn: sqlite3.Connection):
        super().__init__()
        self._conn = conn

    def compose(self) -> ComposeResult:
        yield Static("[dim]Actual ÷ default-estimate ratio per submission type.[/]")
        yield DataTable(id="calibration-table")

    def on_mount(self) -> None:
        self.action_refresh_view()

    def focus_default(self) -> None:
        self.query_one("#calibration-table", DataTable).focus()

    def action_refresh_view(self) -> None:
        table = self.query_one("#calibration-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Type", "Multiplier", "Samples", "Applied?")
        data = multipliers_by_type(self._conn)
        if not data:
            return
        for stype, (mult, n) in sorted(data.items()):
            applied = "yes" if n >= MIN_SAMPLES else f"needs {MIN_SAMPLES - n} more"
            table.add_row(stype, f"{mult:.2f}x", str(n), applied)

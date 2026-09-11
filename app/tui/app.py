"""The TUI: dashboard, plan, tutor chat, calibration in one tabbed app.
Launch with `asp tui` / `python -m app.cli tui`.
"""

from __future__ import annotations

import sqlite3

from textual import on
from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, TabbedContent, TabPane, Tabs

from app.config import Settings, get_settings
from app.db.connection import connect as db_connect
from app.tui.assignments_pane import AssignmentsPane
from app.tui.calibration_pane import CalibrationPane
from app.tui.dashboard import DashboardPane
from app.tui.plan_pane import PlanPane
from app.tui.settings_pane import SettingsPane
from app.tui.tutor_pane import TutorPane


class StudyPlannerApp(App):
    TITLE = "AI Study Planner"
    BINDINGS = [("q", "quit", "Quit")]

    def __init__(
        self,
        settings: Settings | None = None,
        conn: sqlite3.Connection | None = None,
    ):
        super().__init__()
        self._settings = settings or get_settings()
        # Accepting an existing connection (tests pass one in) means we
        # only close it here if we're the ones who opened it.
        self._conn = conn if conn is not None else db_connect(self._settings)
        self._owns_conn = conn is None

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="dashboard-tab"):
            with TabPane("Dashboard", id="dashboard-tab"):
                yield DashboardPane(self._conn)
            with TabPane("Plan", id="plan-tab"):
                yield PlanPane(self._conn)
            with TabPane("Assignments", id="assignments-tab"):
                yield AssignmentsPane(self._conn)
            with TabPane("Tutor", id="tutor-tab"):
                yield TutorPane(self._conn, self._settings)
            with TabPane("Calibration", id="calibration-tab"):
                yield CalibrationPane(self._conn)
            with TabPane("Settings", id="settings-tab"):
                yield SettingsPane(self._conn)
        yield Footer()

    def on_mount(self) -> None:
        # The tab bar itself is focusable by default, so a click lands
        # keyboard focus there — including clicking a tab that's already
        # active, which doesn't fire TabActivated at all (no real state
        # change) and so wouldn't be caught by that handler below. This
        # app never wants the tab bar itself holding focus, only
        # whatever's inside the active pane — simplest fix is to make it
        # unfocusable outright rather than chase every route into it.
        for tabs in self.query(Tabs):
            tabs.can_focus = False
        self._focus_active_pane()

    @on(TabbedContent.TabActivated)
    def _tab_switched(self) -> None:
        self._focus_active_pane()

    def _focus_active_pane(self) -> None:
        pane = self.query_one(TabbedContent).active_pane
        if pane is None:
            return
        for child in pane.children:
            focus_default = getattr(child, "focus_default", None)
            if focus_default is not None:
                focus_default()
                break

    def on_unmount(self) -> None:
        if self._owns_conn:
            self._conn.close()


def run() -> None:
    StudyPlannerApp().run()

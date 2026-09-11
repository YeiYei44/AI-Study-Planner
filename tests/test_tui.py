"""TUI tests via Textual's Pilot — real widget tree, real focus/click
simulation, fake backends/data (no real network, no real DB) so this
runs fast and deterministically. Real end-to-end verification against
the live database and a real Groq backend was done by hand; see
docs/DESIGN.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.db.connection import connect
from app.planner.availability import set_weekly
from app.tui.app import StudyPlannerApp
from app.tutor.embed import serialize_embedding
from app.tutor.qa import RetrievedChunk


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path)


@pytest.fixture
def conn(settings: Settings):
    c = connect(settings)
    yield c
    c.close()


def _seed_course_and_assignment(conn, cid=1, aid=1, name="Homework 1", due="2099-01-01T23:59:00Z", points=50):
    conn.execute(
        "INSERT INTO courses (id, name, source, raw_json, fetched_at) "
        "VALUES (?, 'Course', 's', '{}', 't') ON CONFLICT(id) DO NOTHING",
        (cid,),
    )
    conn.execute(
        "INSERT INTO assignments (id, course_id, name, due_at, points_possible, "
        "workflow_state, submission_types, source, raw_json, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, 'published', '[]', 's', '{}', 't')",
        (aid, cid, name, due, points),
    )
    conn.commit()


def _seed_calibration_session(conn, aid, actual=100, estimated=50, submission_type="online_upload"):
    conn.execute(
        "INSERT INTO sessions (assignment_id, actual_minutes, estimated_minutes_at_log, "
        "submission_type, logged_at) VALUES (?, ?, ?, ?, 't')",
        (aid, actual, estimated, submission_type),
    )
    conn.commit()


async def _click_tab(pilot, tab_id: str):
    await pilot.click(f"#--content-tab-{tab_id}")
    await pilot.pause()


# -- Dashboard ---------------------------------------------------------


async def test_dashboard_shows_real_counts_and_rows(conn, settings):
    _seed_course_and_assignment(conn)
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable, Static

        status = app.query_one("#status-line", Static)
        assert "1 courses" in str(status.content)
        assert "1 assignments" in str(status.content)
        table = app.query_one("#dash-assignments", DataTable)
        assert table.row_count == 1


async def test_dashboard_status_shows_never_synced_when_no_sync_run(conn, settings):
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Static

        status = app.query_one("#status-line", Static)
        assert "never synced" in str(status.content)


async def test_dashboard_course_name_with_brackets_not_corrupted(conn, settings):
    # Same risk class found in cli.py's console.print output earlier this
    # session — confirming DataTable doesn't share that vulnerability.
    conn.execute(
        "INSERT INTO courses (id, name, source, raw_json, fetched_at) "
        "VALUES (1, 'Bio [Honors]', 's', '{}', 't')"
    )
    conn.execute(
        "INSERT INTO assignments (id, course_id, name, due_at, workflow_state, "
        "submission_types, source, raw_json, fetched_at) "
        "VALUES (1, 1, 'Lab [optional]', '2099-01-01T00:00:00Z', 'published', '[]', 's', '{}', 't')"
    )
    conn.commit()
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable

        table = app.query_one("#dash-assignments", DataTable)
        row_key = list(table.rows.keys())[0]
        cols = list(table.columns.keys())
        course_cell = table.get_cell(row_key, cols[1])
        name_cell = table.get_cell(row_key, cols[2])
        assert course_cell == "Bio [Honors]"
        assert name_cell == "Lab [optional]"


# -- Plan ----------------------------------------------------------------


async def test_plan_regenerate_with_no_availability_shows_message(conn, settings):
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await _click_tab(pilot, "plan-tab")
        await pilot.press("r")
        await pilot.pause()
        from textual.widgets import Static

        status = str(app.query_one("#plan-status", Static).content)
        assert "no availability" in status.lower() or "No availability" in status


async def test_plan_regenerate_with_availability_populates_blocks(conn, settings):
    _seed_course_and_assignment(conn, due="2099-01-03T23:59:00Z", points=90)
    for dow in range(5):
        set_weekly(conn, dow, "16:00", "19:00")
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await _click_tab(pilot, "plan-tab")
        await pilot.press("r")
        await pilot.pause()
        from textual.widgets import DataTable, Static

        status = str(app.query_one("#plan-status", Static).content)
        assert "Regenerated" in status
        table = app.query_one("#plan-table", DataTable)
        assert table.row_count > 0


# -- Calibration -----------------------------------------------------------


async def test_calibration_shows_multipliers_from_real_sessions(conn, settings):
    _seed_course_and_assignment(conn)
    for _ in range(3):
        _seed_calibration_session(conn, aid=1, actual=100, estimated=50)
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await _click_tab(pilot, "calibration-tab")
        from textual.widgets import DataTable

        table = app.query_one("#calibration-table", DataTable)
        assert table.row_count == 1
        row_key = list(table.rows.keys())[0]
        cols = list(table.columns.keys())
        assert table.get_cell(row_key, cols[0]) == "online_upload"
        assert table.get_cell(row_key, cols[1]) == "2.00x"
        assert table.get_cell(row_key, cols[3]) == "yes"


async def test_calibration_empty_when_no_sessions_logged(conn, settings):
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await _click_tab(pilot, "calibration-tab")
        from textual.widgets import DataTable

        table = app.query_one("#calibration-table", DataTable)
        assert table.row_count == 0


# -- Tutor -----------------------------------------------------------------


async def test_tutor_shows_unavailable_message_without_backend_config(conn):
    settings = Settings(llm_backend="claude", anthropic_api_key=None)
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await _click_tab(pilot, "tutor-tab")
        from textual.widgets import Input, RichLog

        log = app.query_one("#tutor-log", RichLog)
        text = "\n".join("".join(seg.text for seg in line._segments) for line in log.lines)
        assert "unavailable" in text.lower()
        assert app.query_one("#tutor-input", Input).disabled


class _FakeTutorBackend:
    default_max_concurrency = 4

    async def answer(self, prompt: str) -> str:
        return "The answer is 42."


async def test_tutor_full_round_trip_with_fake_backend(conn, settings, monkeypatch):
    monkeypatch.setattr("app.tui.tutor_pane.make_backend", lambda *a, **k: _FakeTutorBackend())
    monkeypatch.setattr(
        "app.tui.tutor_pane.tutor_ask",
        lambda conn, backend, question, **k: _fake_ask_result(),
    )
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await _click_tab(pilot, "tutor-tab")
        from textual.widgets import Input, RichLog

        inp = app.query_one("#tutor-input", Input)
        assert not inp.disabled  # backend was available (faked)
        inp.focus()
        inp.value = "What is X?"
        await pilot.press("enter")

        for _ in range(20):
            await pilot.pause()
            if not inp.disabled:
                break

        log = app.query_one("#tutor-log", RichLog)
        text = "\n".join("".join(seg.text for seg in line._segments) for line in log.lines)
        assert "What is X?" in text
        assert "42" in text
        assert "Course [Notes]" in text  # bracketed citation label, unescaped and intact
        assert not inp.disabled  # re-enabled after answering


async def _fake_ask_result():
    from app.tutor.qa import AnswerResult

    return AnswerResult(
        answer="The answer is 42.",
        sources=[
            RetrievedChunk(
                chunk_id=1,
                material_title="Course [Notes]",
                page_ref="page 1",
                text="...",
                score=0.9,
            )
        ],
    )


# -- Focus routing (regression tests for two real bugs found by hand) ------


async def test_focus_lands_on_content_not_tab_bar_on_switch(conn, settings):
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await _click_tab(pilot, "plan-tab")
        assert app.focused is not None
        assert app.focused.id == "plan-table"


async def test_focus_survives_reclicking_the_already_active_tab(conn, settings):
    # The tab bar doesn't fire TabActivated for a re-click on the tab
    # that's already active (no real state change) — the fix has to hold
    # regardless, not just for genuine switches.
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await _click_tab(pilot, "dashboard-tab")  # already active on startup
        assert app.focused is not None
        assert app.focused.id == "dash-assignments"


async def test_tutor_input_typeable_immediately_after_switching_tabs(conn, settings):
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await _click_tab(pilot, "tutor-tab")
        from textual.widgets import Input

        await pilot.press("h", "i")
        assert app.query_one("#tutor-input", Input).value == "hi"


# -- Assignments -------------------------------------------------------------


async def test_assignments_table_populates_from_real_data(conn, settings):
    _seed_course_and_assignment(conn, points=90)
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await _click_tab(pilot, "assignments-tab")
        from textual.widgets import DataTable

        table = app.query_one("#assignments-table", DataTable)
        assert table.row_count == 1
        row_key = list(table.rows.keys())[0]
        cols = list(table.columns.keys())
        assert table.get_cell(row_key, cols[1]) == "Course"
        assert table.get_cell(row_key, cols[2]) == "Homework 1"
        assert table.get_cell(row_key, cols[3]) == "90.0"
        assert table.get_cell(row_key, cols[5]) == ""  # not done


async def test_assignments_toggle_complete_prompts_for_time_and_logs_it(conn, settings):
    # Git-commit-style: marking something done prompts for minutes spent,
    # and a real number actually gets logged as a session.
    _seed_course_and_assignment(conn)
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await _click_tab(pilot, "assignments-tab")
        from textual.widgets import DataTable, Input, Static

        await pilot.press("c")
        await pilot.pause()
        modal_input = app.screen.query_one("#modal-input", Input)
        modal_input.value = "45"
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause()
            if len(app.screen_stack) == 1:
                break

        table = app.query_one("#assignments-table", DataTable)
        assert table.row_count == 2  # "COMPLETED" header + the assignment
        row_key = list(table.rows.keys())[1]
        cols = list(table.columns.keys())
        assert table.get_cell(row_key, cols[5]) == "✓"
        status = str(app.query_one("#assignments-status", Static).content)
        assert "done" in status.lower()
        assert "logged 45 min" in status.lower()

        from app.db.completion import is_completed

        assert is_completed(conn, 1)
        assert (
            conn.execute("SELECT COUNT(*) c FROM sessions WHERE assignment_id = 1").fetchone()["c"] == 1
        )

        # toggling again (undo) doesn't prompt at all
        table.move_cursor(row=1)
        await pilot.press("c")
        await pilot.pause()
        assert len(app.screen_stack) == 1  # no modal appeared
        row_key = list(table.rows.keys())[0]
        cols = list(table.columns.keys())
        assert table.get_cell(row_key, cols[5]) == ""
        assert not is_completed(conn, 1)


async def test_assignments_toggle_complete_blank_time_skips_logging(conn, settings):
    _seed_course_and_assignment(conn)
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await _click_tab(pilot, "assignments-tab")
        await pilot.press("c")
        await pilot.pause()
        await pilot.press("enter")  # submit blank — mark done, skip logging
        for _ in range(20):
            await pilot.pause()
            if len(app.screen_stack) == 1:
                break

        from app.db.completion import is_completed

        assert is_completed(conn, 1)
        assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 0


async def test_assignments_toggle_complete_escape_cancels_entirely(conn, settings):
    _seed_course_and_assignment(conn)
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await _click_tab(pilot, "assignments-tab")
        await pilot.press("c")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        from app.db.completion import is_completed

        assert not is_completed(conn, 1)
        assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 0


async def test_assignments_missing_group_sorts_to_top(conn, settings):
    _seed_course_and_assignment(conn, aid=1, name="Overdue", due="2020-01-01T00:00:00Z")
    _seed_course_and_assignment(conn, aid=2, name="Future", due="2099-01-01T00:00:00Z")
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await _click_tab(pilot, "assignments-tab")
        from textual.widgets import DataTable

        table = app.query_one("#assignments-table", DataTable)
        cols = list(table.columns.keys())
        row_keys = list(table.rows.keys())
        # header row, then the overdue assignment, then the future one
        assert table.get_cell(row_keys[0], cols[2]).plain == "⚠ MISSING"
        assert table.get_cell(row_keys[1], cols[2]) == "Overdue"
        assert table.get_cell(row_keys[2], cols[2]) == "Future"


async def test_assignments_completed_group_sorts_to_bottom(conn, settings):
    _seed_course_and_assignment(conn, aid=1, name="Done", due="2099-01-01T00:00:00Z")
    _seed_course_and_assignment(conn, aid=2, name="NotDone", due="2099-02-01T00:00:00Z")
    from app.db.completion import set_completed

    set_completed(conn, 1)
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await _click_tab(pilot, "assignments-tab")
        from textual.widgets import DataTable

        table = app.query_one("#assignments-table", DataTable)
        cols = list(table.columns.keys())
        row_keys = list(table.rows.keys())
        assert table.get_cell(row_keys[0], cols[2]) == "NotDone"
        assert table.get_cell(row_keys[1], cols[2]).plain == "✓ COMPLETED"
        assert table.get_cell(row_keys[2], cols[2]) == "Done"


async def test_assignments_header_row_action_is_a_no_op(conn, settings):
    _seed_course_and_assignment(conn, aid=1, name="Overdue", due="2020-01-01T00:00:00Z")
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await _click_tab(pilot, "assignments-tab")
        from textual.widgets import DataTable

        table = app.query_one("#assignments-table", DataTable)
        table.move_cursor(row=0)  # the "MISSING" header row
        await pilot.press("c")
        await pilot.pause()
        assert len(app.screen_stack) == 1  # no modal — nothing selected

        from app.db.completion import is_completed

        assert not is_completed(conn, 1)


async def test_assignments_set_estimate_via_modal(conn, settings):
    _seed_course_and_assignment(conn)
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await _click_tab(pilot, "assignments-tab")
        await pilot.press("e")
        await pilot.pause()
        from textual.widgets import DataTable, Input

        modal_input = app.screen.query_one("#modal-input", Input)
        modal_input.value = "45"
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause()
            if len(app.screen_stack) == 1:  # modal popped
                break

        table = app.query_one("#assignments-table", DataTable)
        row_key = list(table.rows.keys())[0]
        cols = list(table.columns.keys())
        assert table.get_cell(row_key, cols[4]) == "45m (you)"

        row = conn.execute("SELECT minutes, basis FROM estimates WHERE assignment_id = 1").fetchone()
        assert row["minutes"] == 45
        assert row["basis"] == "user"


async def test_assignments_log_time_via_modal(conn, settings):
    _seed_course_and_assignment(conn)
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await _click_tab(pilot, "assignments-tab")
        await pilot.press("l")
        await pilot.pause()
        from textual.widgets import Input, Static

        modal_input = app.screen.query_one("#modal-input", Input)
        modal_input.value = "30"
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause()
            status = str(app.query_one("#assignments-status", Static).content)
            if "Logged" in status:
                break

        assert conn.execute("SELECT COUNT(*) c FROM sessions WHERE assignment_id = 1").fetchone()["c"] == 1


async def test_assignments_estimate_modal_cancel_leaves_estimate_unset(conn, settings):
    _seed_course_and_assignment(conn)
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await _click_tab(pilot, "assignments-tab")
        await pilot.press("e")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert conn.execute("SELECT * FROM estimates WHERE assignment_id = 1").fetchone() is None


async def test_assignments_name_with_brackets_not_corrupted(conn, settings):
    _seed_course_and_assignment(conn, name="Lab [optional]")
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await _click_tab(pilot, "assignments-tab")
        from textual.widgets import DataTable, Static

        table = app.query_one("#assignments-table", DataTable)
        row_key = list(table.rows.keys())[0]
        cols = list(table.columns.keys())
        assert table.get_cell(row_key, cols[2]) == "Lab [optional]"

        await pilot.press("c")
        await pilot.pause()
        await pilot.press("enter")  # submit the time-spent prompt blank
        for _ in range(20):
            await pilot.pause()
            if len(app.screen_stack) == 1:
                break
        from rich.text import Text

        raw_status = str(app.query_one("#assignments-status", Static).content)
        rendered = Text.from_markup(raw_status).plain  # what actually reaches the screen
        assert "Lab [optional]" in rendered


# -- Settings ------------------------------------------------------------


async def test_settings_shows_empty_state_with_no_availability(conn, settings):
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await _click_tab(pilot, "settings-tab")
        from textual.widgets import Static

        status = str(app.query_one("#settings-status", Static).content)
        assert "nothing set" in status.lower()


async def test_settings_add_availability_via_modal(conn, settings):
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await _click_tab(pilot, "settings-tab")
        await pilot.press("a")
        await pilot.pause()
        from textual.widgets import DataTable, Input

        modal_input = app.screen.query_one("#modal-input", Input)
        modal_input.value = "mon-fri 16:00-19:00"
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause()
            if len(app.screen_stack) == 1:  # modal popped
                break

        table = app.query_one("#settings-availability", DataTable)
        assert table.row_count == 5
        from app.planner.availability import list_weekly

        assert len(list_weekly(conn)) == 5


async def test_settings_add_availability_invalid_spec_shows_error(conn, settings):
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await _click_tab(pilot, "settings-tab")
        await pilot.press("a")
        await pilot.pause()
        from textual.widgets import Input, Static

        modal_input = app.screen.query_one("#modal-input", Input)
        modal_input.value = "not a spec"
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause()
            status = str(app.query_one("#settings-status", Static).content)
            if "expected" in status.lower():
                break

        from app.planner.availability import list_weekly

        assert list_weekly(conn) == []


async def test_settings_clear_availability_requires_confirmation(conn, settings):
    set_weekly(conn, 0, "16:00", "19:00")
    app = StudyPlannerApp(settings=settings, conn=conn)
    async with app.run_test() as pilot:
        await _click_tab(pilot, "settings-tab")
        await pilot.press("x")
        await pilot.pause()
        await pilot.press("n")  # decline
        await pilot.pause()
        from app.planner.availability import list_weekly

        assert len(list_weekly(conn)) == 1  # untouched

        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")  # confirm
        for _ in range(20):
            await pilot.pause()
            if not list_weekly(conn):
                break
        assert list_weekly(conn) == []

"""Tutor: a persistent chat, instead of one-shot `ask` calls — the
retrieval/grounding rules are unchanged (see app/tutor/qa.py), this is
just a nicer place to hold a conversation.

RichLog defaults to markup=False, so dynamic content (the question,
the model's answer, citation labels — all potentially containing
literal brackets from real course/material names, the same risk class
found and fixed in cli.py's console.print output) is safe as plain
text without needing to escape anything. Text.from_markup() is used
only for the styling we deliberately want (labels like "You:").
"""

from __future__ import annotations

import sqlite3

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, RichLog

from app.config import Settings
from app.planner.llm_estimate import BackendConfigError, make_backend
from app.tutor.qa import ask as tutor_ask


class TutorPane(Vertical):
    def __init__(self, conn: sqlite3.Connection, settings: Settings):
        super().__init__()
        self._conn = conn
        self._settings = settings
        self._backend = None
        self._backend_error: str | None = None

    def compose(self) -> ComposeResult:
        yield RichLog(id="tutor-log", wrap=True)
        yield Input(placeholder="Ask a question about your uploaded materials...", id="tutor-input")

    def focus_default(self) -> None:
        inp = self.query_one("#tutor-input", Input)
        if not inp.disabled:
            inp.focus()

    def on_mount(self) -> None:
        log = self.query_one("#tutor-log", RichLog)
        try:
            self._backend = make_backend(self._settings, tutor=True)
        except BackendConfigError as e:
            self._backend_error = str(e)
            log.write(Text.from_markup("[red]Tutor unavailable:[/] ") + Text(str(e)))
            self.query_one("#tutor-input", Input).disabled = True
            return
        log.write(
            Text.from_markup(
                "[dim]Ask below. Answers are grounded only in what you've uploaded "
                "with `material add` — it'll say so if your materials don't cover "
                "something, rather than guess.[/]"
            )
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        question = event.value.strip()
        if not question or self._backend is None:
            return
        event.input.value = ""
        event.input.disabled = True
        self._answer(question)

    @work(exclusive=True)
    async def _answer(self, question: str) -> None:
        log = self.query_one("#tutor-log", RichLog)
        log.write(Text.from_markup("\n[b cyan]You:[/] ") + Text(question))
        log.write(Text.from_markup("[dim]thinking...[/]"))
        try:
            result = await tutor_ask(self._conn, self._backend, question)
        except Exception as e:
            log.write(Text.from_markup("[red]Error:[/] ") + Text(str(e)))
        else:
            log.write(Text.from_markup("[b green]Tutor:[/] ") + Text(result.answer))
            if result.sources:
                log.write(Text.from_markup("[dim]Sources:[/]"))
                for s in result.sources:
                    label = f"{s.material_title}, {s.page_ref}" if s.page_ref else s.material_title
                    log.write(Text.from_markup(f"  [dim]({s.score:.2f})[/] ") + Text(label))
        finally:
            inp = self.query_one("#tutor-input", Input)
            inp.disabled = False
            inp.focus()

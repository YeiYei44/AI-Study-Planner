"""AI Study Planner CLI.

Run as `python -m app.cli <command>` (no installed script/executable
needed — just the already-trusted python.exe running a module):

Canvas ingestion:
    python -m app.cli login             # sign in (visible browser), stores the session
    python -m app.cli login-cdp F       # sign in via a Chromium you launch yourself
    python -m app.cli export-cookies F  # write the session to a small JSON file
    python -m app.cli import-cookies F  # headless boxes: load a session from that file
    python -m app.cli whoami            # is the stored session still valid?
    python -m app.cli sync              # pull, store in SQLite, report what changed
    python -m app.cli assignments       # list what's stored, soonest due first

Planning:
    python -m app.cli availability --add "mon-fri 16:00-19:00"
    python -m app.cli estimate <assignment_id> <minutes>  # override the default guess
    python -m app.cli estimate-llm       # estimate the rest via the Claude API
    python -m app.cli plan               # generate/regenerate the study plan
    python -m app.cli log <assignment_id> <minutes>  # after studying: what it actually took
    python -m app.cli calibration        # see the learned pace multipliers

Tutor:
    python -m app.cli material add <file> [--course ID] [--kind slides]
    python -m app.cli material list
    python -m app.cli ask "<question>"   # cited Q&A over your uploaded materials

`login` needs a real display. On a box without one: run `login` (and
`export-cookies`) on a machine that has one, move the resulting file over,
and `import-cookies` it here. If Playwright can't keep its own browser
process alive on that machine either (security software terminating it),
use `login-cdp` instead — it connects to a Chromium you start by hand.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from playwright.async_api import Error as PlaywrightError
from rich.console import Console
from rich.markup import escape as esc
from rich.table import Table

from app.config import get_settings
from app.db.connection import connect as db_connect
from app.db.completion import UnknownAssignmentError as UnknownAssignmentForCompletion
from app.db.completion import set_completed
from app.db.sessions import UnknownAssignmentError, log_session
from app.db.sync import record_sync_run, sync_courses_and_assignments
from app.ingest.canvas import CanvasClient
from app.ingest.cookies import filter_domain, load_cookies
from app.ingest.session import (
    BrowserClosedError,
    BrowserLaunchError,
    CanvasSession,
    LoginTimeoutError,
    NoDisplayError,
    SessionExpiredError,
    install_cookies,
    interactive_login,
    login_via_cdp,
)
from app.planner.availability import (
    AvailabilitySpecError,
    clear_weekly,
    list_weekly,
    parse_availability_spec,
    set_weekly,
)
from app.planner.calibration import MIN_SAMPLES, multipliers_by_type
from app.planner.estimate import set_estimate
from app.planner.llm_estimate import BackendConfigError, make_backend, run_llm_estimates
from app.planner.schedule import generate_plan, write_plan
from app.tutor.extract import UnsupportedFileType
from app.tutor.materials import add_material
from app.tutor.qa import DEFAULT_TOP_K
from app.tutor.qa import ask as tutor_ask

app = typer.Typer(add_completion=False, help="AI Study Planner - ingestion CLI")
console = Console()
material_app = typer.Typer(add_completion=False, help="Manage uploaded course material.")
app.add_typer(material_app, name="material")


@app.command()
def login() -> None:
    """Open a browser and sign in to Canvas (stores the session)."""
    settings = get_settings()
    console.print(
        f"Opening a browser at [cyan]{settings.canvas_base_url}[/]. "
        "Sign in with your Fulton account; this closes itself once you're in."
    )
    try:
        user = asyncio.run(interactive_login(settings))
    except (LoginTimeoutError, NoDisplayError, BrowserClosedError) as e:
        console.print(f"[yellow]{esc(str(e))}[/]")
        raise typer.Exit(1)
    console.print(
        f"[green]Signed in as {esc(str(user.get('name')))} (id {user.get('id')}).[/]"
    )


@app.command("export-cookies")
def export_cookies(
    path: Path = typer.Argument(
        Path("canvas-cookies.json"), help="Where to write the exported cookies"
    ),
) -> None:
    """Export the current session as a small JSON cookie file.

    Run this after a successful `login` on a machine where you can log in
    directly (e.g. locally, outside the Codespace). Move the resulting
    file into the Codespace and run `import-cookies <file>` there — a few
    KB, so pasting its contents works fine too.
    """
    try:
        cookies = asyncio.run(_export_cookies())
    except SessionExpiredError as e:
        console.print(f"[yellow]{esc(str(e))}[/]")
        raise typer.Exit(1)
    path.write_text(json.dumps(cookies, indent=2))
    console.print(f"[green]Wrote {len(cookies)} cookies to {path}.[/]")
    console.print(
        f"[dim]Move it into the Codespace and run "
        f"`python -m app.cli import-cookies {path.name}` there.[/]"
    )


async def _export_cookies() -> list[dict]:
    async with CanvasSession() as s:
        cookies = await s.context.cookies()
    return [c for c in cookies if "instructure.com" in c.get("domain", "")]


@app.command("login-cdp")
def login_cdp(
    out: Path = typer.Argument(
        Path("canvas-cookies.json"),
        help="Where to write the exported cookies once signed in",
    ),
    cdp_url: str = typer.Option(
        "http://localhost:9222", "--cdp-url", help="Chromium's debugging endpoint"
    ),
    auto_launch: bool = typer.Option(
        True,
        "--auto-launch/--no-auto-launch",
        help="Launch Chromium ourselves if nothing's already listening at --cdp-url",
    ),
) -> None:
    """Sign in via Chromium connected to over its debugging port, for
    machines where Playwright can't keep its own browser process alive
    (security software on a managed device killing a freshly spawned
    automated browser within seconds).

    One command: launches Playwright's bundled Chromium as a plain OS
    process (not through Playwright's own launcher — that's specifically
    what gets killed), opens the Canvas login, waits for you to sign in,
    and writes the resulting session to `out`. Leaves the browser window
    open afterward; closing this command doesn't close it.

    If that still gets killed, fall back to starting Chromium by hand
    first (`--no-auto-launch`) — on Windows PowerShell:

    \b
        $chromium = (Get-ChildItem "$env:LOCALAPPDATA\\ms-playwright\\chromium-*\\chrome-win64\\chrome.exe" | Select-Object -First 1).FullName
        & $chromium --remote-debugging-port=9222 --no-first-run --no-default-browser-check about:blank
    """
    settings = get_settings()
    if auto_launch:
        console.print(
            f"Launching Chromium (or connecting to one already at "
            f"[cyan]{cdp_url}[/])..."
        )
    else:
        console.print(f"Connecting to Chromium at [cyan]{cdp_url}[/]...")
    try:
        user, cookies = asyncio.run(
            login_via_cdp(cdp_url, settings, auto_launch=auto_launch)
        )
    except (LoginTimeoutError, BrowserClosedError, BrowserLaunchError) as e:
        console.print(f"[yellow]{esc(str(e))}[/]")
        raise typer.Exit(1)
    except PlaywrightError as e:
        console.print(
            f"[red]Couldn't connect to {esc(cdp_url)}"
            + (
                ""
                if auto_launch
                else " — is Chromium running with --remote-debugging-port open?"
            )
            + f" ({esc(str(e))})[/]"
        )
        raise typer.Exit(1)

    out.write_text(json.dumps(cookies, indent=2))
    console.print(
        f"[green]Signed in as {esc(str(user.get('name')))} (id {user.get('id')}). "
        f"Wrote {len(cookies)} cookies to {out}.[/]"
    )
    console.print(
        f"[dim]Move it into the Codespace and run "
        f"`python -m app.cli import-cookies {out.name}` there.[/]"
    )


@app.command("import-cookies")
def import_cookies(
    path: Optional[Path] = typer.Argument(
        None,
        help="File with cookie material. Omit to paste on stdin.",
    ),
) -> None:
    """Load a Canvas session from cookies, for when `login` can't open a
    visible browser.

    Accepts (auto-detected): a JSON array from a cookie-export extension, a
    `curl` command copied from DevTools, or a raw `Cookie:` request header.
    On a locked-down device, DevTools -> Network -> the top document
    request -> copy the `Cookie:` header value is usually reachable.

        python -m app.cli import-cookies cookies.json
        pbpaste | python -m app.cli import-cookies          # or just run it and paste
    """
    settings = get_settings()
    if path is not None:
        try:
            text = path.read_text()
        except FileNotFoundError:
            console.print(
                f"[red]No file at {path} in this Codespace.[/] If it's on "
                "another machine, it needs to be transferred first — drag "
                "it into VS Code's Explorer panel, use its "
                "Upload... option, or paste the file's contents by "
                "running `import-cookies` with no path."
            )
            raise typer.Exit(1)
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        console.print(
            "[dim]Paste a Cookie header, a curl command, or JSON; "
            "then press Ctrl-D:[/]"
        )
        text = sys.stdin.read()

    cookies = filter_domain(
        load_cookies(text, settings.canvas_base_url), "instructure.com"
    )
    if not cookies:
        console.print(
            "[red]No instructure.com cookies found in that input.[/]"
        )
        raise typer.Exit(1)
    try:
        user = asyncio.run(install_cookies(cookies, settings))
    except SessionExpiredError as e:
        console.print(f"[yellow]{esc(str(e))}[/]")
        raise typer.Exit(1)
    console.print(
        f"[green]Imported {len(cookies)} cookies. Signed in as "
        f"{esc(str(user.get('name')))} (id {user.get('id')}).[/]"
    )


@app.command()
def whoami() -> None:
    """Check whether the stored session is still valid."""

    async def _run() -> dict:
        async with CanvasSession() as s:
            return s.user or {}

    try:
        user = asyncio.run(_run())
    except SessionExpiredError as e:
        console.print(f"[yellow]{esc(str(e))}[/]")
        raise typer.Exit(1)
    console.print(f"[green]{esc(str(user.get('name')))} (id {user.get('id')})[/]")


@app.command()
def sync(
    save_raw: bool = typer.Option(
        True, help="Write raw JSON snapshots under data/raw/<timestamp>/"
    ),
) -> None:
    """Pull courses and assignments; print a summary."""
    try:
        asyncio.run(_sync(save_raw))
    except SessionExpiredError as e:
        console.print(f"[yellow]{esc(str(e))}[/]")
        raise typer.Exit(1)


async def _sync(save_raw: bool) -> None:
    settings = get_settings()
    started_at = datetime.now(timezone.utc).isoformat()

    async with CanvasSession() as s:
        client = CanvasClient(s.context.request, settings)
        console.print("Fetching courses...")
        courses = await client.courses()
        ids = [c["id"] for c in courses]
        console.print(f"Fetching assignments for {len(ids)} courses...")
        by_course = await client.all_assignments(ids)
        user = s.user or {}

    fetched_at_dt = datetime.now(timezone.utc)
    fetched_at = fetched_at_dt.isoformat()

    if save_raw:
        stamp = fetched_at_dt.strftime("%Y%m%dT%H%M%SZ")
        outdir = settings.raw_dir / stamp
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "user.json").write_text(json.dumps(user, indent=2))
        (outdir / "courses.json").write_text(json.dumps(courses, indent=2))
        (outdir / "assignments.json").write_text(
            json.dumps({str(k): v for k, v in by_course.items()}, indent=2)
        )
        console.print(f"Raw snapshot: [dim]{outdir}[/]")

    total_assignments = sum(len(v) for v in by_course.values())
    conn = db_connect(settings)
    try:
        changes = sync_courses_and_assignments(conn, courses, by_course, fetched_at)
        record_sync_run(
            conn,
            started_at,
            datetime.now(timezone.utc).isoformat(),
            True,
            len(courses),
            total_assignments,
            changes,
        )
    finally:
        conn.close()

    _print_summary(courses, by_course)
    _print_changes(changes)
    console.print(f"[dim]Stored in {settings.db_path}[/]")


def _print_changes(changes: list) -> None:
    if not changes:
        console.print("[dim]No changes since last sync.[/]")
        return
    console.print(f"[cyan]{len(changes)} change(s) since last sync:[/]")
    for c in changes[:20]:
        console.print(f"  {esc(str(c))}")
    if len(changes) > 20:
        console.print(f"  [dim]... and {len(changes) - 20} more[/]")


def _print_summary(
    courses: list[dict], by_course: dict[int, list[dict]]
) -> None:
    table = Table(title="Canvas sync summary")
    table.add_column("Course")
    table.add_column("Code", style="dim")
    table.add_column("Assign.", justify="right")
    table.add_column("Dated", justify="right")
    table.add_column("Next due", style="cyan")

    now = datetime.now(timezone.utc)
    for c in sorted(courses, key=lambda x: (x.get("name") or "").lower()):
        items = by_course.get(c["id"], [])
        dated = [a for a in items if a.get("due_at")]
        upcoming = sorted(
            (a for a in dated if (_parse(a["due_at"]) or now) >= now),
            key=lambda a: a["due_at"],
        )
        nxt = upcoming[0]["due_at"][:10] if upcoming else "-"
        table.add_row(
            esc(c.get("name") or "?"),
            esc(c.get("course_code") or ""),
            str(len(items)),
            str(len(dated)),
            nxt,
        )

    console.print(table)
    total = sum(len(v) for v in by_course.values())
    console.print(f"[green]{len(courses)} courses, {total} assignments.[/]")


def _parse(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


@app.command()
def assignments(
    show_all: bool = typer.Option(
        False, "--all", help="Include past-due and undated assignments too"
    ),
) -> None:
    """List assignments stored from the last sync, soonest due first."""
    settings = get_settings()
    conn = db_connect(settings)
    try:
        if show_all:
            rows = conn.execute(
                "SELECT a.*, c.name AS course_name FROM assignments a "
                "JOIN courses c ON c.id = a.course_id "
                "ORDER BY (a.due_at IS NULL), a.due_at"
            ).fetchall()
        else:
            now = datetime.now(timezone.utc).isoformat()
            rows = conn.execute(
                "SELECT a.*, c.name AS course_name FROM assignments a "
                "JOIN courses c ON c.id = a.course_id "
                "WHERE a.due_at IS NOT NULL AND a.due_at >= ? "
                "ORDER BY a.due_at",
                (now,),
            ).fetchall()
    finally:
        conn.close()

    if not rows:
        console.print(
            "[dim]Nothing stored yet — run `sync` first"
            + ("" if show_all else ", or pass --all if everything's past due")
            + ".[/]"
        )
        return

    table = Table(title="Assignments" + (" (all)" if show_all else " (upcoming)"))
    table.add_column("ID", style="dim")
    table.add_column("Due")
    table.add_column("Course")
    table.add_column("Assignment")
    table.add_column("Pts", justify="right")
    for r in rows:
        due = r["due_at"][:16].replace("T", " ") if r["due_at"] else "-"
        pts = "-" if r["points_possible"] is None else str(r["points_possible"])
        table.add_row(str(r["id"]), due, esc(r["course_name"]), esc(r["name"]), pts)
    console.print(table)


_DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@app.command()
def availability(
    add: Optional[str] = typer.Option(
        None, "--add", help='e.g. "mon-fri 16:00-19:00" or "sat 10:00-16:00"'
    ),
    clear: bool = typer.Option(
        False, "--clear", help="Remove all availability and start over"
    ),
) -> None:
    """Show or set your weekly study-availability template."""
    settings = get_settings()
    conn = db_connect(settings)
    try:
        if clear:
            clear_weekly(conn)
            console.print("[yellow]Cleared.[/]")
        if add:
            try:
                specs = parse_availability_spec(add)
            except AvailabilitySpecError as e:
                console.print(f"[red]{esc(str(e))}[/]")
                raise typer.Exit(1)
            for dow, start, end in specs:
                set_weekly(conn, dow, start, end)
            console.print(f"[green]Added {len(specs)} day(s).[/]")
        rows = list_weekly(conn)
    finally:
        conn.close()

    if not rows:
        console.print(
            '[dim]Nothing set. Example:[/] --add "mon-fri 16:00-19:00"'
        )
        return
    table = Table(title="Weekly availability")
    table.add_column("Day")
    table.add_column("Start")
    table.add_column("End")
    for r in rows:
        table.add_row(_DOW_NAMES[r["dow"]], r["start"], r["end"])
    console.print(table)


@app.command()
def estimate(
    assignment_id: int = typer.Argument(..., help="Assignment id — see `assignments --all`"),
    minutes: int = typer.Argument(..., help="Your estimate, in minutes"),
) -> None:
    """Set your own effort estimate for an assignment.

    Overrides the default heuristic `plan` otherwise falls back to; a
    hand-entered number here is always better than a guess.
    """
    settings = get_settings()
    conn = db_connect(settings)
    try:
        row = conn.execute(
            "SELECT name FROM assignments WHERE id = ?", (assignment_id,)
        ).fetchone()
        if not row:
            console.print(
                f"[red]No assignment with id {assignment_id}. "
                "Run `assignments --all` to find it.[/]"
            )
            raise typer.Exit(1)
        set_estimate(conn, assignment_id, minutes, basis="user")
    finally:
        conn.close()
    console.print(f"[green]{esc(row['name'])}: {minutes} min.[/]")


@app.command()
def plan(
    days: int = typer.Option(14, help="How many days ahead to plan"),
) -> None:
    """Generate a study plan: work backward from due dates into your
    weekly availability. Re-running replaces the open (unlocked,
    incomplete) plan; anything already marked locked or completed stays.
    """
    settings = get_settings()
    conn = db_connect(settings)
    try:
        if not list_weekly(conn):
            console.print(
                '[yellow]No availability set — nothing to plan into. '
                'Try: availability --add "mon-fri 16:00-19:00"[/]'
            )
            raise typer.Exit(1)
        blocks, shortfalls = generate_plan(conn, horizon_days=days)
        write_plan(conn, blocks)
    finally:
        conn.close()

    if not blocks:
        console.print("[dim]Nothing to schedule — no upcoming dated assignments.[/]")
        return

    table = Table(title=f"Study plan — next {days} days")
    table.add_column("Date")
    table.add_column("Time")
    table.add_column("Block")
    for b in blocks:
        label = "[dim]buffer[/]" if b.kind == "buffer" else esc(b.title)
        table.add_row(
            b.date.isoformat(),
            f"{b.start.strftime('%H:%M')}-{b.end.strftime('%H:%M')}",
            label,
        )
    console.print(table)

    if shortfalls:
        console.print("[red]Not enough time before the deadline for:[/]")
        for name, short_minutes in shortfalls:
            console.print(f"  {esc(name)} — short {short_minutes} min")


@app.command("estimate-llm")
def estimate_llm_cmd(
    limit: Optional[int] = typer.Option(
        None, help="Only process the first N eligible assignments (cost control while testing)"
    ),
) -> None:
    """Estimate effort for assignments that need it, via whichever
    backend ASP_LLM_BACKEND selects (claude / openai_compat / local —
    see .env.example and app/planner/llm_backends/).

    Skips anything you've set yourself (`estimate`) and anything already
    estimated since it last changed — safe to re-run after every `sync`.
    """
    settings = get_settings()
    try:
        backend = make_backend(settings)
    except BackendConfigError as e:
        console.print(f"[red]{esc(str(e))}[/]")
        raise typer.Exit(1)

    conn = db_connect(settings)
    try:
        counts = asyncio.run(
            run_llm_estimates(
                conn,
                backend,
                max_concurrency=settings.llm_estimate_max_concurrency,
                limit=limit,
            )
        )
    finally:
        conn.close()

    console.print(
        f"[green]{counts['estimated']} estimated ({settings.llm_backend}).[/] "
        f"{counts['skipped_cached']} unchanged since last estimate, "
        f"{counts['skipped_user']} yours (left alone), "
        f"{counts['errors']} errors."
    )


@app.command()
def log(
    assignment_id: int = typer.Argument(..., help="Assignment id — see `assignments --all`"),
    minutes: int = typer.Argument(..., help="Actual minutes you spent"),
    note: Optional[str] = typer.Option(None, "--note"),
) -> None:
    """Log actual time spent on an assignment.

    This is the input `calibration` learns your real pace from. Also
    marks any of that assignment's still-open scheduled blocks as
    completed, so the next `plan` doesn't re-offer time you already spent.
    """
    settings = get_settings()
    conn = db_connect(settings)
    try:
        result = log_session(conn, assignment_id, minutes, note=note)
    except UnknownAssignmentError:
        console.print(
            f"[red]No assignment with id {assignment_id}. "
            "Run `assignments --all` to find it.[/]"
        )
        raise typer.Exit(1)
    finally:
        conn.close()

    msg = f"[green]Logged {minutes} min on {esc(result.assignment_name)}.[/]"
    if result.blocks_marked_done:
        msg += f" Marked {result.blocks_marked_done} scheduled block(s) done."
    console.print(msg)


@app.command()
def complete(
    assignment_id: int = typer.Argument(..., help="Assignment id — see `assignments --all`"),
    undo: bool = typer.Option(False, "--undo", help="Mark it not-done again"),
) -> None:
    """Mark an assignment fully done (or, with --undo, not done).

    A completed assignment is left out of the next `plan` entirely,
    instead of having its remaining estimate scheduled again.
    """
    settings = get_settings()
    conn = db_connect(settings)
    try:
        result = set_completed(conn, assignment_id, completed=not undo)
    except UnknownAssignmentForCompletion:
        console.print(
            f"[red]No assignment with id {assignment_id}. "
            "Run `assignments --all` to find it.[/]"
        )
        raise typer.Exit(1)
    finally:
        conn.close()

    if result.completed:
        msg = f"[green]Marked {esc(result.assignment_name)} done.[/]"
        if result.blocks_marked_done:
            msg += f" Cleared {result.blocks_marked_done} scheduled block(s) for it."
        msg += " Run `plan` to regenerate without it."
    else:
        msg = f"[green]Marked {esc(result.assignment_name)} not done.[/]"
    console.print(msg)


@app.command()
def calibration() -> None:
    """Show learned pace multipliers per assignment type, from logged sessions."""
    settings = get_settings()
    conn = db_connect(settings)
    try:
        table_data = multipliers_by_type(conn)
    finally:
        conn.close()

    if not table_data:
        console.print(
            "[dim]No sessions logged yet — nothing to calibrate from. "
            "Use `log <assignment_id> <minutes>` after you study.[/]"
        )
        return

    t = Table(title="Calibration (actual ÷ default estimate, by submission type)")
    t.add_column("Type")
    t.add_column("Multiplier", justify="right")
    t.add_column("Samples", justify="right")
    t.add_column("Applied?")
    for stype, (mult, n) in sorted(table_data.items()):
        applied = "[green]yes[/]" if n >= MIN_SAMPLES else f"[dim]needs {MIN_SAMPLES - n} more[/]"
        t.add_row(esc(stype), f"{mult:.2f}x", str(n), applied)
    console.print(t)


@material_app.command("add")
def material_add(
    path: Path = typer.Argument(..., exists=True, dir_okay=False, help="A .pdf, .pptx, .docx, .txt, or .md file"),
    title: Optional[str] = typer.Option(None, "--title", help="Defaults to the filename"),
    course: Optional[int] = typer.Option(None, "--course", help="Course id — see `sync`'s summary table"),
    kind: str = typer.Option("file", "--kind", help="syllabus | slides | notes | file"),
) -> None:
    """Extract, chunk, and embed a material file for `ask` to search."""
    settings = get_settings()
    conn = db_connect(settings)
    try:
        try:
            result = add_material(conn, path, title=title, course_id=course, kind=kind)
        except UnsupportedFileType as e:
            console.print(f"[red]{esc(str(e))}[/]")
            raise typer.Exit(1)
    finally:
        conn.close()

    if result.chunk_count == 0:
        console.print(
            f"[yellow]Added {esc(result.title)}, but no extractable text came out of it "
            "— a scanned/image-only PDF, maybe? Nothing for `ask` to search yet.[/]"
        )
        return
    console.print(f"[green]Added {esc(result.title)}: {result.chunk_count} chunks.[/]")


@material_app.command("list")
def material_list() -> None:
    """List uploaded materials and how many chunks each produced."""
    settings = get_settings()
    conn = db_connect(settings)
    try:
        rows = conn.execute(
            "SELECT m.id, m.title, m.kind, m.added_at, c.name AS course_name, "
            "COUNT(ch.id) AS chunk_count "
            "FROM materials m "
            "LEFT JOIN courses c ON c.id = m.course_id "
            "LEFT JOIN chunks ch ON ch.material_id = m.id "
            "GROUP BY m.id ORDER BY m.added_at DESC"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        console.print("[dim]No materials uploaded yet. Try `material add <file>`.[/]")
        return
    table = Table(title="Materials")
    table.add_column("ID", style="dim")
    table.add_column("Title")
    table.add_column("Kind")
    table.add_column("Course")
    table.add_column("Chunks", justify="right")
    for r in rows:
        table.add_row(
            str(r["id"]), esc(r["title"]), r["kind"], esc(r["course_name"] or "-"), str(r["chunk_count"])
        )
    console.print(table)


@app.command()
def ask(
    question: str = typer.Argument(..., help="Your question"),
    top_k: int = typer.Option(DEFAULT_TOP_K, help="How many excerpts to retrieve"),
) -> None:
    """Ask a question, answered only from your uploaded materials, with citations."""
    settings = get_settings()
    try:
        backend = make_backend(settings, tutor=True)
    except BackendConfigError as e:
        console.print(f"[red]{esc(str(e))}[/]")
        raise typer.Exit(1)

    conn = db_connect(settings)
    try:
        result = asyncio.run(tutor_ask(conn, backend, question, top_k=top_k))
    finally:
        conn.close()

    console.print(result.answer)
    if result.sources:
        console.print("\n[dim]Sources consulted:[/]")
        for s in result.sources:
            ref = f"{s.material_title}, {s.page_ref}" if s.page_ref else s.material_title
            console.print(f"  [dim]({s.score:.2f}) {esc(ref)}[/]")


if __name__ == "__main__":
    app()

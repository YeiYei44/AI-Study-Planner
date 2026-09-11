"""Ingestion CLI. This is the step-1 test harness for the session layer
and API client — it proves we can pull every assignment across every
course reliably before anything is built on top.

    asp login              # sign in (visible browser), stores the session
    asp login-cdp F        # sign in via a Chromium you launch yourself
    asp export-cookies F   # write the session to a small JSON file
    asp import-cookies F   # headless boxes: load a session from that file
    asp whoami             # is the stored session still valid?
    asp sync               # pull courses + assignments, print a summary

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
from rich.table import Table

from app.config import get_settings
from app.ingest.canvas import CanvasClient
from app.ingest.cookies import filter_domain, load_cookies
from app.ingest.session import (
    BrowserClosedError,
    CanvasSession,
    LoginTimeoutError,
    NoDisplayError,
    SessionExpiredError,
    install_cookies,
    interactive_login,
    login_via_cdp,
)

app = typer.Typer(add_completion=False, help="AI Study Planner - ingestion CLI")
console = Console()


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
        console.print(f"[yellow]{e}[/]")
        raise typer.Exit(1)
    console.print(
        f"[green]Signed in as {user.get('name')} (id {user.get('id')}).[/]"
    )


@app.command("export-cookies")
def export_cookies(
    path: Path = typer.Argument(
        Path("canvas-cookies.json"), help="Where to write the exported cookies"
    ),
) -> None:
    """Export the current session as a small JSON cookie file.

    Run this after a successful `asp login` on a machine where you can log
    in directly (e.g. locally, outside the Codespace). Move the resulting
    file into the Codespace and run `asp import-cookies <file>` there —
    a few KB, so pasting its contents works fine too.
    """
    try:
        cookies = asyncio.run(_export_cookies())
    except SessionExpiredError as e:
        console.print(f"[yellow]{e}[/]")
        raise typer.Exit(1)
    path.write_text(json.dumps(cookies, indent=2))
    console.print(f"[green]Wrote {len(cookies)} cookies to {path}.[/]")
    console.print(
        f"[dim]Move it into the Codespace and run "
        f"`asp import-cookies {path.name}` there.[/]"
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
) -> None:
    """Sign in via a Chromium you launch yourself, for machines where
    Playwright can't keep its own browser process alive (security
    software on a managed device killing a freshly spawned automated
    browser within seconds).

    First, launch Playwright's own Chromium by hand with a debugging
    port open. On Windows PowerShell:

    \b
        $chromium = (Get-ChildItem "$env:LOCALAPPDATA\ms-playwright\chromium-*\chrome-win64\chrome.exe" | Select-Object -First 1).FullName
        & $chromium --remote-debugging-port=9222 --no-first-run --no-default-browser-check about:blank

    Leave that window open, then run this command. It connects to that
    browser, navigates it to the Canvas login, waits for you to sign in,
    and writes the resulting session to `out`. We only connect — closing
    this command doesn't close your browser window.
    """
    settings = get_settings()
    console.print(f"Connecting to Chromium at [cyan]{cdp_url}[/]...")
    try:
        user, cookies = asyncio.run(login_via_cdp(cdp_url, settings))
    except (LoginTimeoutError, BrowserClosedError) as e:
        console.print(f"[yellow]{e}[/]")
        raise typer.Exit(1)
    except PlaywrightError as e:
        console.print(
            f"[red]Couldn't connect to {cdp_url} — is Chromium running "
            f"with --remote-debugging-port open? ({e})[/]"
        )
        raise typer.Exit(1)

    out.write_text(json.dumps(cookies, indent=2))
    console.print(
        f"[green]Signed in as {user.get('name')} (id {user.get('id')}). "
        f"Wrote {len(cookies)} cookies to {out}.[/]"
    )
    console.print(
        f"[dim]Move it into the Codespace and run "
        f"`asp import-cookies {out.name}` there.[/]"
    )


@app.command("import-cookies")
def import_cookies(
    path: Optional[Path] = typer.Argument(
        None,
        help="File with cookie material. Omit to paste on stdin.",
    ),
) -> None:
    """Load a Canvas session from cookies, for when `asp login` can't open
    a visible browser.

    Accepts (auto-detected): a JSON array from a cookie-export extension, a
    `curl` command copied from DevTools, or a raw `Cookie:` request header.
    On a locked-down device, DevTools -> Network -> the top document
    request -> copy the `Cookie:` header value is usually reachable.

        asp import-cookies cookies.json
        pbpaste | asp import-cookies          # or just run it and paste
    """
    settings = get_settings()
    if path is not None:
        text = path.read_text()
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
        console.print(f"[yellow]{e}[/]")
        raise typer.Exit(1)
    console.print(
        f"[green]Imported {len(cookies)} cookies. Signed in as "
        f"{user.get('name')} (id {user.get('id')}).[/]"
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
        console.print(f"[yellow]{e}[/]")
        raise typer.Exit(1)
    console.print(f"[green]{user.get('name')} (id {user.get('id')})[/]")


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
        console.print(f"[yellow]{e}[/]")
        raise typer.Exit(1)


async def _sync(save_raw: bool) -> None:
    settings = get_settings()
    async with CanvasSession() as s:
        client = CanvasClient(s.context.request, settings)
        console.print("Fetching courses...")
        courses = await client.courses()
        ids = [c["id"] for c in courses]
        console.print(f"Fetching assignments for {len(ids)} courses...")
        by_course = await client.all_assignments(ids)
        user = s.user or {}

    if save_raw:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        outdir = settings.raw_dir / stamp
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "user.json").write_text(json.dumps(user, indent=2))
        (outdir / "courses.json").write_text(json.dumps(courses, indent=2))
        (outdir / "assignments.json").write_text(
            json.dumps({str(k): v for k, v in by_course.items()}, indent=2)
        )
        console.print(f"Raw snapshot: [dim]{outdir}[/]")

    _print_summary(courses, by_course)


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
            c.get("name") or "?",
            c.get("course_code") or "",
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


if __name__ == "__main__":
    app()

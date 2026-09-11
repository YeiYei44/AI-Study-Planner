# AI Study Planner & Tutor

Pulls your Canvas coursework and hand-uploaded material, builds a study
plan of concrete calendar blocks, and tutors you against the actual course
content — feeding what you get wrong back into the plan.

Single user, local-first. See [docs/DESIGN.md](docs/DESIGN.md) for the full
architecture and rationale.

## Status

**Step 1: Canvas session + API client.** No Canvas token is available for
this account, so ingestion runs through a logged-in browser session
(Playwright + a persistent profile). Steps 2–6 (SQLite store, scheduler,
LLM estimation, tutor, spaced review) are not built yet.

## Setup

Requires Python 3.11+.

```bash
python -m pip install -e ".[dev]"
python -m playwright install chromium
# Linux only, for the headless browser's system libraries:
sudo python -m playwright install-deps chromium
```

Optionally copy `.env.example` to `.env` (defaults already target
`https://fultonschools.instructure.com`).

## Usage

Run everything as `python -m app.cli <command>` — no installed
script/executable, just the Python interpreter running a module.
(Deliberate: an installed console-script entry point would generate an
`asp.exe`-style wrapper, and a newly-created unsigned executable is
exactly what endpoint security on a managed device tends to flag.)

```bash
python -m app.cli login     # opens a real browser — sign in with your Fulton/Microsoft account
python -m app.cli whoami    # check the stored session is still valid
python -m app.cli sync      # pull courses + assignments, print a summary, save a raw snapshot
```

`login` needs a real display, so run it on your own machine. It stores
the session under `data/browser-profile/`; `whoami` and `sync` then run
headless against that. When the session expires, `login` again.

`sync` writes raw JSON to `data/raw/<timestamp>/` — that's the input for
step 2.

### Headless box / Codespace

No display means `login` can't open a browser here. In rough order of how
much they depend on the specific device:

**1 — Run `login` locally, then transfer the session.** Clone this repo
(or copy the `app/` directory) onto any machine where you can actually
log into Canvas interactively — install deps, `playwright install
chromium`, `python -m app.cli login`, then:

```bash
python -m app.cli export-cookies canvas-cookies.json
```

writes a small JSON file (a few KB — the file can just be pasted, no need
to move a whole browser profile). Bring that file into the Codespace
(drag it into the Explorer panel, or paste its contents) and:

```bash
python -m app.cli import-cookies canvas-cookies.json
python -m app.cli sync
```

If your district's Conditional Access is scoped to a specific managed
browser rather than network/location, a sign-in from a locally-run
Chromium may still be rejected the same way a Codespace one is — that's a
thing to find out by trying, not to work around. If you hit the same
"does not meet the criteria for this resource" message here, stop and
move to option 3 below rather than changing how the browser identifies
itself.

**1a — If Playwright can't keep its own browser open** on that machine
(managed-device security software killing a freshly launched,
automation-flagged browser within seconds — this can happen headed or
headless, and isn't specific to Canvas): launch Chromium yourself instead
of letting Playwright spawn it, then connect to it.

```powershell
# PowerShell
$chromium = (Get-ChildItem "$env:LOCALAPPDATA\ms-playwright\chromium-*\chrome-win64\chrome.exe" | Select-Object -First 1).FullName
& $chromium --remote-debugging-port=9222 --no-first-run --no-default-browser-check about:blank
```

Leave that window open, then in another terminal:

```bash
python -m app.cli login-cdp canvas-cookies.json
```

It connects over the debugging port, navigates that window to the Canvas
login, waits for you to sign in, and writes the session out — same file,
same next step (`import-cookies`). It only connects; it never closes the
browser you launched.

**2 — Desktop in the Codespace.** `.devcontainer/` adds a noVNC desktop
(Command Palette → *Codespaces: Rebuild Container*, then open port 6080,
password `vscode`). `login` there behaves like a normal local login, but
it's still a sign-in from GitHub's cloud network — Conditional Access
scoped to network/location will reject it exactly like the Codespace's
headless context would.

**3 — Cookies via DevTools**, if reachable anywhere you're signed in:
Network tab → reload → the top `fultonschools.instructure.com` document
request → Headers → copy the `Cookie:` value → `import-cookies` (paste,
Ctrl-D). Also accepts a "Copy as cURL" paste or a JSON export file.
Cookies lapse periodically; re-copy when `sync` reports the session gone.

If none of these are reachable, the ICS calendar feed (no auth) plus
manually uploaded course files sidestep the whole problem — see
`docs/DESIGN.md` §2.

## Layout

```
.devcontainer/          Codespace + noVNC desktop for `login`
app/
  config.py            settings (env, ASP_ prefix)
  ingest/
    session.py         CanvasSession (headless) + interactive_login + install_cookies
    cookies.py         parse cookie header / curl / JSON export into Playwright cookies
    canvas.py          CanvasClient — pagination, rate limiting, endpoints
  cli.py               login / login-cdp / export-cookies / import-cookies / whoami / sync
docs/DESIGN.md         architecture and decisions
tests/
```

## Tests

```bash
python -m pytest
```

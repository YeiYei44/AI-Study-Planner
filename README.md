# AI Study Planner & Tutor

Pulls your Canvas coursework and hand-uploaded material, builds a study
plan of concrete calendar blocks, and tutors you against the actual course
content — feeding what you get wrong back into the plan.

Single user, local-first. See [docs/DESIGN.md](docs/DESIGN.md) for the full
architecture and rationale.

## Status

**Steps 1–5 done: Canvas session + API client, normalize + SQLite +
diffing, deterministic scheduler, LLM estimation + calibration, the
tutor.** No Canvas token is available for this account, so ingestion
runs through a logged-in browser session (Playwright + a persistent
profile). `sync` pulls courses and assignments, stores them in SQLite,
and reports what changed since last time. `plan` turns that into an
actual calendar: effort estimates (via `estimate-llm` — Groq by default,
cached per assignment and falling back to a heuristic until estimated)
scheduled backward from due dates into your weekly availability. `log`
records what things actually took, and `calibration` learns a real
per-type pace multiplier from that — applied automatically, never
overriding an `estimate` you set by hand. `material add` extracts,
chunks, and embeds your own files (syllabi, slides, notes — Canvas alone
never covered lecture content); it also does the same for every synced
assignment's own name/due date/points/description (`sync` keeps this
current automatically), so `ask` answers questions grounded in *both*
what you've uploaded and what Canvas already has, with citations, and
says so plainly when neither covers something rather than guessing. A
"what's due soon" style question is answered from an always-current,
always-included digest rather than similarity search, since ranking by
topical similarity has nothing to grab onto for a schedule question.
`complete` marks a whole assignment done, independent of whether you
ever logged time against it — `plan` then leaves it out entirely instead
of scheduling it again. Verified against a real account, real
assignments, and real files — not just unit tests. Step 6 (spaced
review) is not built yet.

**Plus a TUI** (`asp tui`) — a dashboard, the full plan, per-assignment
actions (estimate/complete/log), a persistent tutor chat, calibration,
and availability settings, all in one interactive screen instead of
separate one-shot commands. Login, cookie import/export, and material
upload stay CLI-only; they're one-shot browser/file operations, not
naturally interactive ones.

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
python -m app.cli login       # opens a real browser — sign in with your Fulton/Microsoft account
python -m app.cli whoami      # check the stored session is still valid
python -m app.cli sync        # pull, store in SQLite (data/planner.db), report what changed
python -m app.cli assignments # list what's stored, soonest due first (--all for everything)

# planning, once you've synced at least once:
python -m app.cli availability --add "mon-fri 16:00-19:00"   # weekly template; repeatable
python -m app.cli estimate <assignment_id> <minutes>          # override the default guess
python -m app.cli estimate-llm                                # estimate the rest via Claude
python -m app.cli plan                                        # generate/regenerate the plan

# after you actually study:
python -m app.cli log <assignment_id> <minutes>   # what it actually took
python -m app.cli calibration                     # see the learned pace multipliers
python -m app.cli complete <assignment_id>         # mark it fully done; --undo to reverse
```

`login` needs a real display, so run it on your own machine. It stores
the session under `data/browser-profile/`; `whoami` and `sync` then run
headless against that. When the session expires, `login` again.

`plan` schedules backward from each assignment's due date (minus a 12h
safety margin) into your availability, highest-priority first
(points ÷ days-until-due), in fixed 45-minute blocks with one buffer
block reserved per available day. Re-running it replaces the open plan;
anything you've locked or marked complete is left alone, and any
assignment you've marked done with `complete` is excluded entirely — not
just its existing blocks, the assignment itself, so it never gets
scheduled again. If it can't fit an assignment in before its deadline
given everything else, it says so rather than silently dropping it.

`estimate-llm` needs a backend configured — `ASP_LLM_BACKEND` picks
which, see `.env.example` for each one's settings:

- **`claude`** (default) — highest quality, costs money. Haiku 4.5 by
  default (`ASP_LLM_ESTIMATE_MODEL` to change it) — effort estimation
  from a short description is a simple, high-volume, low-stakes call,
  not the tier the tutor will need later.
- **`openai_compat`** — any OpenAI-compatible endpoint: **Groq**
  (recommended — free key at console.groq.com, fast, generous daily
  limits), Mistral's La Plateforme, OpenRouter, GitHub Models, or a local
  Ollama server if you'd rather run that than the option below.
- **`local`** — fully offline via `llama-cpp-python`
  (`pip install -e ".[local]"`) against a local `.gguf` file (an
  8B-class instruct model). No network at all, so it runs directly on a
  restricted device with no Codespace and no question of which domains
  are reachable.

Whichever backend, `estimate-llm` skips anything you've set yourself and
anything already estimated since it last changed, so it's cheap to
re-run after every `sync`. It retries automatically on rate limiting
(exponential backoff) and on the occasional malformed response smaller
free models sometimes produce (a quick immediate retry) — verified
end-to-end against Groq's free tier: 112/112 real assignments estimated,
0 errors, after finding and fixing exactly these issues against
production data (see `docs/DESIGN.md`).

### Tutor

```bash
python -m app.cli material add notes.pdf --title "Unit 3 Slides" --kind slides --course 12345
python -m app.cli material list
python -m app.cli ask "What's the difference between ionic and covalent bonds?"
```

`material add` accepts `.pdf`, `.pptx`, `.docx`, `.txt`, and `.md` —
extracts text per page/slide (or per ~15-paragraph section for `.docx`,
which has no stored page concept at all), chunks it (~800 chars, ~100
overlap, never across a page/slide boundary — that would point a
citation at the wrong page), and embeds every chunk locally via
`fastembed` (no torch, ~67MB model, downloaded once on first use — this
step is always local and free no matter which `ASP_LLM_BACKEND` answers
questions, since Groq itself has no embeddings endpoint at all). `--course`
and `--kind` are optional.

`ask` retrieves the most relevant chunks — hybrid search, not embeddings
alone: dense (cosine similarity, brute-force, plenty fast at a personal
corpus's scale) plus lexical (SQLite's built-in FTS5, zero new
dependencies), merged by Reciprocal Rank Fusion. Found necessary against
real uploaded content, not added speculatively: a small embedding model
alone ranked the chunk containing "Document 3" 16th of 19 for a question
asking specifically about document 3, because dense embeddings are weak
at exact/numbered references — the "3" gets diluted into an average
against generic words repeated in every chunk. Fixed and re-verified
against that same real material (see `docs/DESIGN.md` for the full
debugging trail). It's *reliable*, not *guaranteed* — a document with
many more short numbered items than were tested here could still see one
narrowly miss the cutoff; structure-aware chunking (splitting on detected
"Document N"-style headers) would close that gap further but isn't built.

`ask` answers using only the retrieved context, citing sources by title
and page/slide. If your materials don't cover the question, it says so
rather than answering from the model's own training knowledge — verified
directly: asked something absent from the test materials and got an
explicit "the excerpts don't include that" instead of a plausible-sounding
guess.

`log` records actual time against an assignment and marks its open
scheduled blocks done. Once a submission type (Canvas's own
categorization — quiz, upload, discussion, etc.) has 3+ logged sessions,
`calibration` starts applying that type's real actual÷estimated ratio to
future estimates of the same type — automatically, everywhere an
estimate is read, without needing to regenerate anything by hand. A
manual `estimate` override is never adjusted by this.

`sync` also writes a raw JSON snapshot to `data/raw/<timestamp>/` on each
run — the untouched API response, kept alongside the normalized SQLite
rows for debugging and as the audit trail behind the diff.

### TUI

```bash
asp tui
```

Six tabs, `Tab`/`Shift+Tab` or click to switch, `q` to quit:

- **Dashboard** — course/assignment/material counts, last sync status,
  upcoming assignments, this week's scheduled blocks. `r` to refresh.
- **Plan** — the full open plan, 30 days out. `r` regenerates it in
  place (same `generate_plan()`/`write_plan()` the `plan` CLI command
  uses) — no need to leave the TUI to re-run it after logging time or
  changing availability.
- **Assignments** — every published assignment, with its estimate and
  done/not-done status, grouped: overdue-and-not-done ("⚠ MISSING") at
  the very top, the regular list in the middle, done work ("✓ COMPLETED")
  out of the way at the bottom. Select a row: `c` toggles it done — like
  a git commit, marking something done prompts for minutes spent first
  (a number logs it as a real session in one step, same as `complete`
  then `log`; blank marks done without logging; Escape cancels the whole
  thing); undoing doesn't prompt. `e` prompts for a minutes estimate
  (same as `estimate`), `l` prompts for actual minutes spent (same as
  `log`). The prompts are a small reusable modal (`app/tui/modals.py`)
  — Textual has no built-in input dialog.
- **Tutor** — the same grounded, cited Q&A as `ask` (materials *and*
  assignment descriptions/due dates), but as a persistent conversation
  instead of one-shot calls: type a question, press Enter, the answer
  and its sources append below. Needs a configured backend
  (`ASP_LLM_BACKEND`) — says so plainly and disables the input if none
  is set, rather than crashing.
- **Calibration** — same live-computed multiplier table as the
  `calibration` command.
- **Settings** — the weekly availability template: `a` adds a spec
  (same `"mon-fri 16:00-19:00"` syntax as `availability --add`), `x`
  clears it (asks to confirm first — the one destructive action in the
  TUI).

Built on [Textual](https://textual.textualize.io/), tested against a
real database, real calibration data, and a real Groq call — not just
that it renders. One real bug worth knowing about if you extend this:
switching tabs by default leaves keyboard focus on the tab bar itself,
not the pane you switched to, silently breaking every pane's own
keybindings until you click into the content by hand; disabling the tab
bar's focusability (`Tabs.can_focus = False`) was the fix that actually
held, including the trickier case of re-clicking a tab that's already
active (which doesn't fire Textual's `TabActivated` event at all, so a
handler that only listens for that misses it).

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
headless, and isn't specific to Canvas):

```bash
python -m app.cli login-cdp canvas-cookies.json
```

This launches Chromium as a plain OS process (not through Playwright's
own launcher, which is specifically what gets killed — the driver
process, `--remote-debugging-pipe`, and the automation flag set together
make a recognizable signature; a directly-launched browser over a plain
debugging port looks the same as one a human started from a terminal),
opens the Canvas login, waits for you to sign in, and writes the session
out. Leaves the browser open afterward — closing the command doesn't
close it. Same next step either way (`import-cookies`).

If that *still* gets killed, fall back to starting Chromium by hand first
(`--no-auto-launch` connects without launching anything):

```powershell
# PowerShell
$chromium = (Get-ChildItem "$env:LOCALAPPDATA\ms-playwright\chromium-*\chrome-win64\chrome.exe" | Select-Object -First 1).FullName
& $chromium --remote-debugging-port=9222 --no-first-run --no-default-browser-check about:blank
```

```bash
python -m app.cli login-cdp canvas-cookies.json --no-auto-launch
```

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
    normalize.py       raw Canvas JSON -> canonical row dicts
  db/
    schema.py          SQLite DDL (courses, assignments, sync_runs,
                        availability, estimates, plan_blocks, sessions,
                        assignment_status, materials, chunks)
    connection.py       connect() — WAL, schema bootstrap, column migrations,
                        chunks_fts (FTS5 hybrid-search index, auto-synced)
    sync.py            diff-then-upsert; returns what changed
    sessions.py         log_session() — actual time + mark blocks done
    completion.py        set_completed()/is_completed() — mark a whole
                        assignment done, distinct from log_session()'s
                        per-block completion
  planner/
    availability.py    weekly template + "mon-fri 16:00-19:00" parsing
    estimate.py        estimate priority chain (user > llm > default) + calibration
    llm_estimate.py     backend-agnostic caching + bulk-run orchestration
    llm_backends/        claude.py, openai_compat.py, local_llamacpp.py — pluggable,
                          each implementing estimate() and answer()
    calibration.py      live per-submission-type actual÷estimate multiplier
    schedule.py         generate_plan() — backward-fill into availability,
                        excludes anything marked done via completion.py
  tutor/
    extract.py         per-format text extraction (pdf/pptx/docx/txt/md)
    chunk.py           chunk_text() — overlapping, word-boundary-safe
    embed.py           local embeddings (fastembed, no torch)
    materials.py        add_material() — extract -> chunk -> embed -> store
    assignment_sync.py   sync_assignment_materials() — same pipeline,
                        run per synced assignment instead of an uploaded file
    qa.py              retrieve() + ask() — cited Q&A + an always-included
                        upcoming-assignments digest; no answer without a source
  tui/
    app.py             StudyPlannerApp — tabs, focus routing
    dashboard.py, plan_pane.py, assignments_pane.py, tutor_pane.py,
    calibration_pane.py, settings_pane.py
    modals.py          TextInputModal / ConfirmModal — reusable prompts
    queries.py         shared read helpers, testable without Textual
  cli.py               login / login-cdp / export-cookies / import-cookies /
                        whoami / sync / assignments / availability /
                        estimate / estimate-llm / plan / log / complete /
                        calibration / material add / material list / ask / tui
docs/DESIGN.md         architecture and decisions
tests/
```

## Tests

```bash
python -m pytest
```

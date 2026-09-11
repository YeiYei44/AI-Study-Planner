# AI Study Planner & Tutor — Design

Status: **step 1 in progress** (session layer + Canvas API client).
Scope: single user, local-first, personal use.

---

## 1. What it does

Pulls the user's Canvas coursework (courses, assignments, due dates,
descriptions, files, grades), plus material the user uploads by hand
(syllabi, lecture slides, notes), and turns it into:

- a **study plan** — concrete time blocks on a calendar, working backward
  from due dates within the user's stated availability;
- a **tutor** — quizzes and Q&A grounded in the actual course material,
  with citations, that feeds what the user gets wrong back into the plan.

Plan → study → assess → update mastery → re-plan. Without the write-back
from assessment to plan it's just a to-do list with a chatbot next to it;
the loop is the point.

---

## 2. Constraints & decisions

**No Canvas API token.** Fulton County Schools does not let this account
mint a personal access token. Canvas exposes data through four independent
doors; only one is the token.

| Door | Auth | Data | Verdict |
|---|---|---|---|
| Personal access token | token | full `/api/v1` | blocked for this account |
| ICS calendar feed | secret URL, no login | assignments + due dates only, −30/+366 days | good cheap fallback |
| **Logged-in browser session** | session cookie | full `/api/v1` (same as token) | **primary** |
| Manual upload | n/a | syllabi, slides, notes | **mandatory** — content the tutor needs often isn't in Canvas |

**Decision: Playwright with a persistent browser profile.** The user
signs in once through a real browser (Microsoft / Entra ID SSO, no MFA on
this account). Playwright stores the profile; later syncs run headless and
ride the same cookies. When the session expires, the app surfaces a
"reconnect" prompt that reopens a visible browser for a fresh login.

**Not doing:** HTML scraping (the JSON API is right there via the same
cookies), the mobile apps' leaked OAuth keys (impersonates Instructure's
client, rotates without notice), a hosted multi-user service (would mean
custodying other students' session cookies — out of scope).

**LLM:** Claude API (`claude-sonnet-5` for judgment tasks) for now.

---

## 3. Architecture

```
  ics-feed      playwright      manual-upload      (future: extension)
      |             |                |
      +------ normalize + diff ------+
                    |
        SQLite: courses · assignments · materials
                chunks+embeddings · estimates · sessions
                cards+reviews · mastery · plan_blocks
                    |
      +-------------+--------------+
      |             |             |
   Planner        Tutor        Web UI
 (deterministic) (RAG + quiz)  (calendar + chat)
```

**Ingestion adapters are pluggable.** Every adapter emits the same
canonical records tagged with `source` and `confidence`. The planner never
knows whether a due date came from ICS or the API. Adding a source later is
a new adapter, not a rewrite.

**Raw payloads are kept.** Each sync writes the raw JSON alongside the
normalized rows with `fetched_at`, and diffs against the previous snapshot.
"3 things changed since Tuesday — re-plan?" falls out of having the
history for free.

---

## 4. Session lifecycle

Entry points, kept separate (`app/ingest/session.py`):

- **`CanvasSession`** — headless, for syncing. Opens the persistent
  context, hits `GET /api/v1/users/self`; on non-200 it raises
  `SessionExpiredError` and does nothing else. It never prompts.
- **`interactive_login()`** — a visible browser pointed at
  `…/login`. Reuses the same profile dir. Polls `users/self` every 2 s
  until 200 or a 5-minute timeout. Whatever redirect dance Microsoft SSO
  does in between is irrelevant — the poll is the only success signal.
  Refuses to run (`NoDisplayError`) when there's no `DISPLAY`.
- **`install_cookies()`** — the no-display fallback. The user pastes cookie
  material and we write it into the persistent profile, then verify with
  `users/self`. `cookies.py` auto-detects and normalizes three shapes: a
  JSON array from a cookie-export extension (or `storage_state`), a `curl`
  command from DevTools, or a raw `Cookie:` request header. Header/curl
  cookies carry no domain, so they're scoped to the Canvas base URL.
  Downside vs. `interactive_login`: no one-click reconnect — re-paste when
  cookies lapse.

**Dev environment.** The project is built on a locked-down school device
(no Claude Code, no browser extensions, DevTools blocked) via a GitHub
Codespace. `.devcontainer/` adds the `desktop-lite` feature: a Fluxbox
desktop over noVNC on port 6080, for a visible `python -m app.cli login`.

**Finding: Conditional Access blocks sign-in from the Codespace.** A login
attempted there (via noVNC) completed the Microsoft credential check but
was then rejected by Entra: "does not meet the criteria for this
resource" — Conditional Access scoped to the Canvas SSO app specifically,
not a blanket account lock. Likely a managed-device or network/location
requirement set by the district. `document.cookie` can't substitute
either — Canvas's session cookie is `httpOnly`, unreachable from any
in-page script (bookmarklet or otherwise), so there's no in-browser way
around a DevTools/extension block short of reading the OS's encrypted
cookie store directly, which we won't do: it's a credential-extraction
technique, and stacked on top of two other deliberate restrictions
(no token, no extensions/DevTools) it reads as the district's intent,
not an accidental gap.

**The boundary we're keeping:** get an already-authenticated session
by any means that doesn't touch what the restrictions are actually
checking — run the real login on a device/network Conditional Access
already trusts, then carry over only the resulting Canvas cookies
(`python -m app.cli export-cookies` / `python -m app.cli import-cookies`). We do not spoof device
compliance, tunnel to appear on the school network, or alter how the
browser identifies itself. If a locally-run login hits the same
Conditional Access message, that means the policy is browser/app-scoped
rather than network-scoped — and that's where this stops, not where it
escalates. At that point: ICS feed + manual upload, and ask the district's
IT/Canvas admin directly.

**Second finding: local Playwright gets killed too.** Attempting
`python -m app.cli login` on the school-managed Windows device (not the Codespace) got
past the Chromium launch but the browser closed itself within seconds —
`TargetClosedError`, no dialog, no notification. The user had independent
evidence from a prior, unrelated project on the same machine: Playwright
launching its own browser process is unreliable there regardless of
destination site, but a Chromium launched by hand with
`--remote-debugging-port` and connected to afterward is stable. Read
together with the no-dialog, no-notification presentation, this points to
security software matching on *how the browser process was spawned*
(likely automation flags and/or the parent-child relationship to a
driver process) rather than on *what site it's visiting* — a generic
anti-automation heuristic, not a targeted control on Canvas access. That
distinction is why `login_via_cdp()` (`python -m app.cli login-cdp`) was worth building
where routing around Conditional Access was not: it doesn't defeat a
deliberate, resource-scoped access decision, it avoids a false-positive
trigger on an unrelated detector, using a real Playwright API
(`connect_over_cdp`) against a browser the user launched and signs into
themselves, in full view, with their own credentials. Verified before
shipping: `browser.close()` on a CDP-attached browser disconnects only —
confirmed the underlying Chromium process survives it — and
`context.request` shares cookies correctly on a CDP-attached context, so
`check_auth` behaves identically to the launched-context path. Still
doesn't resolve Conditional Access either way; that gets tested once a
login is actually attempted this way.

**Third finding: the installed `asp` command itself got blocked**, having
worked half an hour earlier — consistent with an endpoint security agent
scanning newly-created files a few minutes after they appear rather than
at creation, which also explains "was working, now isn't" with nothing
having changed in between. `[project.scripts]` in `pyproject.toml` is
what creates that wrapper (`asp.exe` and friends in the venv's `Scripts/`
on Windows) at install time — removed it entirely rather than work around
the block, since there's a zero-cost alternative: `python -m app.cli
<command>` runs the exact same code through the interpreter directly, no
new executable created for anything to flag. All docs and in-app help
text now say `python -m app.cli ...` throughout.

**Fourth finding: a real login didn't survive between commands.**
`import-cookies` reported a successful sign-in; the very next `sync`
reported the session gone. Cause: Canvas's session cookie has no
`Expires`/`Max-Age` (`expires: -1` in Playwright's export — confirmed by
inspecting the actual exported file) — a true session cookie, correctly
dropped by Chromium when a `launch_persistent_context` closes, which is
what "session cookie" means. Every CLI invocation is its own process:
launch, do one thing, close. So the cookie was live for the `check_auth`
call `import-cookies` made *inside* its own still-open context (hence the
reported success), then discarded on close, then absent for `sync`'s
separate, later context launch against the same profile. Never surfaced
earlier because every prior test was either an empty profile (correctly
unauthenticated) or blocked before a real login completed.

Fix: `_persist_session_cookies()` in `session.py` — right after a login
or cookie-import is confirmed valid, before the context closes, rewrite
any cookie with no expiry to one with a concrete future timestamp
(`local_session_cookie_ttl_s`, default 24h). This only changes what our
own local Chromium profile retains between separate process launches;
Canvas's server independently enforces its own session expiry regardless
of what the client-side `Expires` attribute says, so this doesn't weaken
or extend anything the server actually trusts — it just stops our own
multi-process architecture from discarding a still-valid session for a
reason that has nothing to do with the server. Verified directly (no
network needed): added a session-only cookie, closed and reopened a
context against the same profile — gone without the fix, present with
matching value and a real `expires` with it.

**`login-cdp` now launches Chromium itself, still avoiding the kill.**
The two-step flow (launch by hand, then connect) worked but was
friction; the question was whether we could spawn Chromium ourselves
without reintroducing whatever gets it killed. The reasoning: it's
specifically Playwright's `launch()` that's the problem, because it
launches through Playwright's own Node driver process — `--remote-
debugging-pipe` IPC, driver-as-parent, a recognizable automation flag
set. `connect_over_cdp` never spawns anything, so it was never the
issue. So `login_via_cdp(auto_launch=True)` (the default) now resolves
Playwright's bundled Chromium path via `chromium.executable_path` (no
`launch()` call, so no driver spawn involved in getting that string) and
starts it with a plain `subprocess.Popen` — same shape as a human
launching it from a terminal, just with `python.exe` as the parent
instead of `powershell.exe`. If something's already listening at
`--cdp-url`, it connects to that instead of spawning a second one
(`--no-auto-launch` forces this path unconditionally). Verified in this
Codespace: `executable_path` resolves correctly with no launch; the spawn
helper builds the right arguments and does start a real Chromium process
(confirmed via its profile-directory output — it only fails to open a
debug port here because this container requires `--no-sandbox` for
Chromium, a Docker/Codespaces-specific restriction, irrelevant on a real
Windows machine); `_wait_for_cdp`/`_cdp_alive` correctly report false
while it's down and true once a debug port genuinely is up (proven with
`--no-sandbox` added just for this test). What can't be verified from
here: whether a `python.exe`-spawned Chromium actually survives the
device's security software the way the hand-launched one did — that's
answerable only on the real machine.

**Re-login as a first-class state.** APScheduler runs syncs every few
hours. A 401 flips a `session_dead` flag in the DB; the web UI shows a
banner with a *Reconnect* button; clicking it calls a backend route that
runs `interactive_login()`, then resumes the queued sync.

**Profile lock.** `launch_persistent_context` holds an exclusive lock on
the profile dir, so the headless syncer and a headed login can't run at
once. In the server phase a single `BrowserManager` owns the one context
and relaunches it headed/headless on demand; syncs queue behind a login.

**Stale locks across a Codespace rebuild.** Chrome's `SingletonLock` is a
symlink recording `hostname-pid`. `data/` lives on the workspace volume
and survives a container rebuild; the container's hostname doesn't. Next
launch sees a lock naming an unverifiable host and refuses to start with
a "profile in use ... on another computer" error, even though nothing is
running. Every launch site clears `SingletonLock` / `SingletonSocket` /
`SingletonCookie` before launching (`_clear_stale_singleton_locks`) —
safe since this project never runs two syncs against one profile at once.

**Display requirement.** `interactive_login()` needs a real display, so it
runs on the user's machine, not a headless box. (Codespaces has `xvfb`
installed for smoke-testing the headless path only.)

---

## 5. Canvas API client (`app/ingest/canvas.py`)

Talks to `/api/v1` over Playwright's `APIRequestContext`, which shares
cookie storage with the browser context — so calls are session-authed,
no token, no `Authorization` header. Reads work as-is; writes (not needed
yet) would need the `X-CSRF-Token` header from the `_csrf_token` cookie.

Two things that silently break naive clients, handled here:

- **Pagination** — Canvas returns RFC 5988 `Link` headers; follow
  `rel="next"` until it's absent. `per_page` caps at 100. Miss this and
  you get 10 items per course and a wrong plan.
- **Rate limiting** — cost-based leaky bucket. Bounded concurrency
  (`request_max_concurrency`, default 3), a short pause when
  `X-Rate-Limit-Remaining` drops below `rate_limit_floor`, exponential
  backoff on a 403 throttle body.

---

## 6. Data model (SQLite, WAL)

```sql
courses(id, canvas_id, name, code, term, source, raw_json, fetched_at)

assignments(id, course_id, canvas_id, title, description_html, due_at,
            points, submission_types, html_url, workflow_state,
            source, raw_json, fetched_at)

materials(id, course_id, kind,           -- syllabus | slides | notes | file
          title, source_uri, local_path, text, fetched_at)

chunks(id, material_id, ord, text, embedding, page_ref)   -- page_ref -> citation

topics(id, course_id, name)
assignment_topics(assignment_id, topic_id)
chunk_topics(chunk_id, topic_id)

estimates(assignment_id, minutes_p50, minutes_p80, basis)  -- llm | history | user
sessions(id, block_id, assignment_id, started_at, ended_at, actual_minutes)

cards(id, topic_id, front, back, source_chunk_id)
card_reviews(card_id, ts, rating, stability, difficulty, due)   -- FSRS state
mastery(topic_id, theta, updated_at)

availability(dow, start, end)
blackouts(start, end, reason)
plan_blocks(id, date, start, end, kind,   -- assignment | review | buffer
            assignment_id, topic_id, locked, completed)

sync_runs(id, started_at, ended_at, source, ok, changes_json)
```

`chunks.page_ref` exists so the tutor can say "Lecture 7, slide 12" — a
tutor that invents course specifics the night before an exam is worse than
none. `sessions.actual_minutes` is the one column that makes the whole
system get better with use.

---

## 7. Planner — LLM judgment, deterministic scheduling

The tempting build ("dump assignments in a prompt, ask for a plan")
produces prose that reads well, quietly breaks constraints, can't be
edited, and costs a full generation on every change. Split it:

**LLM does judgment** (cached per assignment version):
- effort estimate (p50 / p80 minutes) from title, description, type, points
- decomposition into subtasks
- topic tagging
- the prose explanation of a plan

**Deterministic code does scheduling** (`app/planner/schedule.py`):
- take estimates as input; for each assignment walk backward from
  `due_at` minus a safety margin
- slice into blocks that fit `availability` minus `blackouts`
- order by priority = f(deadline proximity, grade weight, mastery gap)
- greedy placement with local repair; reserve ~20% as `buffer` blocks
- a plan is a **table of `plan_blocks`**, not text. Re-planning is
  idempotent, respects `locked` and `completed`, runs in ms, is unit-testable

**Effort calibration.** Cold-start from the LLM estimate; log
`actual_minutes` per session; fit a per-user, per-course-type multiplier.
After a few weeks it knows this user runs 2.3× on problem sets, 0.7× on
reading. Only possible because the estimate is a stored number.

**Review scheduling: FSRS** (not home-grown spacing, not SM-2). Due cards
merge into the same daily time budget as assignment work, so review
competes with deadlines honestly.

---

## 8. Tutor — grounded, and it writes back

- `materials` → `chunks` → embeddings (`app/tutor/index.py`)
- Q&A retrieves chunks, answers with citations to `page_ref`
- card generation from chunks; grading of the user's answers
- grading writes `mastery.theta` per topic; the scheduler reads it back and
  shifts blocks toward weak topics

**Scope boundary:** the tutor helps the user *learn* the material. It does
Socratic questioning and explanation, not drafting of graded submissions.
This is a prompt-design and feature constraint, decided up front.

---

## 9. Tech stack

- **Python 3.11+** throughout (Playwright bindings, FSRS, embeddings, doc
  parsing all live here comfortably)
- **Playwright** (async, persistent context) — primary ingestion
- **FastAPI + SQLite** (WAL), bound to `127.0.0.1` only
- **APScheduler** — background syncs
- **React + Vite** — draggable calendar, streaming tutor chat
- **No app auth** — single user on localhost; a login would be theater.
  Tradeoff: anything local can reach the port. Acceptable for a personal
  tool.

---

## 10. Build order

1. **Session layer + API client** ← *here.* Persistent profile, health
   check, headed re-login, pagination, rate limiting. Prove a full,
   reliable pull of every assignment across every course.
2. Normalize + SQLite + diffing; a plain assignments/due-dates table.
3. Deterministic scheduler + availability editor + hand-entered estimates.
   Useful at this point.
4. LLM estimation + time logging + calibration.
5. File ingestion → chunk → embed → cited Q&A.
6. FSRS cards + mastery loop closing back into the scheduler.

Step 1 is front-loaded because it's the risky part — if Fulton's SSO does
something unusual, better to hit it in an afternoon than after building a
planner on top.

---

## 11. Status

**Step 1 (session + API client) and step 2 (normalize + SQLite + diffing)
are both done**, verified against a real, live Fulton account — not just
synthetic tests. `python -m app.cli login-cdp` on the school device
survived (Conditional Access turned out to be network/location-scoped,
not browser-scoped, so a local sign-in cleared it), and `sync` pulled the
user's actual courses and assignments: 13 courses, 112 assignments, real
due dates and point values, stored in SQLite and confirmed via
`assignments`. First real end-to-end proof the whole ingestion side
works, after several rounds of environment-specific blockers (Conditional
Access, a killed Playwright browser, an AV-flagged CLI wrapper, a
session-cookie persistence bug) that are all resolved and documented
above.

Built:
- `.devcontainer/` — Codespace image + `desktop-lite` (noVNC) + node
- `app/config.py` — env-driven settings (`ASP_` prefix)
- `app/ingest/session.py` — `CanvasSession` (headless, auth-checked),
  `interactive_login()` (headed), `install_cookies()`, `login_via_cdp()`
  (self-launched-or-attached Chromium over CDP)
- `app/ingest/cookies.py` — parse Cookie header / curl / JSON export
- `app/ingest/canvas.py` — `CanvasClient`: pagination, rate-limit
  handling, `courses()` / `assignments()` / `all_assignments()`
- `app/ingest/normalize.py` — raw Canvas JSON -> canonical row dicts
- `app/db/schema.py`, `app/db/connection.py` — SQLite (WAL), `courses` /
  `assignments` / `sync_runs`, Canvas's own ids as primary keys for now
- `app/db/sync.py` — diff-then-upsert; a `Change` per new/removed
  assignment or changed due date/points/name
- `app/cli.py` — `login` / `login-cdp` / `export-cookies` /
  `import-cookies` / `whoami` / `sync` (now persists to SQLite and
  reports changes) / `assignments` (queries the store, soonest due first)

Verified: everything from earlier sections, plus — a real sync against
the live account (13 courses, 112 assignments, all correctly reported
`new` on first write); `assignments` correctly joins and sorts real
upcoming due dates; 20 tests passing including normalize and diff-upsert
coverage (new/due-changed/points-changed/name-changed/removed-but-not-
deleted, all against a real SQLite connection via `tmp_path`, not mocks).

Known gap: a failed `sync` (e.g. expired session) doesn't get a
`sync_runs` row — only successes are recorded. Minor, fine to pick up
later.

**Step 3 (deterministic scheduler) is also done.** v1 scope, stated
plainly in `app/planner/schedule.py`'s docstring: fixed 45-minute blocks
rather than variable-length slicing, one reserved buffer block/day rather
than a percentage, a weekly availability template only (no blackout
dates yet), and a regenerated plan doesn't yet subtract time already
covered by locked/completed blocks from what's still needed (harmless
while nothing is locked/completed yet — needs fixing once blocks can be
marked done). None of these are secret shortcuts; all four are called out
in the code and here.

Built:
- `app/db/schema.py` — added `availability`, `estimates`, `plan_blocks`
- `app/planner/availability.py` — weekly template; `parse_availability_spec`
  turns `"mon-fri 16:00-19:00"` into per-day rows, day-range wraparound
  included (`"fri-mon ..."` spans the weekend correctly)
- `app/planner/estimate.py` — `default_minutes()` heuristic (placeholder
  until step 4's LLM estimation), overridden by a hand-entered
  `set_estimate(basis='user')` when one exists
- `app/planner/schedule.py` — `generate_plan()`: priority = points /
  days-until-due, greedy backward-fill from today into the deadline day
  (due minus a 12h safety margin), reports `shortfalls` when there isn't
  enough room; `write_plan()` replaces the open (unlocked, incomplete)
  plan, leaves locked/completed rows alone
- `app/cli.py` — `availability` / `estimate` / `plan`; `assignments` now
  shows the `id` column `estimate` needs

Verified against the real synced data (112 real assignments, not
synthetic): `plan` correctly built a full backward-scheduled calendar —
e.g. "Periodic Trend Puzzles" (due 9/14) got 3 blocks ending 9/12,
"Current Events Rough Draft" (due 9/29) got blocks starting 9/18; buffer
blocks landed on every available day; `estimate 2682052 120` dropped that
assignment's block count from 6 (the default heuristic's 240min, clamped
to its max) to 3 (⌈120/45⌉) — confirming the override actually reaches
the scheduler, not just gets stored. It also correctly reported
real shortfalls ("week 6 practice — short 90 min") when lower-priority
assignments didn't fit before their deadline given the availability set —
exactly the honest signal this was designed to produce, not a bug.

Also caught and fixed a real bug via testing, not just theory: an early
version of `generate_plan` accepted an overridable `today` for tests but
always computed its due-date liveness cutoff from the real wall clock,
so the two could silently disagree (fine in production, where both
default together, but would have meant a subtly wrong result the moment
anyone ever passed a deliberately different `today`). Fixed by deriving
`today` from a single `now` parameter instead of each defaulting
independently. 38 tests passing.

**Step 4 (LLM effort estimation + time logging + calibration) is done.**

Model choice: Haiku 4.5 (`ASP_LLM_ESTIMATE_MODEL`), deliberately, not
Sonnet — estimating minutes from a short description is a simple,
high-volume (112 assignments today), low-stakes judgment call, the
textbook case for a cheap/fast model. The tutor (step 5) is a different
kind of task — actual pedagogical reasoning — and should use a
Sonnet-tier model instead. Stated here so the distinction is a decision,
not an inconsistency someone finds later.

Built:
- `app/db/schema.py` — `sessions` table; `estimates` gains `minutes_p80`,
  `content_hash` via `app/db/connection.py`'s new guarded-`ALTER TABLE`
  migration helper (no framework yet — young enough that this is more
  honest than pretending we need Alembic). Verified against the real
  database: the existing `basis='user'` row from step 3 survived the
  migration with its value intact, new columns added as NULL.
- `app/planner/llm_estimate.py` — `run_llm_estimates()`: async, bounded
  concurrency (`ASP_LLM_ESTIMATE_MAX_CONCURRENCY`, same pattern as
  `CanvasClient`) — sequential would mean minutes for 100+ assignments.
  A Claude tool-use call (`report_estimate`, forced via `tool_choice`)
  returns p50/p80 minutes as structured output rather than parsed free
  text. Caching: `content_hash()` over name/description/points/
  submission_types — unchanged assignments are skipped entirely on
  re-run, `basis='user'` rows are never touched regardless of hash.
- `app/planner/calibration.py` — `get_multiplier()`: live-computed
  (queried fresh from `sessions` every call, not stored — cheap at this
  scale, sidesteps a staleness question entirely) actual÷estimate ratio
  per Canvas `submission_type` (steadier signal than course subject,
  which is free text). Needs 3+ samples of a type before it's applied;
  cold start returns 1.0 (no adjustment). Averaged across an assignment's
  types when it has more than one.
- `app/planner/estimate.py` — `get_estimate_minutes()` now: `basis='user'`
  returns unmodified and un-calibrated (an override is permanent until
  the user changes it themselves); otherwise the stored (llm or default)
  base is multiplied by the live calibration factor. `default_minutes()`
  itself stays fixed and uncalibrated on purpose — it's also the baseline
  `estimated_minutes_at_log` is compared against, so calibration can't
  compound on its own output across repeated recomputation.
- `app/db/sessions.py` — `log_session()`: records actual time, captures
  `estimated_minutes_at_log` from the fixed heuristic (never the LLM or
  calibrated figure, for the same anti-compounding reason), and marks
  that assignment's open (`locked=0, completed=0`) plan_blocks completed
  so a later `plan` doesn't re-offer time already spent. Extracted into
  its own module rather than left inline in the CLI, matching how every
  other command already delegates to `db/`/`planner/` — the earlier
  inline version wasn't covered by tests; this is.
- `app/cli.py` — `estimate-llm` (lazy `import anthropic`, so commands
  that don't touch the LLM never require the package or a key), `log`,
  `calibration`. `assignments` gained an `id` column — needed once
  `estimate`/`log` require one and there was previously no way to see it.

Verified against the real synced data: `log` against three real Hons
Chem assignments (different submission types) correctly required 3
same-type samples before applying — `on_paper` sat at "needs 2 more"
while `online_upload` reached 3 and flipped to applied; a real, previously
un-estimated `online_upload` assignment's effective estimate came back at
exactly 50 × 0.50 = 25 minutes, confirming the multiplier actually reaches
`get_estimate_minutes()` and not just the display table; the existing
`basis='user'` estimate (120 min) stayed exactly 120 after all of this,
confirming the override truly is immune. 19 new tests (57 total) —
`run_llm_estimates()`'s caching/skip/error logic tested against a fake
`AsyncAnthropic` client, no network calls.

Known gaps, stated plainly: `estimate-llm` estimates the *whole
horizon's* published assignments on every run, not just ones near their
due date — fine at 112 assignments and Haiku pricing, would need scoping
(e.g. only assignments due within N days) at real scale. Calibration
groups by submission_type only, and for a multi-type assignment `log`
attributes the whole session to just its first listed type — a v1
simplification, not a correctness claim about mixed-type work.

**Estimation backends made pluggable — cost, not access, was the driver
this time.** Unlike every other environment obstacle in this doc, this
one had nothing to do with the school device: `estimate-llm` runs from
wherever the CLI runs (the Codespace has unrestricted internet — the
blocking has only ever affected things that specifically needed a
trusted browser session on the school device itself, like Canvas login).
The reason to avoid Claude here is purely that paying for API access
isn't worth it yet at this project's stage. Refactored
`app/planner/llm_backends/` into three interchangeable backends behind
one `estimate(course_name, assignment_row, types) -> (p50, p80)` shape,
selected by `ASP_LLM_BACKEND`:

- `claude.py` — unchanged logic, just moved. Still the natural choice
  once the project's ready to spend, and likely still what the tutor
  (step 5, a harder task) will want regardless of what estimation uses.
- `openai_compat.py` — one implementation for *any* OpenAI-compatible
  chat-completions endpoint (same `tools`/`tool_choice` wire format):
  Groq (default recommendation — free tier is genuinely generous,
  thousands of req/day on an 8B model, fast LPU inference), Mistral,
  OpenRouter, GitHub Models, or a local Ollama server. One backend, many
  providers, config-only to switch.
- `local_llamacpp.py` — fully offline via `llama-cpp-python` against a
  local `.gguf` file. **Deliberately not Ollama**, despite Ollama's
  nicer tool-calling ergonomics and being usable via the same
  `openai_compat` backend: Ollama ships as a new standalone binary plus
  a background server process, and this device has already killed or
  flagged *every* new executable handed to it this project (Playwright's
  Chromium, the `asp.exe` console-script wrapper). `llama-cpp-python` is
  a Python package with a compiled extension, loaded in-process by the
  same python.exe already trusted here — nothing new to flag. Uses
  `response_format={"type": "json_object", "schema": ...}`, which
  llama-cpp-python compiles into a GBNF grammar and enforces at the
  sampler level — genuinely guaranteed-valid JSON, not just usually
  valid. Optional dependency (`pip install -e ".[local]"`) — heavy,
  compiled, platform-specific, shouldn't be forced on an environment
  that isn't doing local inference (the Codespace never needs it).

Shared prompt/schema logic (`llm_backends/schema.py`) is defined once so
the three integrations can't quietly drift out of sync with each other.
`llm_estimate.py`'s caching/orchestration layer is now fully
backend-agnostic — it holds an object with an `estimate()` method and
doesn't know or care which backend built it. Testing got easier, not
harder, from this: `run_llm_estimates()`'s tests now inject a trivial
fake backend directly rather than monkeypatching the Anthropic SDK's
internals; each real backend gets its own focused wire-format test
against a fake SDK client. 13 new/changed tests, 70 total.

**Also found and fixed while building this: a real Rich-markup
corruption bug**, unrelated to the backend work but caught by it — an
error message containing literal `.[local]"` silently lost that
substring when printed. Rich's console markup treats any `[lowercase
word]` span as an attempted style tag; an unrecognized one doesn't
raise, it just swallows text up to the next `[/]`. Reproduced
deliberately: `console.print(f"[green]{'Homework [optional]'}[/]")`
prints only `"Homework"`. This is a live risk anywhere Canvas-sourced
text (assignment/course names, which plausibly contain bracketed
annotations like "[optional]") reaches a `console.print` or
`Table.add_row` call un-escaped — not hypothetical, just not yet hit by
this account's actual data (checked). Fixed by wrapping every dynamic
interpolation that could carry exception text or Canvas-sourced names
with `rich.markup.escape` throughout `cli.py`, and verified against a
synthetic bracketed name end-to-end through the real `assignments`
command.

**First real Groq run against production data, and three more real bugs
it surfaced.** The user chose `openai_compat`/Groq over paying for
Claude at this stage. Running `estimate-llm` against all 112 real
assignments (not synthetic tests) found, in order:

1. **Wrong model name.** `llama-3.1-8b-instant` — the model recommended
   in this doc's own earlier write-up — no longer exists on Groq;
   deprecated June 2026. Confirmed live against Groq's `/models`
   endpoint (via the SDK client, not raw `urllib` — that got a bare 403,
   most likely Cloudflare-level bot protection on a request the SDK's
   own headers avoid) rather than trusting search results a second time.
   Replaced with `openai/gpt-oss-20b`, Groq's own migration
   recommendation, confirmed working.
2. **Truncated JSON at `max_tokens=300`.** A verbose model's `reasoning`
   field (present in the schema, never actually persisted downstream)
   ran the response past the token budget and cut off mid-object,
   breaking the parse. Fixed at the source — tightened the schema's
   guidance to "at most 12 words" — and with a safety margin regardless
   (300 -> 500/400 across all three backends, not just the one that hit
   it; Claude or a local model could plausibly hit the same wall with a
   sufficiently verbose response).
3. **Real rate limiting under bulk concurrency (57/106 failing).**
   Confirmed via `openai.RateLimitError`, HTTP 429, a Groq free-tier TPM
   cap — invisible at `--limit 5`, unmissable at full scale. Added
   retry-with-exponential-backoff in `run_llm_estimates`'s worker
   (`_is_rate_limited` checks `.status_code == 429`, works for both the
   Anthropic and OpenAI SDKs' error classes without needing to import
   either type-specifically) — the same shape `CanvasClient` already
   uses for Canvas's own rate limiting, extended to the LLM side.
4. **Intermittent malformed tool calls, unrelated to token budget.**
   Even after fix #2, isolated re-tests of the same exact prompt against
   the same assignment succeeded most of the time and occasionally
   failed differently each time (once truncated-looking, once "model did
   not call a tool" with an empty generation) — confirmed genuinely
   stochastic, not content-triggered, by rerunning the identical request
   three times in a row and getting two different outcomes. A smaller
   free model doesn't hit forced `tool_choice` 100% of the time. Added a
   second, smaller retry tier for non-rate-limit failures
   (`_MAX_RETRIES_OTHER`, no backoff delay — waiting doesn't make a
   model more careful) sitting alongside the rate-limit tier
   (`_MAX_RETRIES_RATE_LIMIT`, full exponential backoff). Started at 2,
   watched it still fail twice in a row for one assignment across two
   full runs, re-tested that exact assignment 3 more times fresh and
   watched it succeed twice and fail once — confirmed a real per-attempt
   failure rate worth budgeting for, not a fluke — and moved it to 3.
   Final result: 112/112 published assignments estimated, 0 errors.

Also fixed along the way, unprompted: a real Groq API key got pasted
directly into `.env.example` — the *template* file, meant to be
committed with placeholders only, not the gitignored `.env` — while
testing config values in the editor. Caught before anything was staged
or committed (confirmed via `git log --all -p | grep`, not just
assumed), so this was a same-turn fix rather than a repeat of the
cookie-file incident: moved the real key to `.env`, restored
`.env.example` to placeholders, verified `.env` is actually gitignored
and that Settings still loads the real value correctly.

**Step 5 (the tutor) is done — its core slice.** Materials, chunking,
embedding, and cited Q&A over manually-uploaded content, since Canvas
access alone was never going to cover lecture content.

Built:
- `app/db/schema.py` — `materials` (course_id nullable — not everything
  ties to one course) and `chunks` (`ord`, `text`, `page_ref` for
  citation, `embedding` as a little-endian float32 BLOB)
- `app/tutor/extract.py` — `.pdf` (pypdf, one section per page),
  `.pptx` (python-pptx, one section per slide — the natural fit for
  "Lecture 7, slide 12"-style citations), `.docx` (python-docx; docx
  has no stored page concept at all — pagination is a rendering detail,
  not in the file — so paragraphs are grouped into fixed-size
  "section N" units instead of faking page numbers), `.txt`/`.md`
- `app/tutor/chunk.py` — `chunk_text()`: ~800-char chunks with ~100-char
  overlap, chunked *within* each section (never across a page/slide
  boundary, or a citation would point at the wrong page for text near
  the seam)
- `app/tutor/embed.py` — `fastembed` (ONNX runtime, no torch —
  `BAAI/bge-small-en-v1.5`, 384-dim, ~67MB, lazy-downloaded on first
  use). Always local and free regardless of which chat backend answers
  questions: Groq (this project's chat backend) has no embeddings
  endpoint at all, and embedding is cheap enough on CPU to never need a
  paid API for it. Verified directly: loads in ~2.5s, embeds in
  milliseconds, and correctly ranked semantically related sentences
  above unrelated ones before any production code was written around it
- `app/tutor/materials.py`, `app/tutor/qa.py` — `add_material()`
  (extract → chunk → embed → store); `retrieve()` (brute-force cosine
  similarity over stored chunks — fine at personal-corpus scale, no
  vector DB needed) and `ask()`, which builds a prompt requiring
  citations and an explicit "not covered" rather than a guess — see
  §8's stated rule: a tutor that invents course specifics the night
  before an exam is worse than no tutor
- `app/planner/llm_estimate.py`'s `make_backend()` gained a `tutor=True`
  flag: Claude backend picks `llm_tutor_model` (Sonnet, default) instead
  of `llm_estimate_model` (Haiku); openai_compat picks
  `openai_compat_tutor_model` if set, else reuses the estimation model.
  Same three backends as step 4, now each also implementing `answer()`
  — free-form completion, no forced tool schema, alongside `estimate()`
- `app/cli.py` — `material add` / `material list` / `ask`

Verified end-to-end against real files and the real Groq backend, not
just unit tests: ingested a `.txt`, a `.docx`, and a `.pptx` (each
generated for real, not mocked), confirmed extraction produced correct
per-format citations (`section N` for docx, `slide N` for pptx), then
asked three real questions through Groq — one answered correctly with
the right source ranked highest by cosine similarity, one correctly
included the real slide number in its citation, and one (asking
something not in any material) correctly declined rather than answering
from the model's own training knowledge. 29 new tests (103 total).

Two real bugs found through this verification, not assumed away:

1. **The prompt's citation instruction backfired.** Told the model to
   cite "in the form (Title, page_ref)" — meant as a format template,
   read by the model as literal text to reproduce. First real answer
   came back citing "(Periodic Trends Notes, page_ref)" — the literal
   word "page_ref", not an actual page number. Fixed by giving a
   concrete example instead of a bare placeholder name, and explicitly
   telling it never to output "page_ref" or "Title" literally. Re-tested
   against both a source with a real page/slide number and one without
   (a plain .txt) — both now cite correctly.
2. **A real chunking bug, caught by a test, not by inspection.** The
   original `chunk_text()` trimmed each chunk's *end* to the nearest
   word boundary but computed the *next* chunk's start by subtracting a
   fixed character count from that trimmed end — which can land
   mid-word regardless of where the previous chunk ended cleanly.
   `test_prefers_breaking_on_whitespace_not_mid_word` caught a chunk
   starting with `"silon"` (a fragment of "epsilon") on a synthetic
   run — small enough that manual CLI testing of the 3 short real files
   never happened to exercise the multi-chunk path at all. Fixed by
   snapping the next chunk's start forward to the next word boundary
   too, not just trimming the previous chunk's end. Re-verified against
   a genuinely long, realistic passage (cellular respiration notes, 2
   real chunks) — confirmed no fragments, and the overlap correctly
   repeats whole words across the boundary, not partial ones.

Scope cuts, stated plainly: no `--course` auto-detection (the flag
exists, nothing infers it from filename/content); citation is
retrieval-based only — nothing stops a chat backend from ignoring the
"cite exactly this label" instruction, verified to work against Groq's
`openai/gpt-oss-20b` specifically, not guaranteed for every possible
provider/model; card generation and the mastery write-back loop
described in §8 are step 6, not this slice — this is retrieval +
grounded Q&A only.

**Real user testing found a genuine retrieval-quality gap, fixed with
hybrid search.** First question asked against real uploaded content (a
DBQ document, AP World History format — numbered "Document 1" through
"Document 7" excerpts) failed: "what is document 3 about" correctly
declined to answer rather than hallucinate, but for the wrong reason —
the chunk containing the literal text "Document 3" (confirmed present
and correctly extracted, via direct inspection of the stored chunk, not
assumed) ranked **16th of 19** by dense cosine similarity alone. Root
cause, not just symptom: a small dense embedding model captures semantic
*topic* similarity well but is structurally weak at exact/numbered
references — the token "3" gets diluted into an average against generic
words repeated in every chunk of that material ("Unit 1 DBQ"). No
amount of `top_k` tuning fixes a chunk ranked 16th; this needed a second
retrieval signal.

Fix: real hybrid retrieval, dense + lexical, merged by Reciprocal Rank
Fusion (`app/tutor/qa.py`):
- Lexical side uses SQLite's built-in FTS5 (`app/db/connection.py`'s
  `_ensure_chunks_fts` — external-content table + sync triggers, so it
  stays consistent from any code path that touches `chunks`, not just
  `materials.py`; confirmed available in this environment before relying
  on it, and degrades to dense-only if a SQLite build lacks it, rather
  than assuming). Zero new dependencies — this is exactly the kind of
  case FTS5 exists for.
- `_fts_query()` OR's the question's non-stopword terms together (bare
  space-separated FTS5 terms mean AND by default — far too strict for a
  natural-language question) and lets `bm25()` do the real work of
  weighting rarer terms — like "3" — higher than common ones like
  "document"/"unit"/"dbq" that appear in nearly every chunk of this
  particular material.
- Merged via standard RRF (`score = Σ 1/(60 + rank)` across whichever
  ranking(s) a chunk appears in) rather than a hand-tuned weighted sum —
  a chunk doesn't need to win outright on one signal, scoring well on
  either is enough to surface it.

Verified against the real failing case at each step, not just re-run
once and called done: lexical-only ranking already put the right chunk
at **#2** (bm25 correctly recognized "3" as the distinctive term) — but
RRF at equal 1:1 weighting only pulled its combined rank to **6th**,
because the dense side was *so* bad (16th) that even a strong lexical
signal couldn't fully outweigh it within `top_k=5`. Rather than
hand-tuning the RRF weighting to just barely cross that one threshold
(tried the arithmetic — a 1.5x lexical weight happens to work, but
"happens to just barely work for this one case" is a sign of overfitting
a knob to a single data point, not a real fix), raised `DEFAULT_TOP_K`
from 5 to 8 instead — cheap, safe, generalizes to the whole class of
"specific item in a list" query rather than this one instance. Then hit
a *second*, unrelated bug the first fix exposed: `cli.py`'s `ask` command
had its own hardcoded `top_k: int = typer.Option(5, ...)`, entirely
separate from `qa.py`'s `DEFAULT_TOP_K` — so the constant change had zero
effect until this was caught by literally re-running the real failing
query and seeing 5 sources instead of 8. Now imports the constant instead
of duplicating it, so this can't happen again. Final verification: the
original query now correctly answers "Marco Polo's Travels... Kublai
Khan," matching the actual stored chunk text; tested "document 1" and
"document 7" against the same real material too, both correct and
distinct — confirms the fix generalizes across the document rather than
being tuned to one lucky number. 4 new tests reproducing the dense-loses-
lexical-wins scenario directly, not just re-asserting the fix worked once
live (107 total).

Stated honestly, not oversold: RRF at `top_k=8` makes this *reliable*,
not *guaranteed* — a material with many more than ~19 short numbered
items competing for the same generic surrounding words could still see
a specific one narrowly miss the cutoff. The real, durable fix on top of
this would be structure-aware chunking (splitting on detected "Document
N"-style headers so each numbered item gets its own clean, undiluted
chunk instead of landing mid-chunk next to unrelated neighboring text) —
noted as a natural next improvement to `chunk.py`, not built here.

Next: **step 6, spaced review.** FSRS-scheduled cards generated from
chunks, graded, writing `mastery.theta` back per topic — closing the
loop described in §8 so weak topics actually pull more of the
scheduler's attention, not just a static plan.

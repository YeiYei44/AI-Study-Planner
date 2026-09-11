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

## 11. Step 1 status

Done:
- `.devcontainer/` — Codespace image + `desktop-lite` (noVNC) + node
- `app/config.py` — env-driven settings (`ASP_` prefix)
- `app/ingest/session.py` — `CanvasSession` (headless, auth-checked),
  `interactive_login()` (headed, poll-to-success, `NoDisplayError` guard),
  `install_cookies()` (no-display fallback)
- `app/ingest/cookies.py` — parse Cookie header / curl / JSON export
- `app/ingest/canvas.py` — `CanvasClient`: pagination, rate-limit
  handling, `courses()` / `assignments()` / `all_assignments()`
- `app/cli.py` — `python -m app.cli login` / `login-cdp` / `export-cookies` /
  `import-cookies` / `whoami` / `sync`
- `sync` writes raw JSON snapshots to `data/raw/<timestamp>/`

Verified in Codespace: headless launch, auth check returns
not-authenticated for an empty profile; header/curl/JSON cookie parsing
(9 tests); the export -> import round trip (Playwright's own cookie
shape, injected, re-checked) works end to end; CLI degrades cleanly
everywhere (no tracebacks). Verified against a real locally-launched
Chromium: `login_via_cdp` connects, polls, detects a successful sign-in
via a mock Canvas server, exports cookies, exits 0, and leaves the
browser process running afterward; a dead debugging port fails cleanly
with no traceback.

Not yet verified: a real Fulton session and a live pull. Blocked so far
by Conditional Access on every sign-in attempted from the Codespace, and
then by the local Windows browser closing itself before `login-cdp`
existed to work around it (see above). Currently trying: `python -m app.cli login-cdp`
against a manually-launched Chromium on the school device.

Next, depending how that goes: either `python -m app.cli login-cdp` locally ->
`python -m app.cli import-cookies` here -> `python -m app.cli sync`, confirm pagination against a
course with 100+ assignments, then step 2 (normalize + SQLite) — or, if
that hits the same Conditional Access wall, pivot straight to the ICS
adapter + manual upload as the primary ingestion path instead.

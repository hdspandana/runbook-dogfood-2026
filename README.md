# RUNBOOK

> **Runbook is a self-hosted hackathon submission and judging platform that turns real event data into a live operational view showing organizers what is complete, what is active, what is blocked, what became stale, and what they should do next.**

A hackathon portal usually answers *“what data exists?”* Runbook answers *“what is true now, what is preventing progress, and what should I do next?”* It is not a generic checklist. Every Runbook card is recomputed from the event database on each request; the only persisted workflow history is the last normalization snapshot, so the dashboard can detect **STALE** results when reviews or rubric data change.

## Status

**T1 + T2 implemented and verified (7/7 official checks; 51 passing pytest tests; headless-browser responsive audit with 0 overflow failures).** `acceptance-report.txt` was produced by actually running `python3 run.py .dogfood.toml` against a running portal; `pytest -q` also exercises role isolation, normalization and Runbook behavior. No higher tier is claimed. The root `fixtures.json` is the official DOGFOOD 2026 fixture (event, 8 tracks, 30 judges, 40 teams, 41 project entries and 126 score entries); it is **not replaced by a tiny invented demo**. Its duplicate submission and duplicate team display names are adapted without breaking database constraints (see below). An optional second, live-window pilot event makes participant-side create/edit/submit demonstrable despite the official event's closed submission deadline.

## One-command local start

```bash
docker compose up --build
```

Open **http://localhost:8080**. The first boot creates `/data/runbook.sqlite3`, loads the official fixture, creates a current-window pilot event, and prints the five fixed demo session mappings used by the acceptance checker. Subsequent boots preserve the SQLite volume and do not reseed. Once the image is built, startup is offline: no hosted database, mail service, external API, CDN, JS build or cloud account. If port 8080 is busy, change the left side of `8080:8080` in `docker-compose.yml` and set `base_url` in `.dogfood.toml` to match.

Direct Python alternative (for development, no Docker):

```bash
python3 -m pip install -r requirements.txt
RUNBOOK_DATA_DIR="$PWD/.runtime" python3 server.py
```

The app binds to `0.0.0.0:8080` by default. `GET /api/health` returns a simple liveness check.

### Seeded browser accounts

| Role | Email | Password | What to open |
|---|---|---|---|
| Organizer | `organizer@example.org` | `organizer123` | `/organizer/runbook` |
| Admin | `admin@example.org` | `admin123` | `/organizer/runbook` (organizer/system capabilities; no judge or participant access) |
| Judge | `marek.nowak@example.org` | `judge123` | `/judge` |
| Participant (official event, deadline closed) | `priya1@example.org` | `participant123` | `/projects` |
| Participant (live pilot, no project yet) | `pilot11@example.org` | `participant123` | `/projects` |

Registration at `/register` creates participant accounts only; an organizer can issue judge-invite links at `/organizer/events/<event_id>/judges`. Demo credentials and the fixed acceptance sessions are intentionally not secret; **disable the fixed sessions and rotate passwords/secret before deploying to a real event** (`RUNBOOK_DEMO_SESSIONS=off`, `RUNBOOK_SECRET_KEY=...`, HTTPS with `RUNBOOK_COOKIE_SECURE=1`).

### Seven acceptance checks

```bash
# While the portal is listening on http://localhost:8080:
python3 run.py .dogfood.toml > acceptance-report.txt
cat acceptance-report.txt
```

The official checker reads the root `fixtures.json` and `.dogfood.toml`. The four `[auth]` values are **real Cookie headers for server-side sessions**, not fake role headers. `judge_scores` and `peer_scores` point to the same review URL: judge A owns it and gets 200; judge B's session gets 403. `submit` is `/api/projects`, which resolves an omitted event id to the official fixture event and enforces its actual past deadline. The checker never relies on a client-side clock. You can also run:

```bash
pytest -q
python3 scripts/smoke.py
```

## Workflows

### T1: participants and gallery

1. Sign in or register as a participant.
2. Create a team in an event or join through a random 192-bit invite link. A participant may belong to at most one team per event.
3. Create **one** project for that team and event (`UNIQUE(event_id, team_id)`). It starts as a private draft.
4. Submit. It becomes public in `/gallery`, is eligible for judge assignment, and remains editable until the event deadline.
5. At `server_UTC_time >= submission_deadline`, creation, editing and submission are rejected by the backend with 403. Disabling buttons in the browser is only UX, not enforcement. The official event's deadline was **1 March 2026 18:00 UTC**; the live pilot remains open into November 2026 for an interactive demo.
6. Visitors need no account to browse `/gallery`, filter by track, search by project/team/track, and open submitted project details. Drafts, judge data, assignments, comments and normalization internals are not public.

Organizers create events with UTC windows, tracks, prizes and a weighted rubric; every event-owned record has an `event_id`.

### T2: judging

Judges are invited via self-hosted links. The organizer runs deterministic **round-robin assignment within track** (existing assignments are kept); judges open `/judge`, see only their assigned projects, review project details, score a weighted rubric, and submit or save a pending draft. Only their own review is readable/writable, while the judging window is open. Organizers see assignment coverage, unassigned projects, judges with zero assignments, live progress by track, and normalization results. Scores are normalized **across judges on weighted per-review totals**; see [JUDGING.md](JUDGING.md) for exact equations, edge cases, completion policy, tie-breaking and caveats.

### Runbook: the operational layer

The organizer's home is `/organizer/runbook`. Its eight stages are Event Setup, Tracks & Prizes, Judging Rubric, Judge Assignment, Submissions, Judging, Normalization, Results / Export. Each returns exactly five kinds of state: `UPCOMING`, `ACTIVE`, `COMPLETE`, `BLOCKED` or `STALE`. A card has a metric, a reason, evidence where useful, a next action and a target URL. The next-action engine chooses the earliest unmet required dependency in workflow order, never a downstream stale normalization while judging is unfinished. Idle judges are a load observation, not a coverage failure. Submissions is deliberately an *observation* after the deadline, not an organizer checkbox to “close” a window that already closed itself.

**Signature scenario.** The official fixture boots with a normalization run recorded *before* the last six completed reviews were restored, so its normalization is genuinely STALE: stored review count is six behind current. The dashboard explains the gap and recommends finishing judging first. Re-running normalization during incomplete judging can make the snapshot current but does not make it publishable; when a judge finishes or edits a review, the fingerprint changes and the snapshot becomes STALE again. The chain is observable through `/api/events/1/runbook`, `/api/events/1/normalize`, and `/api/events/1/export.csv`.

Runbook is informational. A BLOCKED or STALE card **never changes permissions**. Authorized organizers can export the CSV at every judging stage, including before normalization and after a review change. Pending reviews have `review_status=pending` and empty score fields; they are never zero scores. The export labels the last normalization snapshot as `not_run`, `current` or `stale` and includes live computed scores only for completed reviews. Both browser and JSON publish routes require complete assignment/review coverage and a persisted normalization matching current inputs; otherwise they return 409. Each successful publication records the exact normalization run it certified; later input changes — or a later normalization run — mark the historical publication STALE until the organizer republishes, so a publication timestamp is never presented as evidence that the current ranking is unchanged. A normalized ranking during partial judging is provisional, not final.

## Fixtures and awkward cases

The root `fixtures.json` is the default source; `fixtures/fixtures.json` is a separate generated sample and is not read by default. The official root `fixtures.json` is loaded via `app/fixtures/official.py`, then the ordinary, event-scoped loader. The seed is idempotent on an existing volume. The official file contains duplicate display names for distinct teams; the adapter suffixes later occurrences with their fixture team IDs so names remain unique. It also contains a **41st project for the same team as project 7**, which conflicts with the required `UNIQUE(event_id, team_id)` rule. The adapter keeps the team's first project as canonical and records the ignored duplicate project's ID and associated score entries in seed metadata; it does not pretend the duplicate is a second legal submission. The official scores remain attached to their canonical projects. An incomplete review batch is represented by generated *pending assignment slots* on every third submitted project, alongside the official scored rows. A six-review normalization holdback provides the initial STALE scenario.

The live pilot is supplementary; it never replaces the official fixture. It uses a separate event id, three tracks, eight submitted projects, two drafts, two participant teams with no project, four enrolled judges, 16 pending assignments and a judging period starting in November 2026. Open `/events/2`, sign in as `pilot11@example.org`, create and submit a project, then confirm that it appears only in `/gallery?event=2`. For a full team-invite demo, register a new account, create a team for event 2, copy its `/join/<token>` link to a second account, and join it there.

## Files and architecture

- `app/schema.sql` — SQLite constraints and event-scoped entities.
- `app/security.py` — session/role/resource authorization and UTC deadline checks.
- `app/judging/service.py` — rubric, weighted raw total, assignment, progress.
- `app/judging/normalization.py` — mean, population sigma, z-score, fallbacks, ranking, snapshots, freshness.
- `app/judging/publication.py` — transactional coverage-and-freshness check shared by both publish routes.
- `app/runbook/service.py` — deterministic workflow facts → cards → next action.
- `app/exports/service.py` — stage-aware CSV, independent of Runbook status.
- `app/fixtures/official.py` — official fixture adapter; `app/fixtures/demo.py` — live-window pilot.
- `tests/` — reproduced seven checks plus authorization, Runbook, publication, CSV, lifecycle and schedule regressions.
- [PHASES.md](PHASES.md) — the phase-by-phase build order with a reproducible gate per phase and its verification status.
- [ARCHITECTURE.md](ARCHITECTURE.md), [DATA-MODEL.md](DATA-MODEL.md), [JUDGING.md](JUDGING.md) — design and exact behavior.

## Production notes / honest limits

This is a one-container offline-capable hackathon portal built for DOGFOOD judging, not a hardened Internet-scale SaaS. The included Flask server is suitable for the local demo; a production deployment should use a WSGI server behind HTTPS, set a non-default secret key, disable demo sessions, rotate demo passwords, add a CSRF defense to browser POST forms, set secure cookies, and back up the SQLite volume. There is no email delivery, OAuth, account recovery, rate limiting, score history or public ranked leaderboard. Results publication records a timestamp, but this timestamp is not automatically cleared by subsequent changes to reviews or other inputs; use the live freshness state to detect a later stale result. Per-judge scores remain private even afterward. The simple round-robin assignment is auditable, not optimized for balance or impartiality. See [JUDGING.md](JUDGING.md) for statistical limitations.

## Licence

MIT — see [LICENSE](LICENSE).

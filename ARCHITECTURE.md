# Architecture

```text
Browser / acceptance checker
        ↓ HTTP (HTML or JSON)
Flask route blueprints (thin adapters)
        ↓
services/domain logic (auth, projects, judging, normalization, Runbook, export)
        ↓ parameterized SQL
SQLite file in /data (WAL, foreign keys, constraints)
```

RUNBOOK is intentionally a single Python process and a single embedded database. No queue, Redis, hosted DB, OAuth, email, frontend build, external API or runtime CDN. `docker compose up` builds one container and attaches a persistent SQLite volume. After the image is built, the portal can boot with the network disconnected.

## Startup

`server.py` calls `create_app()` in `app/__init__.py`. The factory reads environment-driven configuration, creates the schema with `CREATE TABLE IF NOT EXISTS`, seeds the official `fixtures.json` through `app/fixtures/official.py` and `app/fixtures/loader.py` **only if there is no event**, seeds or repairs a supplementary live pilot, creates documented fixture-only server-side sessions, registers route blueprints, and starts listening on `0.0.0.0:8080`. `app/db.py` opens a per-request SQLite connection with `foreign_keys=ON`, WAL and a busy timeout. Mutations that span multiple rows use `BEGIN IMMEDIATE` transactions. Subsequent starts do not erase or reseed an existing database.

## Route layout

| Area | Primary files | Responsibility |
|---|---|---|
| Authentication | `app/auth/service.py`, `app/auth/routes.py`, `app/security.py` | Hash passwords, opaque server-side sessions, role/resource checks. Flask's signed flash cookie is separately named `runbook_flashes` so it cannot replace the auth cookie `runbook_session`. |
| Events | `app/events/service.py`, `app/organizer/routes.py` | UTC windows, tracks, prizes, rubric and judge onboarding. |
| Teams and projects | `app/teams/routes.py`, `app/projects/service.py`, `app/projects/routes.py` | Formation/invites, one team per user per event, one project per team per event, drafts and submission, server-side deadline gates. |
| Public gallery | `app/gallery/routes.py`, `app/api/routes.py` | Submitted projects only; no joins to review or assignment data. |
| Judging | `app/judging/service.py`, `app/judging/routes.py` | Assignment, score validation/updates, resource-level judge scope, progress. |
| Normalization | `app/judging/normalization.py` | Weighted raw totals, per-judge population statistics, z-scores, neutral/raw fallbacks, snapshot persistence and fingerprint-based staleness. |
| Runbook | `app/runbook/service.py`, `app/runbook/routes.py` | Derive the eight operational cards and a deterministic next action from database facts on each read. No stored checklist statuses. |
| Export | `app/exports/service.py`, `app/exports/routes.py` | Operational CSV with pending rows and explicit normalization status; organizer-only and **never** gated by Runbook state. |
| Tests | `tests/`, `run.py`, `scripts/smoke.py` | Official seven checks, security matrix, normalization/Runbook tests, smoke walkthrough. |

## Authorization

Authentication identity comes from the opaque random `runbook_session` cookie, resolved against the `sessions` table and checked for server-side expiry. No role claim, judge id, user id or team id supplied in a form/query/JSON/header can identify the caller. Role/capability policy lives in `app/security.py`. Organizers and admins can operate on events and export. Admin is a real role; its capabilities are an explicit organizer superset plus `system:manage`. Judges have only their own assigned projects/reviews; a judge's `GET /api/judging/reviews/<id>` requires that `review.judge_id == session user id`; a project review additionally requires a row for that `(project_id, session user id)` in `assignments`. A track-progress read requires an assignment in that track, and reports only the current judge's counts. Participants can manage only the projects of teams they belong to. Visitors can read the public gallery without login. JSON protected routes return 401 or 403 rather than redirecting to a login form; denied responses do not serialize the protected resource.

Deadline policy lives in `app/security.submissions_closed`: `current_server_utc >= submission_deadline` is closed. Every participant-side project write (create, edit, submit) invokes that check server-side; JavaScript and display text are not authoritative. Judge write policy is analogous: only `judging_starts_at <= current_server_utc < judging_ends_at` permits saving a review.

## Normalization and Runbook

The rubric is common across all tracks in one event. Completed reviews produce weighted raw totals; each judge's population mean/sigma provides a z-score for each completed review, with neutral z for zero variance or fewer than 3 completed reviews. Pending rows are excluded rather than scored zero. Projects aggregate completed review z-values; if no judge has meaningful variation, the event uses raw mean as the final score. `app/judging/normalization.py` is the implementation; [JUDGING.md](JUDGING.md) is the exact mathematical contract.

Every Runbook read recomputes facts from the SQLite database: event phase and windows, track/prize counts, rubric validity, assignment coverage, submission counts, review progress, and normalization freshness. No checkbox or giant workflow table. The single persisted history is `normalization_runs`: the latest run's result JSON and SHA-256 fingerprint of completed criterion scores, the submitted-project roster, rubric and event/track/team result metadata. A mismatch after a judge edits/completes a review produces `STALE`. `app/judging/publication.py` supplies the shared publication policy: publishing is allowed only with complete coverage and a current persisted normalization, and it records the id of the exact normalization run certified. The derived publication state (not_published / current / stale) is recomputed on read, so later input changes or a later normalization mark the historical publication STALE until it is republished; a timestamp alone never certifies a ranking. The next-action engine chooses the earliest unmet required prerequisite; unfinished judging takes precedence over stale normalization, always with an explanation and URL. It never authorizes an HTTP request.

## Export semantics

The CSV route requires organizer/admin identity and an existing event — **nothing else**. It works while judging is incomplete and after a result becomes stale. Pending assignments have rows with empty score fields; unassigned projects have `unassigned` rows. Live computed numbers are included for completed reviews; the file also states whether the last persisted normalization is missing/current/stale and when it was produced. This separation of *operational export* from *published final result* is a design invariant and has a regression test.

## Self-hosting and limitations

SQLite, a persistent Docker volume and a single process make backups/migration simple. The optional second event shares the same schema and is always scoped by `event_id`. There is no browser CSRF token in this build and no rate limiting; do not expose the demo config directly to the public Internet. Use a WSGI server/TLS proxy, rotate seeded passwords, set `RUNBOOK_COOKIE_SECURE=1`, disable fixture demo sessions, add CSRF protection and back up the volume for a real deployment. The Docker image can be started offline after dependency installation/build; `docker compose up --build` itself naturally needs the base image/dependencies if not already cached.

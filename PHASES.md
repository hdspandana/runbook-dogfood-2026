# Build phases and gates

RUNBOOK was built in the phase order from the brief (Foundation → T1 → T2 → CSV →
Normalization → Runbook → Polish). Each phase has a **gate**: a reproducible
command that must pass before the next phase starts. Nothing in a later phase is
claimed on the strength of an unverified earlier one.

Reproduce every gate in this order:

```bash
python3 -m pip install -r requirements.txt
pytest -q                       # gates 1–7 (see final test count in acceptance-report.txt)
python3 scripts/smoke.py        # gates 1–4 end to end, against a throwaway DB
docker compose up --build       # gate 0: boot
python3 run.py .dogfood.toml    # gates 2–4: the official seven checks
python3 scripts/demo_flow.py    # gates 5–7: the causal chain, live over HTTP
```

---

## GATE 0 — Container and boot · **UNVERIFIED (Docker unavailable in this sandbox)**

| | |
|---|---|
| Built | `Dockerfile` (python:3.13-slim, one process), `docker-compose.yml` (single service + SQLite volume), `server.py` binds `0.0.0.0:8080`, `/api/health` |
| Gate | `docker compose up --build` → `GET /api/health` = 200, portal usable with no network |
| Evidence | Same boot path verified without Docker by running `server.py` on an empty data dir (see gate 1). Docker itself could not be executed here — **this is the one gate not machine-verified in this environment**, and it is reported as such rather than claimed. |
| Residual risk | Base-image pull needs network once; after that the image runs offline. Anyone running `docker compose up` locally should eyeball the printed seed summary. |

## GATE 1 — Foundation · **PASS**

| | |
|---|---|
| Built | Flask app factory, env config, SQLite schema (`app/schema.sql`), `app/db.py` connection/transaction helpers, opaque server-side sessions + password hashing, official-fixture adapter and loader, live-pilot seed, boot-time bootstrap |
| Gate | App boots on an empty data dir, schema created, fixture seeded, second boot is a no-op, fixed demo sessions map to the four acceptance roles |
| Reproduction | `pytest -q` (all tests boot real apps), `python3 scripts/smoke.py` |
| Evidence | Fresh boot: 2 events, 11 tracks, 13 prizes, 52 teams, 50 projects, 7 rubric criteria, 152 assignments, 366 criterion scores, 135 users, 5 demo sessions. Re-seed prints `already seeded`. Demo session mapping is stable across boots. |

## GATE 2 — T1 acceptance (gallery + deadline) · **PASS**

| | |
|---|---|
| Built | Public home/event/gallery/project pages, search + track filter, team creation, 192-bit invite links, join-by-token, one team per user per event, one project per team per event, draft→submitted lifecycle, server-side UTC deadline gate on **create, edit, submit, team create and team join** |
| Gate | Checks 1–3 of the official suite: public gallery 200 without auth; a known fixture project appears; a post-deadline submission returns 4xx |
| Reproduction | `python3 run.py .dogfood.toml` (checks 1–3), `pytest -q tests/test_acceptance_suite.py tests/test_deadlines_and_lifecycle.py` |
| Evidence | 200 / PASS / PASS. `test_live_pilot_create_edit_submit_and_duplicate` also proves the positive path on the live event: draft is 404 publicly, edit works before the deadline, submit makes it public, a duplicate project is 409 **and** rejected by `UNIQUE(event_id, team_id)` at the database level. Deadline boundary tested either side of the microsecond. |

## GATE 3 — T2 acceptance (judging + isolation) · **PASS**

| | |
|---|---|
| Built | Judge invites/onboarding, deterministic round-robin-within-track assignment, weighted rubric (validated to 100%), judge queue, browser scoring UI, judge-review API, track-scoped progress, assignment coverage detection |
| Gate | Checks 4–6: judge reads own scores (200); judge reads a peer's scores (401/403, no leaked values); participant reads judge scores (401/403) |
| Reproduction | `python3 run.py .dogfood.toml` (checks 4–6), `pytest -q tests/test_security_matrix.py` |
| Evidence | Own review 200; peer review 403 with body `{"error":"forbidden"}` and no `raw_total`/`criteria` keys; participant 403. Also verified: unassigned project → 403, `?judge_id=<peer>` spoof → 403, cross-track progress → 403, other team's project → 403, visitor/judge/participant → Runbook 403, admin has organizer capability. |

## GATE 4 — CSV export · **PASS**

| | |
|---|---|
| Built | Stage-independent CSV export for organizer/admin only, one row per `(project, judge)` assignment, criterion columns, raw total, judge statistics, z / normalized / project score, `review_status` incl. `pending` and `unassigned`, normalization status + timestamps |
| Gate | Check 7: organizer export is 200 and yields CSV text |
| Reproduction | `python3 run.py .dogfood.toml` (check 7), `pytest -q tests/test_runbook_engine.py::test_runbook_never_gates_csv_export` |
| Evidence | 200, `text/csv`, 136 data rows on the official event, pending rows present with empty score fields, participant/judge/anon exports all 403. |

At this point the official suite is **7/7**, which is the brief's condition for
proceeding.

## GATE 5 — Normalization · **PASS**

| | |
|---|---|
| Built | Weighted raw totals, per-judge mean + population σ, z-scores, neutral fallbacks (σ=0, <3 reviews), raw-fallback mode, project aggregation, deterministic ranking and tie-breaking, snapshot persistence, fingerprint freshness, `JUDGING.md` written against the code |
| Gate | Deterministic math, all documented edge cases, no crash on empty/partial review sets |
| Reproduction | `pytest -q tests/test_normalization_maths.py` |
| Evidence | σ = 2.0 exactly on the classic 8-value set; a flat judge yields σ = 0 → z = 0; live seeded event ranks monotonically with sequential ranks; the documented tie-break order `(-score, -raw_mean, project_id)` is asserted. `JUDGING.md` states the exact equations and every fallback. |

## GATE 6 — Runbook · **PASS**

| | |
|---|---|
| Built | Eight computed stages, the five required states only, metrics/reasons/evidence/next-action per stage, submissions as an observation stage, assignment-gap and idle-judge detection, normalization freshness, STALE detection by fingerprint, deterministic next-action engine |
| Gate | UPCOMING→ACTIVE→COMPLETE transitions, COMPLETE→STALE after a new review, and export unaffected by Runbook state |
| Reproduction | `pytest -q tests/test_runbook_engine.py`, `python3 scripts/demo_flow.py` |
| Evidence (live HTTP run) | Boot: `judging ACTIVE 122/136`, `normalization STALE (last 116 · current 122)`, next action *Continue judging*. After a judge submits one review: `123/136`, STALE delta 7. After the organizer normalizes: `COMPLETE 123 reviews`, next action *Continue judging*. After a later review edit: STALE again at equal counts ("scores changed"). CSV stayed 200 throughout. |

## GATE 7 — Polish · **IN PROGRESS (deliberately last)**

| | |
|---|---|
| Built so far | Dark header + card system, status pills colour-coded per state, Runbook two-column hierarchy with next-action banner, judge left/right split, evidence disclosure, empty states, error and deadline-closed pages, copy that distinguishes observation from action |
| Remaining | Visual pass on the Runbook banner hierarchy, judge scoring sliders vs. number fields, mobile layout of the Runbook tables, README screenshots |
| Rule from the brief | No architectural changes in this phase — polish only, and every change re-runs gates 2–6 |

---

### Phase discipline

A change is only merged into a phase when its gate is re-run green. The two
things that are *not* claimed: **Docker was not executed in this environment**
(gate 0), and the normalization ranking is **provisional while coverage is
incomplete** (documented in `JUDGING.md`, surfaced on the Runbook card).

## Final hardening gate (2026-09-28 local date) — **CODE VERIFIED; CONTAINER UNVERIFIED**

- `python -m compileall -q app tests`: exit 0.
- `pytest -q`: 51 passed in 123.59s (final run). Tests cover nine workflow/publication/export cases plus unassigned projects, metadata freshness, admin least privilege, schedule validation, zero-review stale snapshots, eligible judge selection, browser publication and publication-run freshness (a published snapshot stales on later changes and requires republication; legacy timestamps are unverified until republished).
- `python3 scripts/smoke.py`: all smoke checks passed.
- `python3 run.py .dogfood.toml` against a newly started Python server on port 8080: 7/7 PASS.
- `python3 scripts/demo_flow.py` against that server: completed; the Next Action stays **Continue judging** with 122/136, 123/136, after normalization at 123/136, and after another score edit. CSV stayed 200 and declared stale after edits.
- `docker compose version`: not executable (`docker: command not found`). `docker compose up --build` was **not run**, so container boot remains unverified.
- Publication is a new final-result gate: both HTTP routes refuse incomplete, never-run and stale snapshots (409). Operational CSV is never gated by this policy.
- Remaining deployment risks: demo sessions and credentials, default secret, no CSRF protection on browser forms, no rate limiting, and a development Flask server. Publication freshness is enforced instead by run linkage: each publication certifies a specific normalization run and turns STALE on later result-input changes until deliberately republished.
